"""The dependency-free RFB/VNC client (services/vnc.py): DES against the FIPS
vector, the VNC challenge-response quirk, vnc:// parsing, combo->keysym, and a
full handshake + input-event exchange against a fake in-process RFB server.
No real VM, no network beyond a localhost socket."""
import asyncio
import json
import socket
import struct
import threading
import time
import zlib

import pytest

from echoecho_app import diagnostics
from echoecho_app.services import vnc as vnc_mod
from echoecho_app.services.vnc import (
    VncClient, VncError, combo_to_events, des_encrypt_block, parse_vnc_url,
    vnc_challenge_response)


# -- DES ---------------------------------------------------------------------

def test_des_fips_vector():
    # Classic single-block DES test vector (FIPS PUB 81).
    key = bytes.fromhex("133457799BBCDFF1")
    plain = bytes.fromhex("0123456789ABCDEF")
    assert des_encrypt_block(key, plain) == bytes.fromhex("85E813540F0AB405")


def test_des_another_vector():
    key = bytes.fromhex("0E329232EA6D0D73")
    plain = bytes.fromhex("8787878787878787")
    assert des_encrypt_block(key, plain) == bytes.fromhex("0000000000000000")


def test_vnc_challenge_response_shape_and_determinism():
    challenge = bytes(range(16))
    resp = vnc_challenge_response("secret", challenge)
    assert len(resp) == 16
    # deterministic, and the two 8-byte halves are independent ECB blocks
    assert resp == vnc_challenge_response("secret", challenge)
    assert resp[:8] == vnc_challenge_response("secret", challenge[:8] * 2)[:8]


def test_vnc_password_truncated_to_eight_bytes():
    challenge = bytes(range(16))
    # only the first 8 password bytes matter to VNC auth
    assert (vnc_challenge_response("papa-swan-sun-whale", challenge)
            == vnc_challenge_response("papa-swa", challenge))


# -- url + combo -------------------------------------------------------------

def test_parse_vnc_url():
    assert parse_vnc_url("vnc://:pw@127.0.0.1:57977") == ("127.0.0.1", 57977, "pw")
    assert parse_vnc_url("vnc://host:5900") == ("host", 5900, "")
    assert parse_vnc_url("vnc://host") == ("host", 5900, "")
    with pytest.raises(VncError):
        parse_vnc_url("http://nope")


@pytest.mark.parametrize("raw", [
    "https://:URL-SECRET-CANARY@example.test:5900",
    "vnc://:URL-SECRET-CANARY@",
    "vnc://:URL-SECRET-CANARY@example.test:not-a-port",
])
def test_parse_vnc_url_errors_never_echo_credentials(raw):
    with pytest.raises(VncError) as caught:
        parse_vnc_url(raw)
    assert "URL-SECRET-CANARY" not in str(caught.value)


def test_combo_to_events():
    mods, key = combo_to_events("cmd+s")
    assert key == ord("s")
    assert mods == [vnc_mod.MODIFIER_KEYSYMS["cmd"]]
    mods, key = combo_to_events("return")
    assert key == vnc_mod.KEYSYMS["return"] and mods == []
    mods, key = combo_to_events("cmd+shift+4")
    assert key == ord("4") and len(mods) == 2
    with pytest.raises(ValueError):
        combo_to_events("meta+x")
    with pytest.raises(ValueError):
        combo_to_events("")


def test_combo_cmd_keysym_override(monkeypatch):
    monkeypatch.setenv("ECHOECHO_VNC_CMD_KEYSYM", str(0xFFEB))
    mods, _ = combo_to_events("cmd+s")
    assert mods == [0xFFEB]


# -- a fake RFB server, end to end -------------------------------------------

