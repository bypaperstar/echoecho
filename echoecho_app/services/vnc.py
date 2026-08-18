"""A tiny, dependency-free RFB (VNC) client — enough to inject keyboard and
pointer input into echoecho's macOS guest.

Why this exists: the SSH GUI driver (services/gui.py) can `open -a` an app and
`screencapture` the screen, but its `osascript` keystrokes need Accessibility
(TCC) permission, which a SIP-enabled vanilla image will not grant to an
SSH-invoked process — the call hangs on an unanswerable GUI prompt (verified
live). Input delivered over VNC arrives as *virtual HID* events, the same path
a human at Screen Sharing uses, so it sidesteps TCC entirely. Lume exposes the
guest's built-in VNC server even under `--no-display` (its address+password are
the `vncUrl` from `lume get`), which is the endpoint we drive here.

Scope is deliberately small: connect + authenticate (RFB 3.x, security types
None and VNC-DES), KeyEvent / PointerEvent (RFB §7.5.4/§7.5.5), and bounded
raw true-colour framebuffer capture. Everything is blocking socket I/O wrapped
by the async driver in a thread, keeping this file a plain, unit-testable
protocol implementation with no event loop.

Python 3.9, stdlib only (the in-process no-new-deps rule): the VNC challenge
uses DES, which the stdlib lacks, so a compact DES lives here too.
"""
import binascii
import os
import socket
import struct
import tempfile
import time
import zlib
from urllib.parse import urlparse

from echoecho_app import diagnostics


MAX_FAILURE_REASON_BYTES = 64 * 1024
MAX_SERVER_NAME_BYTES = 64 * 1024
MAX_CLIPBOARD_BYTES = 1024 * 1024
MAX_FRAMEBUFFER_PIXELS = 32 * 1024 * 1024
MAX_CAPTURE_RECTS = 4096
MAX_CAPTURE_UPDATES = 16
MAX_CAPTURE_MESSAGES = 8192
MAX_CAPTURE_AUX_BYTES = 4 * 1024 * 1024
MAX_CAPTURE_SECONDS = 60.0
MAX_TYPE_CHARS = 4096
MAX_TYPE_SECONDS = 120.0

# X11 keysyms for the keys we actually send. Printable ASCII maps to its own
# code point (RFB uses X keysyms; for 0x20-0x7e keysym == the character), so we
# only special-case the non-printing keys and modifiers.
KEYSYMS = {
    "return": 0xFF0D, "enter": 0xFF0D, "tab": 0xFF09, "space": 0x0020,
    "escape": 0xFF1B, "esc": 0xFF1B, "backspace": 0xFF08, "delete": 0xFFFF,
    "left": 0xFF51, "up": 0xFF52, "right": 0xFF53, "down": 0xFF54,
    "home": 0xFF50, "end": 0xFF57, "pageup": 0xFF55, "pagedown": 0xFF56,
}
# Modifier keysyms. On macOS VNC, Command is delivered as Meta and Option as
# Alt; Super_L works for Command on some servers, so it's the documented knob
# if a guest maps it differently (ECHOECHO_VNC_CMD_KEYSYM).
MODIFIER_KEYSYMS = {
    "shift": 0xFFE1, "control": 0xFFE3, "ctrl": 0xFFE3,
    "option": 0xFFE9, "opt": 0xFFE9, "alt": 0xFFE9,
    "command": 0xFFE7, "cmd": 0xFFE7,
}


class VncError(Exception):
    pass


# -- DES (VNC authentication only) -------------------------------------------
# Standard DES on one 8-byte block. VNC auth's only quirk is that each key byte
# is bit-reversed before use (_vnc_key_bytes handles that); the cipher itself is
# textbook DES, validated against the FIPS test vector in the unit tests.

_IP = [58, 50, 42, 34, 26, 18, 10, 2, 60, 52, 44, 36, 28, 20, 12, 4,
       62, 54, 46, 38, 30, 22, 14, 6, 64, 56, 48, 40, 32, 24, 16, 8,
       57, 49, 41, 33, 25, 17, 9, 1, 59, 51, 43, 35, 27, 19, 11, 3,
       61, 53, 45, 37, 29, 21, 13, 5, 63, 55, 47, 39, 31, 23, 15, 7]
_FP = [40, 8, 48, 16, 56, 24, 64, 32, 39, 7, 47, 15, 55, 23, 63, 31,
       38, 6, 46, 14, 54, 22, 62, 30, 37, 5, 45, 13, 53, 21, 61, 29,
       36, 4, 44, 12, 52, 20, 60, 28, 35, 3, 43, 11, 51, 19, 59, 27,
       34, 2, 42, 10, 50, 18, 58, 26, 33, 1, 41, 9, 49, 17, 57, 25]
_E = [32, 1, 2, 3, 4, 5, 4, 5, 6, 7, 8, 9, 8, 9, 10, 11, 12, 13, 12, 13,
      14, 15, 16, 17, 16, 17, 18, 19, 20, 21, 20, 21, 22, 23, 24, 25,
      24, 25, 26, 27, 28, 29, 28, 29, 30, 31, 32, 1]
_P = [16, 7, 20, 21, 29, 12, 28, 17, 1, 15, 23, 26, 5, 18, 31, 10,
      2, 8, 24, 14, 32, 27, 3, 9, 19, 13, 30, 6, 22, 11, 4, 25]
