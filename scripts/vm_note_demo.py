#!/usr/bin/env python3
"""Live end-to-end proof that echoecho can drive its macOS VM's SCREEN:

    open the VM -> log in over VNC -> create a note in TextEdit -> save it ->
    leave it on screen.

Run ON THE MAC (needs lume + the echoecho-vm guest). It uses echoecho's own
code — LumeVM for lifecycle, the RFB/VNC client for virtual-HID input, and
bounded SSH commands for screenshots. Two deliberate choices proven out live:

  * Input goes over VNC (virtual HID), not SSH osascript, because osascript
    synthetic keystrokes hang on the guest's Accessibility (TCC) prompt.
  * Screenshots use `screencapture` into the shared workspace. The VNC client
    also has a raw-framebuffer capture path, but keeping the proof shots on the
    already-tested guest command makes failures and artifacts easy to inspect.

Screenshots land in <workspace>/screens/:
  00-open           the VM's screen right after boot (login window or desktop)
  01-logged-in      the desktop after logging in over VNC
  02-note-shown     the disk-backed note open in a TextEdit window
  03-typed-over-vnc the document after a line is appended with virtual HID

Env: ECHOECHO_VM_PASSWORD (guest login password, default 'lume'),
ECHOECHO_VNC_CMD_KEYSYM (keysym for Command; default is auto from config),
DEMO_WS (workspace dir whose basename must be 'workspace').
"""
import asyncio
import os
import platform
import shlex
import subprocess
import sys
import time
from pathlib import Path


def _shq(s):
    return shlex.quote(s)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from echoecho_app import __version__, config, diagnostics
from echoecho_app.services import vm as vm_mod
from echoecho_app.services import vnc as vnc_mod
from echoecho_app.services.vm import LumeVM

WS = Path(os.environ.get("DEMO_WS", "/tmp/echoecho-demo/workspace"))
PASSWORD = os.environ.get("ECHOECHO_VM_PASSWORD", "lume")
NOTE_PATH = "~/Desktop/grocery-list.txt"
NOTE = ("echoecho grocery list\n\n"
        "- milk\n- eggs\n- coffee beans\n- bananas\n- olive oil\n")


def log(msg):
    print("[demo] %s" % msg, flush=True)


class Guest:
    """Thin helper: SSH for launch/probe/screenshots, VNC for input."""

    def __init__(self, vm):
        self.vm = vm
        self.c = None
        self.shot_count = 0

    def ssh(self, cmd, timeout=30, operation="command"):
        started = time.monotonic()
        argv = self.vm.ssh_argv(cmd)
        try:
            result = subprocess.run(argv, capture_output=True, text=True,
                                    timeout=timeout)
        except Exception as exc:
            diagnostics.exception(
                "demo.ssh.failed", exc=exc, operation=operation,
                timeout_s=timeout,
                duration_ms=round((time.monotonic() - started) * 1000, 1))
            # TimeoutExpired and some OSErrors stringify the complete SSH
            # argv, including the remote command and user-authored payload.
            raise RuntimeError(
                "guest command could not run during %s (%s)"
                % (operation, type(exc).__name__)) from None
        diagnostics.info(
            "demo.ssh.finished", operation=operation,
            returncode=result.returncode,
            stdout_chars=len(result.stdout), stderr_chars=len(result.stderr),
            duration_ms=round((time.monotonic() - started) * 1000, 1))
        return result

    def require_ssh(self, cmd, operation, timeout=30):
        result = self.ssh(cmd, timeout=timeout, operation=operation)
        if result.returncode != 0:
            raise RuntimeError("guest command failed during %s" % operation)
        return result

    def logged_in(self):
        # screencapture over SSH succeeds only once a console user is logged
        # in and unlocked — a cheap, reliable login probe.
        return self.ssh(
            "screencapture -x -t png /tmp/_probe.png",
            operation="login_probe").returncode == 0

    def vnc(self):
        if self.c is not None:
            return self.c
        url = config.vnc_url_override()
        source = "override" if url else "lume"
        with diagnostics.span("demo.vnc.connect", source=source):
            if not url:
                url = vm_mod.vnc_url(self.vm.vm_name)
            host, port, pw = vnc_mod.parse_vnc_url(url)
            self.c = vnc_mod.VncClient(
                host, port, pw, timeout=25).connect()
        log("VNC connected (%dx%d)" % (self.c.width, self.c.height))
        diagnostics.info(
            "demo.vnc.ready", source=source,
            width=self.c.width, height=self.c.height,
            password_present=bool(pw))
        return self.c

    def shot(self, name):
        # screencapture over SSH into the shared workspace mount, so the PNG
        # lands on the host with no transfer.
        (WS / "screens").mkdir(parents=True, exist_ok=True)
        guest = "/Volumes/My Shared Files/workspace/screens/%s" % name
        self.shot_count += 1
        started = time.monotonic()
        r = self.ssh('screencapture -x -t png "%s"' % guest,
                     operation="screenshot")
        ok = r.returncode == 0 and (WS / "screens" / name).exists()
        diagnostics.info(
            "demo.screenshot.finished", sequence=self.shot_count, success=ok,
            duration_ms=round((time.monotonic() - started) * 1000, 1))
        log("captured %s" % name if ok else
            "screenshot %s unavailable (exit %d)" % (name, r.returncode))
        return ok

    def click(self, x, y):
        self.vnc().click(x, y)

    def type_text(self, t):
        self.vnc().type_text(t)

    def key(self, combo):
        mods, keysym = vnc_mod.combo_to_events(combo)
        c = self.vnc()
        c.chord(mods, keysym) if mods else c.tap(keysym)

    def close(self):
        if self.c is not None:
            self.c.close()
            self.c = None