class FakeRfbServer:
    """Minimal RFB 3.8 server: version, VNC-auth (type 2), ClientInit, then
    records every input message the client sends."""

    def __init__(self, password="secret"):
        self.password = password
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.events = []
        self.challenge = bytes(range(16))
        self.auth_ok = True
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def _recvn(self, conn, n):
        buf = b""
        while len(buf) < n:
            chunk = conn.recv(n - len(buf))
            if not chunk:
                raise EOFError
            buf += chunk
        return buf

    def _serve(self):
        conn, _ = self.sock.accept()
        try:
            conn.sendall(b"RFB 003.008\n")
            self._recvn(conn, 12)  # client version
            conn.sendall(bytes([1, 2]))  # one security type on offer: VNC auth
            chosen = self._recvn(conn, 1)[0]
            assert chosen == 2
            conn.sendall(self.challenge)
            response = self._recvn(conn, 16)
            self.got_response = response
            expected = vnc_challenge_response(self.password, self.challenge)
            ok = self.auth_ok and response == expected
            conn.sendall(struct.pack(">I", 0 if ok else 1))
            if not ok:
                fail = b"bad password"
                conn.sendall(struct.pack(">I", len(fail)) + fail)
                return
            self._recvn(conn, 1)  # ClientInit shared flag
            name = b"fake"
            conn.sendall(struct.pack(">HH", 800, 600)
                         + b"\x00" * 16
                         + struct.pack(">I", len(name)) + name)
            # record input messages until the client hangs up
            while True:
                msg_type = self._recvn(conn, 1)[0]
                if msg_type == 4:  # KeyEvent
                    down, _, keysym = struct.unpack(">BHI", self._recvn(conn, 7))
                    self.events.append(("key", down, keysym))
                elif msg_type == 5:  # PointerEvent
                    mask, x, y = struct.unpack(">BHH", self._recvn(conn, 5))
                    self.events.append(("pointer", mask, x, y))
                else:
                    break
        except (EOFError, OSError, AssertionError):
            pass
        finally:
            conn.close()

    def stop(self):
        try:
            self.sock.close()
        except OSError:
            pass


class NoneOnlyRfbServer(FakeRfbServer):
    """Offer only unauthenticated access, then wait for the client to close."""

    def _serve(self):
        conn, _ = self.sock.accept()
        try:
            conn.sendall(b"RFB 003.008\n")
            self._recvn(conn, 12)
            conn.sendall(bytes([1, 1]))  # one offered type: None
            # A password-configured client must reject before choosing it.
            conn.settimeout(2)
            self.chosen = conn.recv(1)
        except (EOFError, OSError):
            self.chosen = b""
        finally:
            conn.close()


class OversizedNameRfbServer(FakeRfbServer):
    def _serve(self):
        conn, _ = self.sock.accept()
        try:
            conn.sendall(b"RFB 003.008\n")
            self._recvn(conn, 12)
            conn.sendall(bytes([1, 1]))
            self._recvn(conn, 1)
            conn.sendall(struct.pack(">I", 0))
            self._recvn(conn, 1)
            conn.sendall(
                struct.pack(">HH", 800, 600) + b"\x00" * 16 +
                struct.pack(">I", vnc_mod.MAX_SERVER_NAME_BYTES + 1))
        except (EOFError, OSError):
            pass
        finally:
            conn.close()


class UnknownVersionRfbServer(FakeRfbServer):
    def _serve(self):
        conn, _ = self.sock.accept()
        try:
            conn.sendall(b"RFB 003.889\n")
            self.client_version = self._recvn(conn, 12)
            conn.sendall(struct.pack(">I", 1))  # 3.3 server-selected None
            self._recvn(conn, 1)
            name = b"unknown-version"
            pf = (struct.pack(">BBBBHHHBBB", 32, 24, 0, 1,
                              255, 255, 255, 16, 8, 0) + b"\x00\x00\x00")
            conn.sendall(struct.pack(">HH", 20, 10) + pf
                         + struct.pack(">I", len(name)) + name)
            # Drain both KeyEvent messages (down + up) before closing so the
            # kernel does not reset a client that still has unread bytes.
            self._recvn(conn, 16)
        except (EOFError, OSError):
            pass
        finally:
            conn.close()


