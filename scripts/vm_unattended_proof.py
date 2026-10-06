#!/usr/bin/env python3
"""Prove Echoecho can use its VM desktop without entering a login password.

Run on the Mac. Capture the actual VNC display (including login/lock screens),
open a new empty text file in TextEdit, type a unique marker through Echoecho's
VNC input driver, save with Command-S, and independently read the file over SSH.
No guest login or unlock password is entered. VNC transport authentication and
Echoecho's SSH key are used as usual.

Use a preserved clone for a before/after comparison. --stop-first cold-boots
that VM; it never deletes a VM. Results and screenshots go in --output.
"""
import argparse
import asyncio
import json
import plistlib
import shlex
import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from echoecho_app import config
from echoecho_app.services import vnc as vnc_mod
from echoecho_app.services.vm import LumeVM, vnc_url


def guest_command(vm, command, timeout=20):
    return subprocess.run(vm.ssh_argv(command), capture_output=True,
                          timeout=timeout)


def console_state(vm):
    user = guest_command(vm, "stat -f %Su /dev/console")
    if user.returncode:
        raise RuntimeError("cannot inspect the guest console")
    username = user.stdout.decode().strip()
    lock_flags = []
    registry = guest_command(vm, "/usr/sbin/ioreg -a -n Root -d 1")
    if registry.returncode == 0:
        roots = plistlib.loads(registry.stdout)
        if isinstance(roots, dict):
            roots = [roots]
        for root in roots:
            if isinstance(root.get("IOConsoleLocked"), bool):
                lock_flags.append(root["IOConsoleLocked"])
            for session in root.get("IOConsoleUsers", []):
                if session.get("kCGSSessionOnConsoleKey"):
                    flag = session.get("CGSSessionScreenIsLocked")
                    if isinstance(flag, bool):
                        lock_flags.append(flag)
    locked = any(lock_flags) if lock_flags else None
    return {"user": username, "locked": locked,
            "logged_in": username not in ("", "root", "loginwindow",
                                           "_mbsetupuser")}


def send_key(client, combo):
    modifiers, key = vnc_mod.combo_to_events(combo)
    if modifiers:
        client.chord(modifiers, key)
    else:
        client.tap(key)


def connect_display(vm):
    host, port, password = vnc_mod.parse_vnc_url(vnc_url(vm.vm_name))
    return vnc_mod.VncClient(host, port, password, timeout=20).connect()


def capture_screen(vm, client, path, timeout=30):
    client.capture_png(path, timeout=timeout)
    return client


async def run(args):
    started = time.monotonic()
    args.output.mkdir(parents=True, exist_ok=True)
    args.workspace.mkdir(parents=True, exist_ok=True)
    vm = LumeVM(vm_name=args.vm, golden=args.golden,
                workspace=args.workspace)
    client = None
    marker = "Echoecho unattended proof %s %s" % (
        args.phase, uuid.uuid4().hex[:12])
    name = "unattended-%s-%s.txt" % (args.phase, uuid.uuid4().hex[:8])
    result = {"phase": args.phase, "vm": args.vm, "passed": False,
              "login_password_entered": False, "marker": marker,
              "file": str(args.workspace / name), "cold_boot": args.stop_first,
              "screensaver_requested": args.screensaver,
              "screensaver_triggered": False}
    try:
        print("proof: preparing %s" % vm.vm_name, flush=True)
        if args.stop_first and vm._is_running(await vm._get()):
            rc, _ = await vm._lume("stop", vm.vm_name)
            if rc:
                raise RuntimeError("could not stop the proof VM")
        await vm.prepare()
        print("proof: SSH and shared workspace ready", flush=True)
        # SSH can become available before a normal automatic login finishes.
        deadline = time.monotonic() + args.desktop_timeout
        while True:
            state = console_state(vm)
            if state["logged_in"] and state["locked"] is not True:
                break
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(2)
        result["console"] = state
        print("proof: console %s" % json.dumps(state), flush=True)
        client = connect_display(vm)
        if args.screensaver and state["logged_in"] and state["locked"] is not True:
            launched = guest_command(vm, "open -a ScreenSaverEngine")
            if launched.returncode:
                raise RuntimeError("could not start the guest screensaver")
            result["screensaver_triggered"] = True
            await asyncio.sleep(5)
            send_key(client, "escape")
            await asyncio.sleep(2)
            state = console_state(vm)
            result["console"] = state
        if state["locked"] is True:
            # Wake an idle display so the proof shows the unlock screen,
            # without putting a character into its password field.
            send_key(client, "return")
            await asyncio.sleep(2)
            state = console_state(vm)
            result["console"] = state
        print("proof: capturing boot screen", flush=True)
        client = capture_screen(vm, client, args.output / "01-boot.png")
        result["boot_screenshot"] = str(args.output / "01-boot.png")
        guest_file = config.vm_guest_workspace().rstrip("/") + "/" + name
        # Only create an EMPTY file via SSH. The proof text must arrive by GUI.
        empty = guest_command(vm, "umask 077; : > %s" % shlex.quote(guest_file))
        if empty.returncode:
            raise RuntimeError("could not create the empty proof file")
        opened = guest_command(
            vm, "open -n -F -a TextEdit %s" % shlex.quote(guest_file))
        print("proof: TextEdit open exit %d" % opened.returncode, flush=True)
        result["textedit_open_exit_code"] = opened.returncode
        if not state["logged_in"] or state["locked"] is True:
            raise RuntimeError("desktop requires login or unlock")
        if opened.returncode:
            raise RuntimeError("TextEdit could not open the proof file")
        await asyncio.sleep(4)
        # A fresh TextEdit instance avoids restored documents from an earlier
        # session taking focus. Click its empty document before typing.
        client.click(client.width // 5, client.height // 5)
        client.type_text(marker + "\n")
        send_key(client, "cmd+s")
        deadline = time.monotonic() + 15
        while True:
            saved = guest_command(vm, "cat %s" % shlex.quote(guest_file))
            if saved.returncode == 0 and saved.stdout.decode() == marker + "\n":
                result["saved_text"] = saved.stdout.decode()
                break
            if time.monotonic() >= deadline:
                raise RuntimeError("GUI-typed text did not reach the saved file")
            await asyncio.sleep(1)
        client = capture_screen(vm, client, args.output / "02-textedit-saved.png")
        result["saved_screenshot"] = str(args.output / "02-textedit-saved.png")
        result["passed"] = True
    except Exception as exc:
        # Do not print arbitrary exceptions: VNC errors may contain peer data.
        result["error"] = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
        if client is not None:
            try:
                client = capture_screen(vm, client, args.output / "02-failure.png", timeout=15)
                result["failure_screenshot"] = str(args.output / "02-failure.png")
            except Exception:
                pass
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                result["close_error"] = True
        result["duration_seconds"] = round(time.monotonic() - started, 1)
        (args.output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2), flush=True)
    return 0 if result["passed"] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vm", default=config.vm_name())
    parser.add_argument("--golden", default=config.vm_golden())
    parser.add_argument("--workspace", type=Path, required=True,
                        help="shared directory, with basename 'workspace'")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--phase", default="check", choices=("before", "after", "reboot", "check"))
    parser.add_argument("--stop-first", action="store_true")
    parser.add_argument("--screensaver", action="store_true",
                        help="start the guest screensaver, then wake it without a password")
    parser.add_argument("--desktop-timeout", type=float, default=45)
    args = parser.parse_args()
    if args.workspace.name != "workspace":
        parser.error("--workspace must have basename 'workspace'")
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
