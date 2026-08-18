"""The dependency-free RFB/VNC client (services/vnc.py): DES against the FIPS
vector, the VNC challenge-response quirk, vnc:// parsing, combo->keysym, and a
full handshake + input-event exchange against a fake in-process RFB server.
No real VM, no network beyond a localhost socket."""
import socket
import struct
import threading
import time

import pytest

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
                conn.sendall(struct.pack(">HH", 2, 2) + b"\x00" * 16
                             + struct.pack(">I", len(name)) + name)
                self._recvn(conn, 20)  # SetPixelFormat (4 + 16)
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


def test_client_auth_failure_raises():
    server = FakeRfbServer(password="right").start()
    try:
        with pytest.raises(VncError):
            VncClient("127.0.0.1", server.port, "wrong", timeout=5).connect()
    finally:
        server.stop()