def test_client_handshake_and_input():
    server = FakeRfbServer(password="hunter2").start()
    try:
        with VncClient("127.0.0.1", server.port, "hunter2", timeout=5) as c:
            assert (c.width, c.height) == (800, 600)
            assert c.name == "fake"
            c.type_text("Hi")
            c.tap(vnc_mod.KEYSYMS["return"])
            c.chord([vnc_mod.MODIFIER_KEYSYMS["cmd"]], ord("s"))
            c.click(10, 20)
    finally:
        server.stop()
    # allow the server thread to drain
    server.thread.join(timeout=2)
    kinds = [e for e in server.events if e[0] == "key"]
    # 'H' typed with shift held: shift-down, H-down, H-up, shift-up
    downs = [e[2] for e in kinds if e[1] == 1]
    assert ord("H") in downs and vnc_mod.MODIFIER_KEYSYMS["shift"] in downs
    assert ord("i") in downs
    assert vnc_mod.KEYSYMS["return"] in downs
    assert vnc_mod.MODIFIER_KEYSYMS["cmd"] in downs
    pointers = [e for e in server.events if e[0] == "pointer"]
    assert ("pointer", 1, 10, 20) in pointers  # button-1 press at (10,20)


def test_password_client_rejects_unauthenticated_downgrade():
    server = NoneOnlyRfbServer().start()
    try:
        with pytest.raises(VncError, match="password authentication"):
            VncClient("127.0.0.1", server.port,
                      "PASSWORD-DOWNGRADE-CANARY", timeout=2).connect()
    finally:
        server.stop()
        server.thread.join(timeout=2)
    assert getattr(server, "chosen", b"") == b""


def test_server_name_length_is_bounded_before_reading_payload():
    server = OversizedNameRfbServer(password="").start()
    try:
        with pytest.raises(VncError, match="name is too large"):
            VncClient("127.0.0.1", server.port, "", timeout=2).connect()
    finally:
        server.stop()
        server.thread.join(timeout=2)


def test_unknown_protocol_version_falls_back_to_rfb_33():
    server = UnknownVersionRfbServer(password="").start()
    try:
        with VncClient("127.0.0.1", server.port, "", timeout=2) as client:
            assert (client.width, client.height) == (20, 10)
            client.tap(ord("x"), hold=0)
    finally:
        server.stop()
        server.thread.join(timeout=2)
    assert server.client_version == b"RFB 003.003\n"


def test_client_diagnostics_are_metadata_only(tmp_path):
    diag_dir = tmp_path / "diagnostics"
    password = "DIAG-PASSWORD-CANARY"
    typed = "DIAG-TEXT-CANARY"
    server = FakeRfbServer(password=password).start()
    diagnostics.configure("vnc-test", log_dir=diag_dir)
    try:
        with VncClient("127.0.0.1", server.port, password, timeout=2) as client:
            client.type_text(typed, delay=0)
    finally:
        diagnostics.shutdown(outcome="test")
        server.stop()
        server.thread.join(timeout=2)

    raw = "\n".join(
        path.read_text(encoding="utf-8")
        for path in diag_dir.glob("*.jsonl"))
    assert password not in raw
    assert typed not in raw
    records = [json.loads(line) for line in raw.splitlines() if line.strip()]
    events = {record["event"] for record in records}
    assert "gui.vnc.connect.started" in events
    assert "gui.vnc.server.ready" in events
    assert "gui.vnc.input.finished" in events
    closed = next(record for record in records
                  if record["event"] == "gui.vnc.connection.closed")
    assert closed["fields"]["text_chars"] == len(typed)
    assert closed["fields"]["key_event_count"] > 0


def test_vnc_input_bounds_text_coordinates_and_buttons():
    client = VncClient("127.0.0.1", 5900)
    client.width, client.height = 100, 50
    with pytest.raises(ValueError, match="too long"):
        client.type_text("x" * (vnc_mod.MAX_TYPE_CHARS + 1), delay=0)
    with pytest.raises(ValueError, match="outside"):
        client.pointer(100, 2)
    with pytest.raises(ValueError, match="between 1 and 8"):
        client.click(1, 2, button=9)


def test_vnc_gui_driver_selected_by_default(monkeypatch, tmp_path):
    from echoecho_app.orchestrator.core import WorkerContext
    from echoecho_app.services import gui as gui_mod
    from echoecho_app.services.vm import LumeVM
    ctx = WorkerContext(workspace=tmp_path)
    ctx.extra["sandbox"] = LumeVM(vm_name="echoecho-vm")
    monkeypatch.delenv("ECHOECHO_GUI_INPUT", raising=False)
    assert isinstance(gui_mod.for_ctx(ctx), gui_mod.VncGuiDriver)
    monkeypatch.setenv("ECHOECHO_GUI_INPUT", "ssh")
    drv = gui_mod.for_ctx(ctx)
    assert isinstance(drv, gui_mod.SshGuiDriver)
    assert not isinstance(drv, gui_mod.VncGuiDriver)