_PC1 = [57, 49, 41, 33, 25, 17, 9, 1, 58, 50, 42, 34, 26, 18, 10, 2,
        59, 51, 43, 35, 27, 19, 11, 3, 60, 52, 44, 36, 63, 55, 47, 39,
        31, 23, 15, 7, 62, 54, 46, 38, 30, 22, 14, 6, 61, 53, 45, 37,
        29, 21, 13, 5, 28, 20, 12, 4]
_PC2 = [14, 17, 11, 24, 1, 5, 3, 28, 15, 6, 21, 10, 23, 19, 12, 4,
        26, 8, 16, 7, 27, 20, 13, 2, 41, 52, 31, 37, 47, 55, 30, 40,
        51, 45, 33, 48, 44, 49, 39, 56, 34, 53, 46, 42, 50, 36, 29, 32]
_SHIFTS = [1, 1, 2, 2, 2, 2, 2, 2, 1, 2, 2, 2, 2, 2, 2, 1]
_SBOX = [
    [14, 4, 13, 1, 2, 15, 11, 8, 3, 10, 6, 12, 5, 9, 0, 7,
     0, 15, 7, 4, 14, 2, 13, 1, 10, 6, 12, 11, 9, 5, 3, 8,
     4, 1, 14, 8, 13, 6, 2, 11, 15, 12, 9, 7, 3, 10, 5, 0,
     15, 12, 8, 2, 4, 9, 1, 7, 5, 11, 3, 14, 10, 0, 6, 13],
    [15, 1, 8, 14, 6, 11, 3, 4, 9, 7, 2, 13, 12, 0, 5, 10,
     3, 13, 4, 7, 15, 2, 8, 14, 12, 0, 1, 10, 6, 9, 11, 5,
     0, 14, 7, 11, 10, 4, 13, 1, 5, 8, 12, 6, 9, 3, 2, 15,
     13, 8, 10, 1, 3, 15, 4, 2, 11, 6, 7, 12, 0, 5, 14, 9],
    [10, 0, 9, 14, 6, 3, 15, 5, 1, 13, 12, 7, 11, 4, 2, 8,
     13, 7, 0, 9, 3, 4, 6, 10, 2, 8, 5, 14, 12, 11, 15, 1,
     13, 6, 4, 9, 8, 15, 3, 0, 11, 1, 2, 12, 5, 10, 14, 7,
     1, 10, 13, 0, 6, 9, 8, 7, 4, 15, 14, 3, 11, 5, 2, 12],
    [7, 13, 14, 3, 0, 6, 9, 10, 1, 2, 8, 5, 11, 12, 4, 15,
     13, 8, 11, 5, 6, 15, 0, 3, 4, 7, 2, 12, 1, 10, 14, 9,
     10, 6, 9, 0, 12, 11, 7, 13, 15, 1, 3, 14, 5, 2, 8, 4,
     3, 15, 0, 6, 10, 1, 13, 8, 9, 4, 5, 11, 12, 7, 2, 14],
    [2, 12, 4, 1, 7, 10, 11, 6, 8, 5, 3, 15, 13, 0, 14, 9,
     14, 11, 2, 12, 4, 7, 13, 1, 5, 0, 15, 10, 3, 9, 8, 6,
     4, 2, 1, 11, 10, 13, 7, 8, 15, 9, 12, 5, 6, 3, 0, 14,
     11, 8, 12, 7, 1, 14, 2, 13, 6, 15, 0, 9, 10, 4, 5, 3],
    [12, 1, 10, 15, 9, 2, 6, 8, 0, 13, 3, 4, 14, 7, 5, 11,
     10, 15, 4, 2, 7, 12, 9, 5, 6, 1, 13, 14, 0, 11, 3, 8,
     9, 14, 15, 5, 2, 8, 12, 3, 7, 0, 4, 10, 1, 13, 11, 6,
     4, 3, 2, 12, 9, 5, 15, 10, 11, 14, 1, 7, 6, 0, 8, 13],
    [4, 11, 2, 14, 15, 0, 8, 13, 3, 12, 9, 7, 5, 10, 6, 1,
     13, 0, 11, 7, 4, 9, 1, 10, 14, 3, 5, 12, 2, 15, 8, 6,
     1, 4, 11, 13, 12, 3, 7, 14, 10, 15, 6, 8, 0, 5, 9, 2,
     6, 11, 13, 8, 1, 4, 10, 7, 9, 5, 0, 15, 14, 2, 3, 12],
    [13, 2, 8, 4, 6, 15, 11, 1, 10, 9, 3, 14, 5, 0, 12, 7,
     1, 15, 13, 8, 10, 3, 7, 4, 12, 5, 6, 11, 0, 14, 9, 2,
     7, 11, 4, 1, 9, 12, 14, 2, 0, 6, 10, 13, 15, 3, 5, 8,
     2, 1, 14, 7, 4, 10, 8, 13, 15, 12, 9, 0, 3, 5, 6, 11],
]


def _bits(data):
    out = []
    for byte in data:
        for i in range(7, -1, -1):
            out.append((byte >> i) & 1)
    return out


def _frombits(bits):
    out = bytearray()
    for i in range(0, len(bits), 8):
        byte = 0
        for b in bits[i:i + 8]:
            byte = (byte << 1) | b
        out.append(byte)
    return bytes(out)


