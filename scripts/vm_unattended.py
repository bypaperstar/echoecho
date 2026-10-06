#!/usr/bin/env python3
"""Configure Echoecho's running guest for automatic, unlocked desktop access.

Called by vm_golden.sh before freezing the template. Can also repair an
existing running guest with --vm. Settings apply inside that VM only.
Passwords are sent over SSH stdin, never included in process arguments or logs.
"""
import argparse
import asyncio
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from echoecho_app import config
from echoecho_app.services.vm import LumeVM


def guest(vm, command, operation, input_text=None):
    try:
        result = subprocess.run(vm.ssh_argv(command), input=input_text,
                                capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError("guest configuration unavailable during %s" % operation) from None
    if result.returncode:
        raise RuntimeError("guest configuration failed during %s" % operation)
    return result


def configure(vm, username, password):
    # Use the same credential format as Lume's unattended disk setup. The
    # native autologin setter requires a console authorization context and can
    # fail over SSH with exit code zero. Pad BEFORE XOR, including a NUL when
    # the password length is a multiple of 12.
    key = bytes.fromhex("7d895223d2bcddeaa3b91f")
    plaintext = password.encode("utf-8")
    plaintext += bytes(12 - len(plaintext) % 12)
    encoded = bytes(value ^ key[index % len(key)]
                    for index, value in enumerate(plaintext)).hex()
    loginwindow = "/Library/Preferences/com.apple.loginwindow"
    script = ("umask 077; /usr/bin/xxd -r -p > /etc/kcpassword && "
              "/usr/sbin/chown root:wheel /etc/kcpassword && "
              "/bin/chmod 600 /etc/kcpassword && "
              "/usr/bin/defaults write %s autoLoginUser -string %s && "
              "/usr/bin/defaults write %s autoLoginUserScreenLocked -bool false"
              % (loginwindow, shlex.quote(username), loginwindow))
    guest(vm, "sudo -k -S -p '' /bin/sh -c %s" % shlex.quote(script),
          "set unlocked automatic login", password + "\n" + encoded + "\n")
    credential = guest(vm, "sudo -k -S -p '' /usr/bin/xxd -p /etc/kcpassword",
                       "verify automatic login credential", password + "\n")
    if "".join(credential.stdout.split()) != encoded:
        raise RuntimeError("automatic login credentials were not stored correctly")
    current = guest(vm, "defaults read /Library/Preferences/com.apple.loginwindow "
                    "autoLoginUser", "verify automatic login").stdout.strip()
    if current != username:
        raise RuntimeError("automatic login was not configured for the guest account")
    locked = guest(vm, "defaults read %s autoLoginUserScreenLocked" % loginwindow,
                   "verify unlocked automatic login").stdout.strip()
    if locked != "0":
        raise RuntimeError("automatic login still leaves the guest desktop locked")

    # The native policy is separate from autologin: a guest can boot into its
    # account and still demand a password as soon as its screensaver starts.
    guest(vm, "/usr/sbin/sysadminctl -screenLock off -password -",
          "disable screen lock", password + "\n")
    status = guest(vm, "/usr/sbin/sysadminctl -screenLock status",
                   "verify screen lock")
    if not re.search(r"screenLock\b.*\boff\b", status.stdout + status.stderr):
        # sysadminctl can exit zero after refusing a change; inspect its status.
        raise RuntimeError("the guest still requires a password after screen locking")
    # sysadminctl also writes a policy tied to this guest's hardware UUID.
    # Lume gives clones a new UUID, so provide the user-wide fallback too.
    guest(vm, "defaults write com.apple.screensaver askForPassword -int 0",
          "disable screen lock for future clones")
    guest(vm, "defaults write com.apple.screensaver askForPasswordDelay -int 0",
          "set clone screen lock delay")
    guest(vm, "defaults write com.apple.screensaver idleTime -int 0",
          "disable idle screensaver for future clones")
    guest(vm, "defaults -currentHost write com.apple.screensaver idleTime -int 0",
          "disable idle screensaver")
    guest(vm, "sudo -k -S -p '' /usr/bin/pmset -a sleep 0 displaysleep 0 disksleep 0",
          "disable guest sleep", password + "\n")
    power = guest(vm, "/usr/bin/pmset -g custom", "verify guest sleep").stdout
    for timer in ("sleep", "displaysleep", "disksleep"):
        values = re.findall(r"^\s*%s\s+(\d+)\b" % timer, power, re.MULTILINE)
        if not values or any(value != "0" for value in values):
            raise RuntimeError("the guest's %s timer was not disabled" % timer)
    return {"automatic_login_user": current, "screen_lock": "off",
            "sleep": "off", "display_sleep": "off"}


async def run(vm_name):
    vm = LumeVM(vm_name=vm_name)
    vm._require_ssh_key()
    info = await vm._get()
    if not vm._is_running(info):
        raise RuntimeError("start the guest before configuring its desktop")
    vm.ip = info.get("ipAddress") or info.get("ip")
    if not vm.ip:
        raise RuntimeError("the running guest has no SSH address yet")
    return configure(vm, config.vm_guest_user(),
                     os.environ.get("ECHOECHO_VM_PASSWORD", "lume"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vm", default=config.vm_golden())
    args = parser.parse_args()
    try:
        result = asyncio.run(run(args.vm))
    except RuntimeError as exc:
        print("unattended setup failed: %s" % exc, file=sys.stderr)
        raise SystemExit(1) from None
    print("guest desktop configured: automatic login, screen lock off, sleep off")
    return result


if __name__ == "__main__":
    main()
