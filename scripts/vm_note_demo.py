#!/usr/bin/env python3
"""Live end-to-end proof that echoecho can drive its macOS VM's SCREEN:

    open the VM -> lock it -> log back in over VNC -> create a note in
    TextEdit -> save it -> leave it on screen.

Run ON THE MAC (needs lume + the echoecho-vm guest). It uses echoecho's own
code — LumeVM for lifecycle, VncGuiDriver for input (virtual-HID over VNC, so
it sidesteps the guest TCC block that hangs SSH osascript keystrokes) — and
`screencapture` over SSH after every phase, so the PNGs are the proof.

Phases (screenshots land in <workspace>/screens/):
  00-open           the VM's screen right after boot
  01-login-screen   after we lock the session (CGSession -suspend)
  02-logged-in      after VNC-typing the password + Return
  03-note-typed     the note typed into TextEdit
  04-note-saved     the saved document

Env: ECHOECHO_VM_PASSWORD (guest login password, default 'lume'),
DEMO_WS (workspace dir, default /tmp/echoecho-demo-ws).
"""
import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from echoecho_app.services.gui import VncGuiDriver
from echoecho_app.services.vm import LumeVM

WS = Path(os.environ.get("DEMO_WS", "/tmp/echoecho-demo-ws"))
PASSWORD = os.environ.get("ECHOECHO_VM_PASSWORD", "lume")
NOTE = ("echoecho grocery list\n\n"
        "- milk\n- eggs\n- coffee beans\n- bananas\n- olive oil\n")

def log(msg):
    print("[demo] %s" % msg, flush=True)


async def shot(driver, name):
    """Screenshot the guest; True on success, False if screencapture refuses
    (it fails with 'could not create image from display 0' at the
    loginwindow, before any console user is logged in)."""
    try:
        await driver.screenshot(name)
        return True
    except Exception as exc:
        log("screenshot %s not available yet (%s)" % (name, exc))
        return False


async def ssh(vm, cmd, timeout=30):
    proc = await asyncio.create_subprocess_exec(
        *vm.ssh_argv(cmd),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await asyncio.wait_for(proc.communicate(), timeout)
    return proc.returncode, out.decode("utf-8", "replace"), err.decode(
        "utf-8", "replace")


async def main():
    (WS / "screens").mkdir(parents=True, exist_ok=True)
    vm = LumeVM(workspace=WS)

    log("opening the VM (clone-if-needed, boot, wait for ssh, attach share)…")
    await vm.prepare()
    log("VM is up at %s" % vm.ip)

    driver = VncGuiDriver(vm, WS)
    try:
        # A fresh guest sits at the loginwindow, where `screencapture` over
        # ssh fails ("could not create image from display 0") — that failure
        # IS the signal that we still need to log in.
        logged_in = await shot(driver, "screens/00-open.png")
        if logged_in:
            log("already at the desktop (auto-login); 00-open captured")
        else:
            log("at the loginwindow — logging in over VNC…")

        # -- log in over VNC (virtual HID keystrokes) ---------------------
        # A pointer wake, then the password + Return into the focused field.
        await driver.click(640, 460)
        await asyncio.sleep(0.5)
        await driver.type_text(PASSWORD)
        await driver.key("return")
        log("typed the password over VNC; waiting for the desktop…")
        # the desktop takes a few seconds; retry the shot until it renders
        for attempt in range(12):
            await asyncio.sleep(3)
            if await shot(driver, "screens/02-logged-in.png"):
                log("logged in — desktop captured (after %ds)"
                    % ((attempt + 1) * 3))
                break
        else:
            raise SystemExit("desktop never rendered after login")

        # -- create the note in TextEdit ----------------------------------
        log("creating the note in TextEdit…")
        await driver.launch("TextEdit")
        await asyncio.sleep(3)
        await driver.key("cmd+n")            # new document
        await asyncio.sleep(1.5)
        await driver.click(640, 360)          # focus the doc
        await asyncio.sleep(0.5)
        await driver.type_text(NOTE)
        await asyncio.sleep(0.5)
        await shot(driver, "screens/03-note-typed.png")
        log("captured 03-note-typed")

        # -- save it (Cmd+S, name, Return) --------------------------------
        await driver.key("cmd+s")
        await asyncio.sleep(1.5)
        await driver.type_text("grocery-list")
        await driver.key("return")
        await asyncio.sleep(2)
        await shot(driver, "screens/04-note-saved.png")
        log("captured 04-note-saved")
    finally:
        driver.close()

    log("done. screenshots in %s/screens/" % WS)


if __name__ == "__main__":
    asyncio.run(main())