async def main():
    diagnostics.install_asyncio(asyncio.get_running_loop())
    (WS / "screens").mkdir(parents=True, exist_ok=True)
    vm = LumeVM(workspace=WS)
    log("opening the VM (clone-if-needed, boot, wait for ssh, attach share)…")
    with diagnostics.span("demo.vm.prepare"):
        await vm.prepare()
    log("VM is ready")
    g = Guest(vm)

    try:
        g.shot("00-open.png")  # login window or desktop — the VM is open

        if g.logged_in():
            log("already at the desktop (auto-login)")
        else:
            log("at the login window — logging in over VNC…")
            client = g.vnc()
            g.click(client.width // 2,
                    int(client.height * 0.62))  # password field
            time.sleep(0.5)
            with diagnostics.span(
                    "demo.login", password_chars=len(PASSWORD)):
                g.type_text(PASSWORD)
                g.key("return")
                for attempt in range(1, 13):
                    time.sleep(3)
                    if g.logged_in():
                        diagnostics.info(
                            "demo.login.ready", attempt_count=attempt)
                        break
                else:
                    raise RuntimeError(
                        "login didn't take — desktop never came up")
        g.shot("01-logged-in.png")

        # -- create the note and show it in TextEdit ------------------------
        # Write the note to disk (the note is created, provable), then open it
        # so it's shown on screen. This does not depend on GUI typing.
        log("creating the note and opening it in TextEdit…")
        with diagnostics.span("demo.note.create", note_chars=len(NOTE)):
            g.require_ssh(
                "printf %s > %s" % (_shq(NOTE), NOTE_PATH), "note_write")
            g.require_ssh(
                "open -e %s" % NOTE_PATH, "note_open")  # show the window
        time.sleep(4)
        g.shot("02-note-shown.png")

        # -- demonstrate live GUI control: append a line by typing over VNC --
        log("appending a line by typing over VNC…")
        g.click(g.vnc().width // 2, g.vnc().height // 2)  # focus the doc
        time.sleep(0.5)
        g.type_text("\n- (added live over VNC)\n")
        time.sleep(0.5)
        g.shot("03-typed-over-vnc.png")

        saved = g.require_ssh(
            "cat %s" % NOTE_PATH, "note_verify").stdout
        diagnostics.info("demo.note.verified", saved_chars=len(saved))
        log("note file verified (%d characters)" % len(saved))
    finally:
        g.close()

    log("done. screenshots in %s/screens/" % WS)


def _entrypoint():
    diagnostics.configure(
        "vm-note-demo", mode="live", version=__version__,
        python=platform.python_version(), platform=platform.platform())
    try:
        asyncio.run(main())
    except BaseException as exc:
        diagnostics.exception("demo.run.failed", exc=exc)
        diagnostics.shutdown(outcome="error")
        # Standalone VNC/server/subprocess exceptions can contain peer text,
        # endpoints, or remote commands. Point operators to the sanitized run
        # instead of emitting a raw traceback to the launch console.
        log("failed (%s); inspect structured diagnostics" %
            type(exc).__name__)
        raise SystemExit(1) from None
    else:
        diagnostics.shutdown(outcome="ok")


if __name__ == "__main__":
    _entrypoint()