def test_vnc_gui_driver_routes_input_over_vnc(monkeypatch, tmp_path):
    """launch/screenshot stay on SSH; type/key/click go to the VNC client."""
    import asyncio

    from echoecho_app.services import gui as gui_mod
    from echoecho_app.services.vm import LumeVM

    monkeypatch.setenv("ECHOECHO_VNC_URL", "vnc://:pw@127.0.0.1:5900")
    vm = LumeVM(vm_name="echoecho-vm")
    vm.ip = "10.0.0.9"
    driver = gui_mod.VncGuiDriver(vm, tmp_path)

    calls = []

    class FakeClient:
        def type_text(self, text):
            calls.append(("type", text))

        def tap(self, keysym):
            calls.append(("tap", keysym))

        def chord(self, mods, keysym):
            calls.append(("chord", tuple(mods), keysym))

        def click(self, x, y, button=1):
            calls.append(("click", x, y, button))

        def close(self):
            calls.append(("close",))

    async def fake_vnc():
        return FakeClient()
    driver._vnc = fake_vnc

    ssh_argvs = []

    async def fake_run(argv, capture=False):
        ssh_argvs.append(argv)
        return b""
    driver._run = fake_run

    asyncio.run(driver.launch("TextEdit"))
    asyncio.run(driver.type_text("hi"))
    asyncio.run(driver.key("cmd+s"))
    asyncio.run(driver.click(5, 6))

    assert ("type", "hi") in calls
    assert any(c[0] == "chord" for c in calls)  # cmd+s -> chord
    assert ("click", 5, 6, 1) in calls
    assert ssh_argvs[0] == ["open", "-a", "TextEdit"]  # launch stayed on SSH


def test_vnc_gui_driver_never_echoes_credential_url(monkeypatch, tmp_path):
    from echoecho_app.services import gui as gui_mod
    from echoecho_app.services.vm import LumeVM

    secret = "GUI-URL-SECRET-CANARY"
    monkeypatch.setenv(
        "ECHOECHO_VNC_URL", "vnc://:%s@127.0.0.1:1" % secret)
    driver = gui_mod.VncGuiDriver(
        LumeVM(vm_name="echoecho-vm"), tmp_path)

    with pytest.raises(gui_mod.GuiError) as caught:
        asyncio.run(driver._vnc())

    assert secret not in str(caught.value)
    assert "vnc://" not in str(caught.value)


def test_vnc_gui_driver_discards_cached_client_after_input_failure(tmp_path):
    from echoecho_app.services import gui as gui_mod
    from echoecho_app.services.vm import LumeVM

    class BrokenClient:
        def __init__(self):
            self.closed = False

        def type_text(self, _text):
            raise OSError("wire failed")

        def close(self, outcome="closed"):
            self.closed = True

    driver = gui_mod.VncGuiDriver(
        LumeVM(vm_name="echoecho-vm"), tmp_path)
    client = BrokenClient()
    driver._client = client

    with pytest.raises(gui_mod.GuiError, match="input failed"):
        asyncio.run(driver.type_text("private content"))

    assert client.closed is True
    assert driver._client is None


def test_write_png_roundtrip(tmp_path):
    from echoecho_app.services.vnc import _write_png
    # 2x1 image: red, green -> a valid PNG the stdlib can't decode without
    # zlib inflate, so just check the signature + that IEND is present
    png = _write_png(str(tmp_path / "x.png"), 2, 1,
                     bytes([255, 0, 0, 0, 255, 0]))
    data = open(png, "rb").read()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    assert data[-8:-4] == b"IEND"
    # IHDR width/height are big-endian right after the 8-byte sig + len+tag
    assert struct.unpack(">II", data[16:24]) == (2, 1)
    offset = 8
    idat = []
    while offset < len(data):
        length = struct.unpack(">I", data[offset:offset + 4])[0]
        tag = data[offset + 4:offset + 8]
        payload = data[offset + 8:offset + 8 + length]
        if tag == b"IDAT":
            idat.append(payload)
        offset += 12 + length
    assert zlib.decompress(b"".join(idat)) == bytes(
        [0, 255, 0, 0, 0, 255, 0])