def _permute(bits, table):
    return [bits[i - 1] for i in table]


def _key_schedule(key8):
    key = _permute(_bits(key8), _PC1)
    c, d = key[:28], key[28:]
    subkeys = []
    for shift in _SHIFTS:
        c = c[shift:] + c[:shift]
        d = d[shift:] + d[:shift]
        subkeys.append(_permute(c + d, _PC2))
    return subkeys


def _feistel(r, subkey):
    x = [a ^ b for a, b in zip(_permute(r, _E), subkey)]
    out = []
    for i in range(8):
        chunk = x[i * 6:i * 6 + 6]
        row = (chunk[0] << 1) | chunk[5]
        col = (chunk[1] << 3) | (chunk[2] << 2) | (chunk[3] << 1) | chunk[4]
        val = _SBOX[i][row * 16 + col]
        out += [(val >> 3) & 1, (val >> 2) & 1, (val >> 1) & 1, val & 1]
    return _permute(out, _P)


def des_encrypt_block(key8, block8):
    """Textbook single-block DES encryption (8-byte key, 8-byte block)."""
    subkeys = _key_schedule(key8)
    bits = _permute(_bits(block8), _IP)
    l, r = bits[:32], bits[32:]
    for k in subkeys:
        l, r = r, [a ^ b for a, b in zip(l, _feistel(r, k))]
    return _frombits(_permute(r + l, _FP))


def _reverse_bits(byte):
    return int('{:08b}'.format(byte)[::-1], 2)


def _vnc_key_bytes(password):
    """VNC auth key: the password truncated/NUL-padded to 8 bytes, each byte
    bit-reversed (RealVNC's DES variant)."""
    raw = password.encode("latin-1", "replace")[:8]
    raw = raw + b"\x00" * (8 - len(raw))
    return bytes(_reverse_bits(b) for b in raw)


def vnc_challenge_response(password, challenge16):
    key = _vnc_key_bytes(password)
    return b"".join(des_encrypt_block(key, challenge16[i:i + 8])
                    for i in (0, 8))


# -- vncUrl parsing ----------------------------------------------------------

def _write_png(path, width, height, rgb, deadline=None):
    """Write a truecolour PNG from packed RGB bytes — stdlib only (zlib), so
    no Pillow dependency (the in-process no-new-deps rule)."""
    def write_chunk(stream, tag, data):
        checksum = binascii.crc32(data, binascii.crc32(tag)) & 0xFFFFFFFF
        stream.write(struct.pack(">I", len(data)))
        stream.write(tag)
        stream.write(data)
        stream.write(struct.pack(">I", checksum))

    stride = width * 3
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8-bit RGB
    path = os.fspath(path)
    parent = os.path.dirname(os.path.abspath(path))
    fd, temporary = tempfile.mkstemp(prefix=".vnc-capture-", suffix=".png",
                                     dir=parent)
    try:
        with os.fdopen(fd, "wb") as f:
            fd = None
            os.fchmod(f.fileno(), 0o600)
            f.write(b"\x89PNG\r\n\x1a\n")
            write_chunk(f, b"IHDR", ihdr)
            compressor = zlib.compressobj(6)
            for y in range(height):
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError(
                        "VNC capture exceeded its total deadline")
                # PNG filter 0 plus one scanline. Compress incrementally so a
                # large but valid framebuffer never creates another full copy.
                row = b"\x00" + bytes(rgb[y * stride:(y + 1) * stride])
                compressed = compressor.compress(row)
                if compressed:
                    write_chunk(f, b"IDAT", compressed)
            compressed = compressor.flush()
            if compressed:
                write_chunk(f, b"IDAT", compressed)
            write_chunk(f, b"IEND", b"")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    except BaseException:
        if fd is not None:
            os.close(fd)
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return path


def parse_vnc_url(url):
    """(host, port, password) from a vnc://[:password@]host:port URL. Lume
    reports the guest's built-in server this way (services/vm.vnc_url())."""
    try:
        parsed = urlparse(str(url or ""))
        port = parsed.port or 5900
    except (TypeError, ValueError):
        raise VncError("invalid VNC URL")
    if parsed.scheme != "vnc":
        raise VncError("VNC URL must use the vnc:// scheme")
    host = parsed.hostname
    password = parsed.password or ""
    if not host:
        raise VncError("VNC URL has no host")
    return host, port, password


# -- RFB client --------------------------------------------------------------

