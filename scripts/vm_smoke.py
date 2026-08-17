#!/usr/bin/env python3
"""Live smoke test for the Lume VM sandbox tier (Mac-only, keyless).

One run = the exact path an agent.run task takes through services/vm.py:
prepare() the VM (clone from the golden image if missing, boot if stopped,
recover from stale locks, wait for SSH), exec a command inside the guest
through the same ssh argv the worker uses, prove the virtiofs workspace
round-trips guest -> host, and read the VNC endpoint the viewer's /vnc-info
hands to the orb portal.

  python3 scripts/vm_smoke.py               # warm: reuse whatever is running
  python3 scripts/vm_smoke.py --cold        # reset() first: delete + re-clone
  python3 scripts/vm_smoke.py --stop-first  # lume stop first: boot-only path
  python3 scripts/vm_smoke.py --fresh-ws    # new workspace dir: the running
                                            # VM's mounts are stale, prepare()
                                            # must reboot it with fresh shares

The default workspace is a stable path (like the daemon's), so back-to-back
runs exercise true warm reuse. Exit 0 = PASS (with per-step timings),
exit 1 = FAIL naming the step.
"""
import argparse
import asyncio
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from echoecho_app.services import vm as vm_mod  # noqa: E402


def check(cond, what, detail=""):
    if not cond:
        print("FAIL at %s%s" % (what, ": " + detail if detail else ""))
        raise SystemExit(1)


async def run(args):
    t0 = time.monotonic()

    def step(name):
        print("  %-26s %6.1fs" % (name, time.monotonic() - t0), flush=True)

    if args.fresh_ws:
        ws = Path(tempfile.mkdtemp(prefix="echoecho-smoke-")) / "workspace"
    else:
        ws = Path(tempfile.gettempdir()) / "echoecho-smoke" / "workspace"
    ws.mkdir(parents=True, exist_ok=True)  # basename "workspace", like the daemon
    box = vm_mod.LumeVM(workspace=ws)
    print("smoke: vm=%s golden=%s ws=%s" % (box.vm_name, box.golden, ws))

    if args.cold:
        await box.reset()
        step("reset (delete VM)")
    if args.stop_first:
        await box._lume("stop", box.vm_name)
        step("lume stop")

    await box.prepare()
    step("prepare (VM ready)")

    # guest exec through the worker's own ssh argv; the guest cwd is the
    # virtiofs workspace mount, so the write must appear on the host
    token = uuid.uuid4().hex[:8]
    argv, _ = box.command(
        ["sh", "-c",
         "echo SMOKE_OK $(hostname) PATH=$PATH; "
         "echo %s > smoke-%s.txt" % (token, token)],
        ws)
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=90)
    check(proc.returncode == 0, "guest exec",
          "rc=%s stdout=%r stderr=%r"
          % (proc.returncode, proc.stdout[-200:], proc.stderr[-300:]))
    check("SMOKE_OK" in proc.stdout, "guest exec", "no SMOKE_OK in output")
    check("/usr/local/bin" in proc.stdout, "guest PATH",
          "missing /usr/local/bin (claude would exit 127): %r"
          % proc.stdout[-200:])
    step("guest exec over ssh")

    # the real agent CLI must resolve through the guest's non-login PATH;
    # soft on guests without claude installed (golden built with AGENT=0)
    argv, _ = box.command(
        ["sh", "-c", "command -v claude >/dev/null 2>&1 && claude --version "
                     "|| echo NO_CLAUDE"], ws)
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=90)
    check(proc.returncode == 0 and proc.stdout.strip(), "claude resolution",
          "rc=%s stdout=%r" % (proc.returncode, proc.stdout[-200:]))
    step("claude absent (skipped)" if "NO_CLAUDE" in proc.stdout
         else "claude resolves: %s" % proc.stdout.strip()[:40])

    marker = ws / ("smoke-%s.txt" % token)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and not marker.exists():
        time.sleep(0.5)
    check(marker.exists(), "virtiofs round-trip",
          "guest wrote smoke-%s.txt but it never appeared in %s" % (token, ws))
    check(token in marker.read_text(), "virtiofs round-trip", "content mismatch")
    step("virtiofs guest->host")

    url = vm_mod.vnc_url(box.vm_name)
    check(url.startswith("vnc://"), "vnc endpoint", "got %r" % url)
    step("vnc endpoint")

    print("PASS (%.1fs total)" % (time.monotonic() - t0))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cold", action="store_true",
                    help="reset() first: delete the scratch VM, re-clone")
    ap.add_argument("--stop-first", action="store_true",
                    help="lume stop first: exercise the boot-only path")
    ap.add_argument("--fresh-ws", action="store_true",
                    help="unique workspace dir: exercise the stale-mount "
                         "detection + reboot path")
    asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    main()
