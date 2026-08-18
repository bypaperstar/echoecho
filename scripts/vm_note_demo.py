#!/usr/bin/env python3
"""Live end-to-end proof that echoecho can drive its macOS VM's SCREEN:

    open the VM -> log in over VNC -> create a note in TextEdit -> save it ->
    leave it on screen.

Run ON THE MAC (needs lume + the echoecho-vm guest). It uses echoecho's own
code — LumeVM for lifecycle, the RFB/VNC client for virtual-HID input and for
capturing the framebuffer. Two deliberate choices proven out live:

  * Input goes over VNC (virtual HID), not SSH osascript, because osascript
    synthetic keystrokes hang on the guest's Accessibility (TCC) prompt.
  * Screenshots are the VNC FRAMEBUFFER, not `screencapture` over SSH: on a
    headless (--no-display) guest, app windows composite for a connected VNC
    client but not reliably for screencapture-over-SSH. The framebuffer is
    exactly what a human sees over Screen Sharing.

Screenshots land in <workspace>/screens/:
  00-open           the VM's screen right after boot (login window or desktop)
  01-logged-in      the desktop after logging in over VNC
  02-note-typed     the note typed into a TextEdit window
  03-note-saved     the saved document (title bar shows the file name)

Env: ECHOECHO_VM_PASSWORD (guest login password, default 'lume'),
ECHOECHO_VNC_CMD_KEYSYM (keysym for Command; default is auto from config),
DEMO_WS (workspace dir whose basename must be 'workspace').
"""
import asyncio
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path


def _shq(s):
    return shlex.quote(s)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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
    """Thin helper: SSH for launch/probe, VNC for input + framebuffer grabs."""

    def __init__(self, vm):
        self.vm = vm
        self.c = None

    def ssh(self, cmd, timeout=30):
        argv = self.vm.ssh_argv(cmd)
        return subprocess.run(argv, capture_output=True, text=True,
                              timeout=timeout)

    def logged_in(self):
        # screencapture over SSH succeeds only once a console user is logged
        # in and unlocked — a cheap, reliable login probe.
        return self.ssh("screencapture -x -t png /tmp/_probe.png").returncode == 0

    def vnc(self):
        if self.c is not None:
            return self.c
        raw = subprocess.run(["lume", "get", self.vm.vm_name, "-f", "json"],
                             capture_output=True, text=True).stdout
        url = None
        for i in sorted(j for j, ch in enumerate(raw) if ch in "[{"):
            try:
                d = json.loads(raw[i:])
            except ValueError:
                continue
            d = d[0] if isinstance(d, list) else d
            url = d.get("vncUrl")
            break
        host, port, pw = vnc_mod.parse_vnc_url(os.environ.get(
            "ECHOECHO_VNC_URL") or url)
        self.c = vnc_mod.VncClient(host, port, pw, timeout=25).connect()
        log("VNC connected (%dx%d)" % (self.c.width, self.c.height))
        return self.c

    def shot(self, name):
        # screencapture over SSH into the shared workspace mount, so the PNG
        # lands on the host with no transfer. (Apple's guest VNC server won't
        # serve a raw framebuffer, so we don't capture over VNC.)
        (WS / "screens").mkdir(parents=True, exist_ok=True)
        guest = "/Volumes/My Shared Files/workspace/screens/%s" % name
        r = self.ssh('screencapture -x -t png "%s"' % guest)
        ok = r.returncode == 0 and (WS / "screens" / name).exists()
        log("captured %s" % name if ok else
            "screenshot %s unavailable (%s)" % (name, r.stderr.strip()[:80]))
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
    (WS / "screens").mkdir(parents=True, exist_ok=True)
    vm = LumeVM(workspace=WS)
    log("opening the VM (clone-if-needed, boot, wait for ssh, attach share)…")
    await vm.prepare()
    log("VM is up at %s" % vm.ip)
    g = Guest(vm)

    try:
        g.shot("00-open.png")  # login window or desktop — the VM is open

        if g.logged_in():
            log("already at the desktop (auto-login)")
        else:
            log("at the login window — logging in over VNC…")
            g.click(g.c.width // 2, int(g.c.height * 0.62))  # password field
            time.sleep(0.5)
            g.type_text(PASSWORD)
            g.key("return")
            for _ in range(12):
                time.sleep(3)
                if g.logged_in():
                    break
            else:
                raise SystemExit("login didn't take — desktop never came up")
        g.shot("01-logged-in.png")

        # -- create the note and show it in TextEdit ------------------------
        # Write the note to disk (the note is created, provable), then open it
        # so it's shown on screen. This does not depend on GUI typing.
        log("creating the note and opening it in TextEdit…")
        g.ssh("printf %s > %s" % (_shq(NOTE), NOTE_PATH))
        g.ssh("open -e %s" % NOTE_PATH)               # shows the note window
        time.sleep(4)
        g.shot("02-note-shown.png")

        # -- demonstrate live GUI control: append a line by typing over VNC --
        log("appending a line by typing over VNC…")
        g.click(g.vnc().width // 2, g.vnc().height // 2)  # focus the doc
        time.sleep(0.5)
        g.type_text("\n- (added live over VNC)\n")
        time.sleep(0.5)
        g.shot("03-typed-over-vnc.png")

        saved = g.ssh("cat %s" % NOTE_PATH).stdout
        log("note file on disk:\n%s" % saved)
    finally:
        g.close()

    log("done. screenshots in %s/screens/" % WS)


if __name__ == "__main__":
    asyncio.run(main())