class VncClient:
    """Blocking RFB client: connect(), then key()/type_text()/pointer(). Used
    from the async GUI driver via asyncio.to_thread, so it stays sync and
    testable against a plain socket."""

    def __init__(self, host, port, password="", timeout=15.0):
        self.host = host
        self.port = port
        self.password = password
        self.timeout = timeout
        self.sock = None
        self.width = 0
        self.height = 0
        self.name = ""
        self.pixfmt = {}
        self.connection_id = diagnostics.new_id("vnc")
        self._diag_started = None
        self._diag_ready_at = None
        self._diag_phase = "created"
        self._diag_protocol_minor = None
        self._diag_security_type = None
        self._diag_bytes_received = 0
        self._diag_bytes_sent = 0
        self._diag_recv_calls = 0
        self._diag_send_calls = 0
        self._diag_key_events = 0
        self._diag_pointer_events = 0
        self._diag_text_operations = 0
        self._diag_text_chars = 0
        self._diag_text_dropped = 0
        self._diag_capture_count = 0
        self._diag_capture_rects = 0
        self._diag_capture_pixels = 0
        self._diag_failures = 0
        self._diag_close_reported = False

    # -- lifecycle --
    def connect(self):
        self._diag_started = time.monotonic()
        self._diag_close_reported = False
        self._diag_phase = "socket"
        diagnostics.info(
            "gui.vnc.connect.started", connection_id=self.connection_id,
            timeout_s=self.timeout, password_present=bool(self.password))
        try:
            socket_started = time.monotonic()
            self.sock = socket.create_connection((self.host, self.port),
                                                  timeout=self.timeout)
            self.sock.settimeout(self.timeout)
            diagnostics.info(
                "gui.vnc.socket.connected", connection_id=self.connection_id,
                duration_ms=round(
                    (time.monotonic() - socket_started) * 1000, 1))
            self._handshake()
        except Exception as exc:
            self._diag_failures += 1
            diagnostics.exception(
                "gui.vnc.connection.failed", exc=exc,
                connection_id=self.connection_id, stage=self._diag_phase,
                duration_ms=round(
                    (time.monotonic() - self._diag_started) * 1000, 1))
            try:
                self.close(outcome="connect_failed")
            except Exception:
                # Preserve the handshake/socket exception. close() already
                # emitted its own bounded failure record.
                pass
            raise
        self._diag_ready_at = time.monotonic()
        diagnostics.info(
            "gui.vnc.server.ready", connection_id=self.connection_id,
            protocol_minor=self._diag_protocol_minor,
            security_type=self._diag_security_type,
            width=self.width, height=self.height,
            bpp=self.pixfmt.get("bpp"), depth=self.pixfmt.get("depth"),
            big_endian=bool(self.pixfmt.get("big_endian")),
            true_color=bool(self.pixfmt.get("true_color")),
            server_name_chars=len(self.name),
            duration_ms=round(
                (self._diag_ready_at - self._diag_started) * 1000, 1))
        return self

    def close(self, outcome="closed"):
        close_error = None
        if self.sock is not None:
            try:
                self.sock.close()
            except Exception as exc:
                close_error = exc
                self._diag_failures += 1
                diagnostics.exception(
                    "gui.vnc.connection.close_failed", exc=exc,
                    connection_id=self.connection_id)
            finally:
                self.sock = None
        if not self._diag_close_reported and self._diag_started is not None:
            self._diag_close_reported = True
            diagnostics.info(
                "gui.vnc.connection.closed",
                connection_id=self.connection_id, outcome=outcome,
                ready=self._diag_ready_at is not None,
                duration_ms=round(
                    (time.monotonic() - self._diag_started) * 1000, 1),
                bytes_received=self._diag_bytes_received,
                bytes_sent=self._diag_bytes_sent,
                recv_calls=self._diag_recv_calls,
                send_calls=self._diag_send_calls,
                key_event_count=self._diag_key_events,
                pointer_event_count=self._diag_pointer_events,
                text_operation_count=self._diag_text_operations,
                text_chars=self._diag_text_chars,
                text_dropped=self._diag_text_dropped,
                capture_count=self._diag_capture_count,
                capture_rects=self._diag_capture_rects,
                capture_pixels=self._diag_capture_pixels,
                failure_count=self._diag_failures,
                clean=close_error is None)
        if close_error is not None:
            raise close_error

    def __enter__(self):
        return self.connect()

    def __exit__(self, *exc):
        self.close()

    # -- socket helpers --
    def _recv(self, n, deadline=None):
        buf = bytearray()
        while len(buf) < n:
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        "VNC capture exceeded its total deadline")
                current_timeout = self.sock.gettimeout()
                if current_timeout is None or current_timeout > remaining:
                    self.sock.settimeout(remaining)
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise VncError("VNC server closed the connection mid-message")
            self._diag_recv_calls += 1
            self._diag_bytes_received += len(chunk)
            buf.extend(chunk)
        return bytes(buf)

    def _send(self, data):
        self.sock.sendall(data)
        self._diag_send_calls += 1
        self._diag_bytes_sent += len(data)

    # -- handshake (RFB 3.3/3.7/3.8) --
    def _handshake(self):
        self._diag_phase = "version"
        server_version = self._recv(12)
        if not server_version.startswith(b"RFB "):
            raise VncError("not an RFB server: %r" % server_version)
        try:
            major, minor = (int(server_version[4:7]), int(server_version[8:11]))
        except ValueError:
            major, minor = 0, 0
        # RFC 6143 defines only 3.3, 3.7, and 3.8. Unknown/nonstandard 3.x
        # versions must be treated as 3.3 because their handshake shape is
        # otherwise unknowable (Appendix A).
        want_minor = minor if (major, minor) in {(3, 7), (3, 8)} else 3
        want = (3, want_minor)
        self._diag_protocol_minor = want[1]
        diagnostics.info(
            "gui.vnc.protocol.negotiated",
            connection_id=self.connection_id,
            server_major=major, server_minor=minor,
            client_major=3, client_minor=want[1])
        self._send(b"RFB %03d.%03d\n" % (3, want[1]))
        self._diag_phase = "auth"
        self._authenticate(want[1])
        self._diag_phase = "server_init"
        self._client_init()
        self._diag_phase = "ready"

    def _authenticate(self, minor):
        started = time.monotonic()
        types = []
        if minor >= 7:
            count = self._recv(1)[0]
            if count == 0:
                raise VncError(self._read_fail_reason(
                    "server offered no security types"))
            types = list(self._recv(count))
            chosen = self._choose_security(types)
            self._send(bytes([chosen]))
        else:  # 3.3: the server dictates a single 4-byte security type
            chosen = struct.unpack(">I", self._recv(4))[0]
            types = [chosen]
            if chosen == 0:
                raise VncError(self._read_fail_reason("connection failed"))
            if self.password and chosen == 1:
                raise VncError(
                    "VNC server requires an authentication downgrade")
        self._diag_security_type = (
            "none" if chosen == 1 else "vnc_des" if chosen == 2 else "other")
        diagnostics.info(
            "gui.vnc.auth.negotiated", connection_id=self.connection_id,
            offered_count=len(types), supports_none=1 in types,
            supports_vnc_auth=2 in types,
            security_type=self._diag_security_type,
            password_present=bool(self.password), downgrade_to_none=False)
        if chosen == 1:  # None
            pass
        elif chosen == 2:  # VNC authentication (DES challenge)
            challenge = self._recv(16)
            self._send(vnc_challenge_response(self.password, challenge))
        else:
            raise VncError("unsupported VNC security type %d" % chosen)
        # SecurityResult: present for >=3.8 always, and for 3.7/3.3 after auth
        if minor >= 8 or chosen != 1:
            result = struct.unpack(">I", self._recv(4))[0]
            if result != 0:
                reason = (self._read_fail_reason("VNC authentication failed")
                          if minor >= 8 else "VNC authentication failed")
                raise VncError(reason)
        diagnostics.info(
            "gui.vnc.auth.finished", connection_id=self.connection_id,
            security_type=self._diag_security_type, outcome="ok",
            duration_ms=round((time.monotonic() - started) * 1000, 1))

    def _choose_security(self, types):
        if not self.password and 1 in types:
            return 1
        if 2 in types:
            return 2
        if self.password and 1 in types:
            diagnostics.warning(
                "gui.vnc.auth.downgrade_rejected",
                connection_id=self.connection_id,
                offered_count=len(types), password_present=True)
            raise VncError(
                "VNC server does not offer password authentication")
        raise VncError("no supported VNC security type (offered: %s)"
                       % ", ".join(map(str, types)))

    def _read_fail_reason(self, prefix):
        try:
            n = struct.unpack(">I", self._recv(4))[0]
            if n > MAX_FAILURE_REASON_BYTES:
                diagnostics.warning(
                    "gui.vnc.peer_value.rejected",
                    connection_id=self.connection_id, stage=self._diag_phase,
                    kind="failure_reason_length", declared_size=n,
                    max_size=MAX_FAILURE_REASON_BYTES)
                return "%s (server reason omitted: too large)" % prefix
            reason = self._recv(n).decode("utf-8", "replace")
            return "%s: %s" % (prefix, reason)
        except Exception:
            return prefix

    def _client_init(self):
        self._send(b"\x01")  # shared-session flag: don't boot other viewers
        header = self._recv(24)
        self.width, self.height = struct.unpack(">HH", header[:4])
        # ServerInit pixel format (16 bytes). We decode in the SERVER's native
        # format rather than forcing one with SetPixelFormat — Apple's guest
        # VNC server resets the connection on an unexpected SetPixelFormat.
        pf = header[4:20]
        rmax, gmax, bmax = struct.unpack(">HHH", pf[4:10])
        self.pixfmt = {
            "bpp": pf[0], "depth": pf[1], "big_endian": pf[2],
            "true_color": pf[3], "rmax": rmax, "gmax": gmax, "bmax": bmax,
            "rshift": pf[10], "gshift": pf[11], "bshift": pf[12]}
        name_len = struct.unpack(">I", header[20:24])[0]
        if name_len > MAX_SERVER_NAME_BYTES:
            diagnostics.warning(
                "gui.vnc.peer_value.rejected",
                connection_id=self.connection_id, stage=self._diag_phase,
                kind="server_name_length", declared_size=name_len,
                max_size=MAX_SERVER_NAME_BYTES)
            raise VncError("VNC server name is too large")
        self.name = self._recv(name_len).decode("utf-8", "replace") if \
            name_len else ""

    # -- input events --
    def key_event(self, keysym, down):
        # RFB §7.5.4 KeyEvent: type=4, down-flag, padding, keysym
        self._send(struct.pack(">BBHI", 4, 1 if down else 0, 0, keysym))
        self._diag_key_events += 1

    def tap(self, keysym, hold=0.02):
        pressed = False
        try:
            self.key_event(keysym, True)
            pressed = True
            time.sleep(hold)
        finally:
            if pressed:
                self.key_event(keysym, False)
        time.sleep(hold)

    def chord(self, modifiers, keysym, hold=0.03):
        """Press modifier keysyms, tap the key, release modifiers (reverse)."""
        pressed_modifiers = []
        key_pressed = False
        primary_error = None
        release_error = None
        try:
            for modifier in modifiers:
                self.key_event(modifier, True)
                pressed_modifiers.append(modifier)
            time.sleep(hold)
            self.key_event(keysym, True)
            key_pressed = True
            time.sleep(hold)
        except BaseException as exc:
            primary_error = exc
        finally:
            if key_pressed:
                try:
                    self.key_event(keysym, False)
                except BaseException as exc:
                    release_error = release_error or exc
            time.sleep(hold)
            for modifier in reversed(pressed_modifiers):
                try:
                    self.key_event(modifier, False)
                except BaseException as exc:
                    release_error = release_error or exc
        if primary_error is not None:
            raise primary_error
        if release_error is not None:
            raise release_error
        time.sleep(hold)

    def type_text(self, text, delay=0.03):
        if not isinstance(text, str):
            raise TypeError("VNC text input must be a string")
        if len(text) > MAX_TYPE_CHARS:
            diagnostics.warning(
                "gui.vnc.input.rejected", connection_id=self.connection_id,
                action="type", kind="text_length", declared_size=len(text),
                max_size=MAX_TYPE_CHARS)
            raise ValueError("VNC text input is too long")
        started = time.monotonic()
        deadline = started + MAX_TYPE_SECONDS
        sent = 0
        dropped = 0
        self._diag_text_operations += 1
        try:
            for ch in text:
                if time.monotonic() >= deadline:
                    raise TimeoutError("VNC text input exceeded its time limit")
                if ch == "\n":
                    self.tap(KEYSYMS["return"])
                    sent += 1
                elif ch == "\t":
                    self.tap(KEYSYMS["tab"])
                    sent += 1
                elif 0x20 <= ord(ch) <= 0x7E:
                    keysym = ord(ch)  # printable ASCII keysym == code point
                    shift = ch.isupper() or ch in '~!@#$%^&*()_+{}|:"<>?'
                    if shift:
                        self.chord([MODIFIER_KEYSYMS["shift"]], keysym)
                    else:
                        self.tap(keysym)
                    sent += 1
                else:
                    # keysyms for Latin-1 also equal the code point; anything
                    # above is outside this minimal driver's scope.
                    cp = ord(ch)
                    if cp <= 0xFF:
                        self.tap(cp)
                        sent += 1
                    else:
                        dropped += 1
                time.sleep(delay)
        except Exception as exc:
            self._diag_failures += 1
            diagnostics.exception(
                "gui.vnc.input.failed", exc=exc,
                connection_id=self.connection_id, action="type",
                requested_chars=len(text), sent_chars=sent,
                dropped_chars=dropped,
                duration_ms=round((time.monotonic() - started) * 1000, 1))
            raise
        finally:
            self._diag_text_chars += sent
            self._diag_text_dropped += dropped
        diagnostics.info(
            "gui.vnc.input.finished", connection_id=self.connection_id,
            action="type", requested_chars=len(text), sent_chars=sent,
            dropped_chars=dropped,
            duration_ms=round((time.monotonic() - started) * 1000, 1))

    # -- framebuffer capture (what a VNC viewer actually sees) --------------
    # On a headless (--no-display) guest, app windows composite for a
    # connected VNC client but not always for `screencapture` over SSH, so
    # this is the faithful "what's on screen" grab. We decode in the server's
    # NATIVE pixel format (parsed at ServerInit) — Apple's guest VNC server
    # resets the connection if a client forces a format via SetPixelFormat.
    def set_encodings(self, encodings):
        msg = struct.pack(">BBH", 2, 0, len(encodings))
        for e in encodings:
            msg += struct.pack(">i", e)
        self._send(msg)

    def _fb_update_request(self, incremental=0):
        self._send(struct.pack(">BBHHHH", 3, incremental, 0, 0,
                               self.width, self.height))

    def _channel_bytes(self, bpp):
        """For a 32/24bpp true-colour native format, the byte offset (within
        each pixel) of R, G, B — so a rectangle decodes with C-level slicing
        instead of a per-pixel Python loop. Returns None for exotic formats
        (handled by the slow generic path)."""
        pf = self.pixfmt
        if not pf.get("true_color") or bpp not in (24, 32):
            return None
        if pf["rmax"] != 255 or pf["gmax"] != 255 or pf["bmax"] != 255:
            return None
        nbytes = bpp // 8

        def byte_of(shift):
            idx = shift // 8
            return (nbytes - 1 - idx) if pf["big_endian"] else idx
        shifts = (pf["rshift"], pf["gshift"], pf["bshift"])
        if any(shift % 8 for shift in shifts):
            return None
        offsets = tuple(byte_of(shift) for shift in shifts)
        if len(set(offsets)) != 3 or any(
                offset < 0 or offset >= nbytes for offset in offsets):
            return None
        return offsets

    def capture_png(self, path, timeout=None):
        """Request the whole framebuffer (raw encoding) and write it to a PNG.
        Blocks until validated rectangles cover the complete screen."""
        started = time.monotonic()
        try:
            requested_timeout = float(
                self.timeout if timeout is None else timeout)
        except (TypeError, ValueError):
            requested_timeout = MAX_CAPTURE_SECONDS
        total_timeout = max(
            0.001, min(MAX_CAPTURE_SECONDS, requested_timeout))
        deadline = started + total_timeout
        bpp = self.pixfmt["bpp"]
        nbytes = bpp // 8
        offs = self._channel_bytes(bpp)
        if offs is None:
            raise VncError(
                "unsupported native pixel format for capture: %r" % self.pixfmt)
        total_pixels = self.width * self.height
        if (self.width <= 0 or self.height <= 0 or
                total_pixels > MAX_FRAMEBUFFER_PIXELS):
            diagnostics.warning(
                "gui.vnc.peer_value.rejected",
                connection_id=self.connection_id, stage="capture",
                kind="framebuffer_dimensions", declared_size=total_pixels,
                max_size=MAX_FRAMEBUFFER_PIXELS)
            raise VncError("VNC framebuffer dimensions exceed capture limits")
        diagnostics.info(
            "gui.vnc.capture.started", connection_id=self.connection_id,
            width=self.width, height=self.height, bpp=bpp,
            expected_pixels=total_pixels, encoding="raw",
            timeout_s=total_timeout)
        buf = bytearray(total_pixels * 3)
        coverage = [[] for _ in range(self.height)]
        covered = 0
        rect_count = 0
        update_count = 0
        message_count = 0
        auxiliary_bytes = 0
        wire_pixels = 0
        max_wire_pixels = total_pixels * 4
        previous_socket_timeout = self.sock.gettimeout()

        def check_deadline():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    "VNC capture exceeded its total deadline")
            return remaining

        def recv(size):
            return self._recv(size, deadline=deadline)

        def add_coverage(x, y, w, h):
            added = 0
            for row in range(y, y + h):
                if (row - y) % 64 == 0:
                    check_deadline()
                start, end = x, x + w
                intervals = coverage[row]
                merged = []
                before = sum(right - left for left, right in intervals)
                placed = False
                for index, (left, right) in enumerate(intervals):
                    if index % 256 == 0:
                        check_deadline()
                    if right < start:
                        merged.append((left, right))
                    elif end < left:
                        if not placed:
                            merged.append((start, end))
                            placed = True
                        merged.append((left, right))
                    else:
                        start = min(start, left)
                        end = max(end, right)
                if not placed:
                    merged.append((start, end))
                coverage[row] = merged
                after = sum(right - left for left, right in merged)
                added += after - before
            return added

        try:
            # Apply one absolute budget to the request, every response byte,
            # rectangle processing, and PNG encoding. _recv() tightens the
            # socket timeout as that budget shrinks.
            active_timeout = total_timeout
            if (previous_socket_timeout is not None and
                    previous_socket_timeout > 0):
                active_timeout = min(active_timeout, previous_socket_timeout)
            self.sock.settimeout(active_timeout)
            check_deadline()
            self.set_encodings([0])  # raw only — no decoder zoo to maintain
            check_deadline()
            self._fb_update_request(0)
            while covered < total_pixels:
                check_deadline()
                message_count += 1
                if message_count > MAX_CAPTURE_MESSAGES:
                    raise VncError("VNC capture exceeded its message limit")
                msg_type = recv(1)[0]
                if msg_type == 0:  # FramebufferUpdate
                    update_count += 1
                    if update_count > MAX_CAPTURE_UPDATES:
                        raise VncError(
                            "VNC capture exceeded framebuffer update limit")
                    recv(1)  # padding
                    nrects = struct.unpack(">H", recv(2))[0]
                    if (nrects > MAX_CAPTURE_RECTS or
                            rect_count + nrects > MAX_CAPTURE_RECTS):
                        diagnostics.warning(
                            "gui.vnc.peer_value.rejected",
                            connection_id=self.connection_id, stage="capture",
                            kind="rectangle_count",
                            declared_size=rect_count + nrects,
                            max_size=MAX_CAPTURE_RECTS)
                        raise VncError("VNC capture has too many rectangles")
                    for _ in range(nrects):
                        x, y, w, h, enc = struct.unpack(
                            ">HHHHi", recv(12))
                        if enc != 0:
                            raise VncError(
                                "server used non-raw encoding %d" % enc)
                        if (w <= 0 or h <= 0 or x + w > self.width or
                                y + h > self.height):
                            diagnostics.warning(
                                "gui.vnc.peer_value.rejected",
                                connection_id=self.connection_id,
                                stage="capture", kind="rectangle_bounds",
                                declared_size=w * h,
                                max_size=total_pixels)
                            raise VncError(
                                "VNC server sent an out-of-bounds rectangle")
                        rectangle_pixels = w * h
                        wire_pixels += rectangle_pixels
                        if wire_pixels > max_wire_pixels:
                            diagnostics.warning(
                                "gui.vnc.peer_value.rejected",
                                connection_id=self.connection_id,
                                stage="capture", kind="cumulative_pixels",
                                declared_size=wire_pixels,
                                max_size=max_wire_pixels)
                            raise VncError(
                                "VNC capture exceeded its pixel budget")
                        # Read/blit one row at a time; a full-screen rectangle
                        # must not create a second full-frame wire buffer.
                        row_bytes = w * nbytes
                        for row in range(h):
                            check_deadline()
                            data = recv(row_bytes)
                            self._blit(
                                buf, data, x, y + row, w, 1, nbytes, offs)
                        covered += add_coverage(x, y, w, h)
                        rect_count += 1
                    if covered < total_pixels:
                        check_deadline()
                        self._fb_update_request(0)
                elif msg_type == 1:  # SetColourMapEntries
                    header = recv(5)  # pad, first-colour, count
                    n = struct.unpack(">H", header[3:5])[0]
                    payload_bytes = n * 6
                    auxiliary_bytes += payload_bytes
                    if auxiliary_bytes > MAX_CAPTURE_AUX_BYTES:
                        raise VncError(
                            "VNC capture exceeded its auxiliary-data budget")
                    recv(payload_bytes)
                elif msg_type == 2:  # Bell
                    pass
                elif msg_type == 3:  # ServerCutText
                    recv(3)
                    n = struct.unpack(">I", recv(4))[0]
                    if n > MAX_CLIPBOARD_BYTES:
                        diagnostics.warning(
                            "gui.vnc.peer_value.rejected",
                            connection_id=self.connection_id, stage="capture",
                            kind="clipboard_length", declared_size=n,
                            max_size=MAX_CLIPBOARD_BYTES)
                        raise VncError("VNC clipboard message is too large")
                    auxiliary_bytes += n
                    if auxiliary_bytes > MAX_CAPTURE_AUX_BYTES:
                        raise VncError(
                            "VNC capture exceeded its auxiliary-data budget")
                    recv(n)  # discard content; never log it
                else:
                    raise VncError("unexpected server message %d" % msg_type)
            _write_png(
                path, self.width, self.height, buf, deadline=deadline)
        except Exception as exc:
            self._diag_failures += 1
            diagnostics.exception(
                "gui.vnc.capture.failed", exc=exc,
                connection_id=self.connection_id,
                expected_pixels=total_pixels, received_pixels=covered,
                rectangle_count=rect_count, update_count=update_count,
                message_count=message_count, wire_pixels=wire_pixels,
                auxiliary_bytes=auxiliary_bytes,
                duration_ms=round((time.monotonic() - started) * 1000, 1))
            raise
        finally:
            try:
                self.sock.settimeout(previous_socket_timeout)
            except (AttributeError, OSError):
                # A concurrent close already made the socket unusable. Never
                # replace the capture result/error with cleanup noise.
                pass
        self._diag_capture_count += 1
        self._diag_capture_rects += rect_count
        self._diag_capture_pixels += covered
        try:
            png_bytes = os.stat(path).st_size
        except OSError:
            png_bytes = None
        diagnostics.info(
            "gui.vnc.capture.finished", connection_id=self.connection_id,
            width=self.width, height=self.height,
            expected_pixels=total_pixels, received_pixels=covered,
            rectangle_count=rect_count, update_count=update_count,
            message_count=message_count, wire_pixels=wire_pixels,
            auxiliary_bytes=auxiliary_bytes,
            coverage_complete=covered == total_pixels,
            png_bytes=png_bytes,
            duration_ms=round((time.monotonic() - started) * 1000, 1))
        return path

    def _blit(self, buf, data, x, y, w, h, nbytes, offs):
        """Copy a raw true-colour rectangle into the RGB framebuffer, row by
        row via C-level slicing (a per-pixel Python loop over 1080p is too
        slow). offs = (r,g,b) byte offset within each nbytes-wide pixel."""
        ro, go, bo = offs
        line = w * nbytes
        for row in range(h):
            seg = data[row * line:(row + 1) * line]
            rowrgb = bytearray(w * 3)
            rowrgb[0::3] = seg[ro::nbytes]
            rowrgb[1::3] = seg[go::nbytes]
            rowrgb[2::3] = seg[bo::nbytes]
            dst = ((y + row) * self.width + x) * 3
            buf[dst:dst + w * 3] = rowrgb

    def pointer(self, x, y, button_mask=0):
        # RFB §7.5.5 PointerEvent: type=5, button-mask, x, y
        x, y, button_mask = int(x), int(y), int(button_mask)
        if not (0 <= x < self.width and 0 <= y < self.height):
            raise ValueError("VNC pointer coordinates are outside the screen")
        if not 0 <= button_mask <= 0xFF:
            raise ValueError("invalid VNC pointer button mask")
        self._send(struct.pack(">BBHH", 5, button_mask, x, y))
        self._diag_pointer_events += 1

    def click(self, x, y, button=1, hold=0.05):
        button = int(button)
        if not 1 <= button <= 8:
            raise ValueError("VNC button must be between 1 and 8")
        mask = 1 << (button - 1)
        self.pointer(x, y, 0)
        pressed = False
        try:
            self.pointer(x, y, mask)
            pressed = True
            time.sleep(hold)
        finally:
            if pressed:
                self.pointer(x, y, 0)


def combo_to_events(combo):
    """'cmd+s' / 'cmd+shift+4' / 'return' -> (modifier keysyms, key keysym).
    Shared by the GUI driver and tests."""
    import os
    parts = [p.strip().lower() for p in combo.split("+") if p.strip()]
    if not parts:
        raise ValueError("empty key combo")
    *mods, key = parts
    mod_syms = []
    for m in mods:
        if m not in MODIFIER_KEYSYMS:
            raise ValueError("unknown modifier %r" % m)
        sym = MODIFIER_KEYSYMS[m]
        if m in ("cmd", "command"):
            sym = int(os.environ.get("ECHOECHO_VNC_CMD_KEYSYM", sym))
        mod_syms.append(sym)
    if key in KEYSYMS:
        key_sym = KEYSYMS[key]
    elif len(key) == 1 and 0x20 <= ord(key) <= 0x7E:
        key_sym = ord(key)
    else:
        raise ValueError("unknown key %r" % key)
    return mod_syms, key_sym