def test_write_png_atomically_replaces_symlink_without_touching_target(tmp_path):
    victim = tmp_path / "private.txt"
    victim.write_text("must survive")
    output = tmp_path / "screen.png"
    output.symlink_to(victim)

    vnc_mod._write_png(str(output), 1, 1, bytes([1, 2, 3]))

    assert victim.read_text() == "must survive"
    assert output.is_file() and not output.is_symlink()
    assert output.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


def test_capture_png_over_fake_server(tmp_path):
    """A fake server that answers a FramebufferUpdateRequest with one raw
    full-screen rect; the client must assemble and write a PNG."""
    class CapServer(FakeRfbServer):
        def _serve(self):
            conn, _ = self.sock.accept()
            try:
                conn.sendall(b"RFB 003.008\n")
                self._recvn(conn, 12)
                conn.sendall(bytes([1, 1]))  # security: None
                self._recvn(conn, 1)
                conn.sendall(struct.pack(">I", 0))  # SecurityResult ok
                self._recvn(conn, 1)  # ClientInit
                name = b"cap"
                # ServerInit: 2x2, native 32bpp BGRx (LE, R<<16 G<<8 B<<0)
                pf = (struct.pack(">BBBBHHHBBB", 32, 24, 0, 1, 255, 255, 255,
                                  16, 8, 0) + b"\x00\x00\x00")
                conn.sendall(struct.pack(">HH", 2, 2) + pf
                             + struct.pack(">I", len(name)) + name)
                # client decodes in the native format now, so NO SetPixelFormat
                # SetEncodings: 4-byte header + n*4
                hdr = self._recvn(conn, 4)
                n = struct.unpack(">H", hdr[2:4])[0]
                self._recvn(conn, n * 4)
                self._recvn(conn, 10)  # FramebufferUpdateRequest
                # one raw rect covering the 2x2 screen; BGRx pixels
                px = bytes([0, 0, 255, 0,   0, 255, 0, 0,     # red, green
                            255, 0, 0, 0,   255, 255, 255, 0])  # blue, white
                msg = struct.pack(">BBH", 0, 0, 1)
                msg += struct.pack(">HHHHi", 0, 0, 2, 2, 0) + px
                conn.sendall(msg)
                time.sleep(0.2)
            except (EOFError, OSError):
                pass
            finally:
                conn.close()

    server = CapServer(password="").start()
    try:
        with VncClient("127.0.0.1", server.port, "", timeout=5) as c:
            out = c.capture_png(str(tmp_path / "screen.png"))
        data = open(out, "rb").read()
        assert data[:8] == b"\x89PNG\r\n\x1a\n"
        assert struct.unpack(">II", data[16:24]) == (2, 2)
    finally:
        server.stop()


def test_capture_rejects_partial_framebuffer_without_writing_png(tmp_path):
    class PartialServer(FakeRfbServer):
        def _serve(self):
            conn, _ = self.sock.accept()
            try:
                conn.sendall(b"RFB 003.008\n")
                self._recvn(conn, 12)
                conn.sendall(bytes([1, 1]))
                self._recvn(conn, 1)
                conn.sendall(struct.pack(">I", 0))
                self._recvn(conn, 1)
                name = b"partial"
                pf = (struct.pack(">BBBBHHHBBB", 32, 24, 0, 1,
                                  255, 255, 255, 16, 8, 0) + b"\x00\x00\x00")
                conn.sendall(struct.pack(">HH", 2, 2) + pf
                             + struct.pack(">I", len(name)) + name)
                hdr = self._recvn(conn, 4)
                self._recvn(conn, struct.unpack(">H", hdr[2:4])[0] * 4)
                self._recvn(conn, 10)
                pixel = bytes([0, 0, 255, 0])
                conn.sendall(
                    struct.pack(">BBH", 0, 0, 1) +
                    struct.pack(">HHHHi", 0, 0, 1, 1, 0) + pixel)
                self._recvn(conn, 10)  # client's bounded full retry
            except (EOFError, OSError):
                pass
            finally:
                conn.close()

    server = PartialServer(password="").start()
    output = tmp_path / "partial.png"
    try:
        with VncClient("127.0.0.1", server.port, "", timeout=2) as client:
            with pytest.raises(VncError, match="closed"):
                client.capture_png(str(output))
    finally:
        server.stop()
        server.thread.join(timeout=2)
    assert not output.exists()


