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
None and VNC-DES), then KeyEvent / PointerEvent (RFB §7.5.4/§7.5.5). No
framebuffer decoding — screenshots still come from `screencapture` over SSH,
which is simpler and higher fidelity than parsing raw rectangles. Everything is
blocking socket I/O wrapped by the async driver in a thread, keeping this file
a plain, unit-testable protocol implementation with no event loop.

Python 3.9, stdlib only (the in-process no-new-deps rule): the VNC challenge
uses DES, which the stdlib lacks, so a compact DES lives here too.
"""
import binascii
import socket
import struct
import time
import zlib
from urllib.parse import urlparse

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

def _write_png(path, width, height, rgb):
    """Write a truecolour PNG from packed RGB bytes — stdlib only (zlib), so
    no Pillow dependency (the in-process no-new-deps rule)."""
    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", binascii.crc32(tag + data) & 0xFFFFFFFF))
    stride = width * 3
    raw = bytearray()
    for y in range(height):
        raw.append(0)  # filter type 0 (None) per scanline
        raw += rgb[y * stride:(y + 1) * stride]
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8-bit RGB
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        f.write(chunk(b"IHDR", ihdr))
        f.write(chunk(b"IDAT", zlib.compress(bytes(raw), 6)))
        f.write(chunk(b"IEND", b""))
    return path


def parse_vnc_url(url):
    """(host, port, password) from a vnc://[:password@]host:port URL. Lume
    reports the guest's built-in server this way (services/vm.vnc_url())."""
    parsed = urlparse(url)
    if parsed.scheme != "vnc":
        raise VncError("not a vnc:// url: %r" % url)
    host = parsed.hostname
    port = parsed.port or 5900
    password = parsed.password or ""
    if not host:
        raise VncError("vnc url has no host: %r" % url)
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

    # -- lifecycle --
    def connect(self):
        self.sock = socket.create_connection((self.host, self.port),
                                              timeout=self.timeout)
        self.sock.settimeout(self.timeout)
        try:
            self._handshake()
        except Exception:
            self.close()
            raise
        return self

    def close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            finally:
                self.sock = None

    def __enter__(self):
        return self.connect()

    def __exit__(self, *exc):
        self.close()

    # -- socket helpers --
    def _recv(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise VncError("VNC server closed the connection mid-message")
            buf += chunk
        return buf

    def _send(self, data):
        self.sock.sendall(data)

    # -- handshake (RFB 3.3/3.7/3.8) --
    def _handshake(self):
        server_version = self._recv(12)
        if not server_version.startswith(b"RFB "):
            raise VncError("not an RFB server: %r" % server_version)
        try:
            major, minor = (int(server_version[4:7]), int(server_version[8:11]))
        except ValueError:
            major, minor = 3, 8
        # never claim a newer protocol than the server offers
        want = (3, minor if (major, minor) >= (3, 7) else 3)
        self._send(b"RFB %03d.%03d\n" % (3, want[1]))
        self._authenticate(want[1])
        self._client_init()

    def _authenticate(self, minor):
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
            if chosen == 0:
                raise VncError(self._read_fail_reason("connection failed"))
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
                raise VncError(self._read_fail_reason(
                    "VNC authentication failed"))

    def _choose_security(self, types):
        if not self.password and 1 in types:
            return 1
        if 2 in types:
            return 2
        if 1 in types:
            return 1
        raise VncError("no supported VNC security type (offered: %s)"
                       % ", ".join(map(str, types)))

    def _read_fail_reason(self, prefix):
        try:
            n = struct.unpack(">I", self._recv(4))[0]
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
        self.name = self._recv(name_len).decode("utf-8", "replace") if \
            name_len else ""

    # -- input events --
    def key_event(self, keysym, down):
        # RFB §7.5.4 KeyEvent: type=4, down-flag, padding, keysym
        self._send(struct.pack(">BBHI", 4, 1 if down else 0, 0, keysym))

    def tap(self, keysym, hold=0.02):
        self.key_event(keysym, True)
        time.sleep(hold)
        self.key_event(keysym, False)
        time.sleep(hold)

    def chord(self, modifiers, keysym, hold=0.03):
        """Press modifier keysyms, tap the key, release modifiers (reverse)."""
        for m in modifiers:
            self.key_event(m, True)
        time.sleep(hold)
        self.key_event(keysym, True)
        time.sleep(hold)
        self.key_event(keysym, False)
        time.sleep(hold)
        for m in reversed(modifiers):
            self.key_event(m, False)
        time.sleep(hold)

    def type_text(self, text, delay=0.03):
        for ch in text:
            if ch == "\n":
                self.tap(KEYSYMS["return"])
            elif ch == "\t":
                self.tap(KEYSYMS["tab"])
            elif 0x20 <= ord(ch) <= 0x7E:
                keysym = ord(ch)  # printable ASCII keysym == code point
                shift = ch.isupper() or ch in '~!@#$%^&*()_+{}|:"<>?'
                if shift:
                    self.chord([MODIFIER_KEYSYMS["shift"]], keysym)
                else:
                    self.tap(keysym)
            else:
                # keysyms for Latin-1 also equal the code point; anything above
                # is out of this minimal driver's scope
                cp = ord(ch)
                if cp <= 0xFF:
                    self.tap(cp)
            time.sleep(delay)

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
        return byte_of(pf["rshift"]), byte_of(pf["gshift"]), byte_of(pf["bshift"])

    def capture_png(self, path, timeout=None):
        """Request the whole framebuffer (raw encoding) and write it to a PNG.
        Blocks until one FramebufferUpdate covering the screen arrives."""
        if timeout:
            self.sock.settimeout(timeout)
        bpp = self.pixfmt["bpp"]
        nbytes = bpp // 8
        offs = self._channel_bytes(bpp)
        if offs is None:
            raise VncError(
                "unsupported native pixel format for capture: %r" % self.pixfmt)
        self.set_encodings([0])  # raw only — no decoder zoo to maintain
        self._fb_update_request(0)
        buf = bytearray(self.width * self.height * 3)
        got = 0
        while got == 0:
            msg_type = self._recv(1)[0]
            if msg_type == 0:  # FramebufferUpdate
                self._recv(1)  # padding
                nrects = struct.unpack(">H", self._recv(2))[0]
                for _ in range(nrects):
                    x, y, w, h, enc = struct.unpack(">HHHHi", self._recv(12))
                    if enc != 0:
                        raise VncError("server used non-raw encoding %d" % enc)
                    data = self._recv(w * h * nbytes)
                    self._blit(buf, data, x, y, w, h, nbytes, offs)
                    got += w * h
            elif msg_type == 1:  # SetColourMapEntries
                self._recv(5)
                n = struct.unpack(">H", self._recv(2))[0]
                self._recv(n * 6)
            elif msg_type == 2:  # Bell
                pass
            elif msg_type == 3:  # ServerCutText
                self._recv(3)
                n = struct.unpack(">I", self._recv(4))[0]
                self._recv(n)
            else:
                raise VncError("unexpected server message %d" % msg_type)
        _write_png(path, self.width, self.height, bytes(buf))
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
        self._send(struct.pack(">BBHH", 5, button_mask & 0xFF, x, y))

    def click(self, x, y, button=1, hold=0.05):
        mask = 1 << (button - 1)
        self.pointer(x, y, 0)
        self.pointer(x, y, mask)
        time.sleep(hold)
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
