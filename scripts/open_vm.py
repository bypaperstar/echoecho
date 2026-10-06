#!/usr/bin/env python3
"""Open the same warm VM used by Echoecho's workers in Lume's normal window."""
import asyncio
import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from echoecho_app import config
from echoecho_app.services.vm import LumeVM, SandboxUnavailable, vnc_url


class NativeDisplayVM(LumeVM):
    async def _boot(self):
        # This helper exits after attaching. A live asyncio transport would
        # kill its subprocess on teardown; let Lume own a detached runner.
        rc, _ = await self._lume(*self._boot_argv()[1:], "--detach",
                                "--log-file", str(self._boot_log_path()))
        if rc:
            raise SandboxUnavailable("The shared VM could not start")


async def open_vm(show_native=True):
    config.load_env_local()
    lume = shutil.which("lume")
    if not lume:
        raise RuntimeError("Lume is not installed")
    # Check before preparing: an older Lume must not boot a VM whose native
    # desktop it cannot attach to. Keep the VM identity and shares identical
    # to the daemon; opening a viewer never creates an alternate guest.
    if show_native:
        check = subprocess.run([lume, "attach", "--help"],
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=10)
        if check.returncode:
            raise RuntimeError("Opening the shared VM requires Lume 0.6 or newer")
    vm = NativeDisplayVM(workspace=config.WORKSPACE_DIR)
    await vm.prepare()
    if show_native:
        subprocess.run([lume, "attach", vm.vm_name, "--display", "native"],
                       check=True, timeout=30)
    print("Opened the shared VM" if show_native else "Shared VM ready")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepare-only', action='store_true',
                        help='Prepare the shared guest for the embedded app display')
    parser.add_argument('--connection-info', action='store_true',
                        help='Return private display connection data to the app')
    args = parser.parse_args()
    try:
        if args.connection_info:
            config.load_env_local()
            print(json.dumps({'url': vnc_url()}))
        else:
            asyncio.run(open_vm(show_native=not args.prepare_only))
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