def test_capture_has_total_deadline_for_endless_non_framebuffer_messages(
        tmp_path):
    class BellServer(FakeRfbServer):
        def _serve(self):
            conn, _ = self.sock.accept()
            try:
                conn.sendall(b"RFB 003.008\n")
                self._recvn(conn, 12)
                conn.sendall(bytes([1, 1]))
                self._recvn(conn, 1)
                conn.sendall(struct.pack(">I", 0))
                self._recvn(conn, 1)
                name = b"bell"
                pf = (struct.pack(">BBBBHHHBBB", 32, 24, 0, 1,
                                  255, 255, 255, 16, 8, 0) + b"\x00\x00\x00")
                conn.sendall(struct.pack(">HH", 2, 2) + pf
                             + struct.pack(">I", len(name)) + name)
                hdr = self._recvn(conn, 4)
                self._recvn(conn, struct.unpack(">H", hdr[2:4])[0] * 4)
                self._recvn(conn, 10)
                while True:
                    conn.sendall(b"\x02" * 1024)  # Bell forever
            except (EOFError, OSError):
                pass
            finally:
                conn.close()

    server = BellServer(password="").start()
    output = tmp_path / "never.png"
    started = time.monotonic()
    try:
        with VncClient("127.0.0.1", server.port, "", timeout=2) as client:
            with pytest.raises((TimeoutError, VncError), match=(
                    "deadline|message limit")):
                client.capture_png(str(output), timeout=0.05)
    finally:
        server.stop()
        server.thread.join(timeout=2)
    assert time.monotonic() - started < 1
    assert not output.exists()


def test_capture_deadline_cannot_be_bypassed_by_slow_drip_payload(tmp_path):
    class SlowDripServer(FakeRfbServer):
        def _serve(self):
            conn, _ = self.sock.accept()
            try:
                conn.sendall(b"RFB 003.008\n")
                self._recvn(conn, 12)
                conn.sendall(bytes([1, 1]))
                self._recvn(conn, 1)
                conn.sendall(struct.pack(">I", 0))
                self._recvn(conn, 1)
                name = b"slow-drip"
                pf = (struct.pack(">BBBBHHHBBB", 32, 24, 0, 1,
                                  255, 255, 255, 16, 8, 0) + b"\x00\x00\x00")
                conn.sendall(struct.pack(">HH", 2, 1) + pf
                             + struct.pack(">I", len(name)) + name)
                hdr = self._recvn(conn, 4)
                self._recvn(conn, struct.unpack(">H", hdr[2:4])[0] * 4)
                self._recvn(conn, 10)
                conn.sendall(
                    struct.pack(">BBH", 0, 0, 1) +
                    struct.pack(">HHHHi", 0, 0, 2, 1, 0))
                # Each byte arrives inside the per-read timeout, but the full
                # row cannot fit inside the capture's absolute deadline.
                for byte in bytes([0, 0, 255, 0, 0, 255, 0, 0]):
                    conn.sendall(bytes([byte]))
                    time.sleep(0.04)
            except (EOFError, OSError):
                pass
            finally:
                conn.close()

    server = SlowDripServer(password="").start()
    output = tmp_path / "slow.png"
    started = time.monotonic()
    try:
        with VncClient("127.0.0.1", server.port, "", timeout=2) as client:
            original_timeout = client.sock.gettimeout()
            with pytest.raises((TimeoutError, socket.timeout), match=(
                    "deadline|timed out")):
                client.capture_png(str(output), timeout=0.1)
            assert client.sock.gettimeout() == original_timeout
    finally:
        server.stop()
        server.thread.join(timeout=2)
    assert time.monotonic() - started < 1
    assert not output.exists()


def test_client_auth_failure_raises():
    server = FakeRfbServer(password="right").start()
    try:
        with pytest.raises(VncError):
            VncClient("127.0.0.1", server.port, "wrong", timeout=5).connect()
    finally:
        server.stop()
