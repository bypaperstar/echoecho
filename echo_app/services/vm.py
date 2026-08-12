"""Sandbox ladder tier 2: Echo's own macOS VM (PLAN-GENERIC.md).

One port, three implementations. A sandbox never runs the agent itself — it
decides WHAT host argv agent.run spawns and WHERE the workspace lives, so the
worker's whole subprocess pipeline (streaming, budgets, process-group kill)
is identical across tiers:

  ShellSandbox  tier 1: the CLI runs on the host, cwd=workspace/ (default)
  LumeVM        tier 2: the CLI runs inside a macOS guest over ssh -tt, the
                workspace virtiofs-mounted read-write; the VM is an APFS
                clone of a golden image (scripts/vm_golden.sh), so reset()
                = delete + re-clone — "undo that" is cheap by construction
  FakeVM        CI: same port, local tmpdir as the "guest"; keeps the whole
                vm code path keyless and Linux-runnable

Warm policy: one shared LumeVM per process, booted on first use and left
running between tasks (clone ~seconds, cold boot ~30s, warm exec ~instant).
Killing the host ssh (budget breach) drops the tty, which HUPs the remote
tree — and the VM is disposable anyway.
"""
import asyncio
import json
import shlex
import shutil
import time
from pathlib import Path

from echo_app import config


class SandboxUnavailable(Exception):
    pass


class ShellSandbox:
    """Tier 1: host subprocess jailed to the workspace by cwd + the agent
    CLI's own permission mode (see agent_cli tier-1 flags)."""

    name = "shell"

    async def prepare(self):
        pass

    def command(self, argv, workspace):
        return list(argv), Path(workspace)


class LumeVM:
    """Tier 2: a macOS guest managed by the `lume` CLI, controlled over SSH.

    prepare() is idempotent: clone from the golden image if the VM doesn't
    exist, boot it with the workspace shared read-write, wait for SSH. The
    guest sees the workspace at guest_workspace (virtiofs), so files the
    agent writes appear on the host live — the viewer and touched-file
    detection keep working unchanged.
    """

    name = "vm"

    def __init__(self, vm_name=None, golden=None, workspace=None):
        self.vm_name = vm_name or config.vm_name()
        self.golden = golden or config.vm_golden()
        self.workspace = Path(workspace) if workspace else None
        self.ip = None
        self._runner = None  # detached `lume run` process handle

    # -- lume CLI plumbing --------------------------------------------------

    async def _lume(self, *args):
        exe = shutil.which("lume")
        if exe is None:
            raise SandboxUnavailable(
                "lume is not installed — run scripts/vm_golden.sh first")
        proc = await asyncio.create_subprocess_exec(
            exe, *args, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT)
        out, _ = await proc.communicate()
        return proc.returncode, out.decode("utf-8", "replace")

    async def _get(self):
        """Parsed `lume get <vm> -f json`, or None while the VM is unknown."""
        rc, out = await self._lume("get", self.vm_name, "-f", "json")
        if rc != 0:
            return None
        try:
            start = out.index("{")
            return json.loads(out[start:])
        except ValueError:
            return None

    # -- lifecycle ------------------------------------------------------------

    async def prepare(self):
        info = await self._get()
        if info is None:  # first use (or after reset): clone from golden
            rc, out = await self._lume("clone", self.golden, self.vm_name)
            if rc != 0:
                raise SandboxUnavailable(
                    "could not clone %r -> %r (is the golden image built? "
                    "scripts/vm_golden.sh): %s"
                    % (self.golden, self.vm_name, out.strip()[-300:]))
            info = await self._get()
        if not self._is_running(info):
            await self._boot()
        await self._wait_ready()

    def _is_running(self, info):
        return bool(info) and str(info.get("status", "")).lower() == "running"

    async def _boot(self):
        exe = shutil.which("lume")
        if exe is None:
            raise SandboxUnavailable("lume is not installed")
        argv = [exe, "run", self.vm_name, "--no-display"]
        if self.workspace is not None:
            argv += ["--shared-dir", "%s:rw" % self.workspace]
        # detached: `lume run` stays alive for the VM's lifetime in some lume
        # versions; the daemon owns the VM either way, we only poll state
        self._runner = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL, start_new_session=True)

    async def _wait_ready(self, timeout=None):
        """Poll until the VM reports running + an IP, then until SSH answers."""
        deadline = time.monotonic() + (timeout or config.vm_boot_timeout())
        while time.monotonic() < deadline:
            info = await self._get()
            self.ip = (info or {}).get("ipAddress") or (info or {}).get("ip")
            if self._is_running(info) and self.ip:
                if await self._ssh_up():
                    return
            await asyncio.sleep(1.0)
        raise SandboxUnavailable(
            "VM %r did not become reachable within the boot timeout"
            % self.vm_name)

    async def _ssh_up(self):
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(self.ip, 22), 2.0)
        except (OSError, asyncio.TimeoutError):
            return False
        writer.close()
        return True

    async def reset(self):
        """Snapshot rollback: throw the scratch VM away; the next prepare()
        re-clones from the golden image (APFS clone: seconds)."""
        await self._lume("stop", self.vm_name)
        await self._lume("delete", self.vm_name, "--force")
        self.ip = None

    # -- the one job: wrap the agent argv ------------------------------------

    def command(self, argv, workspace):
        if not self.ip:
            raise SandboxUnavailable("VM not prepared")
        remote = "cd %s && exec %s" % (
            shlex.quote(config.vm_guest_workspace()),
            " ".join(shlex.quote(a) for a in argv))
        ssh = ["ssh", "-tt",  # tty: killing host ssh HUPs the remote tree
               "-i", str(Path(config.vm_ssh_key()).expanduser()),
               "-o", "StrictHostKeyChecking=no",
               "-o", "UserKnownHostsFile=/dev/null",
               "-o", "LogLevel=ERROR",
               "-o", "BatchMode=yes",
               "-o", "ConnectTimeout=10"]
        for env_name in config.vm_pass_env():
            # guest sshd AcceptEnv (set up by vm_golden.sh) lets model API
            # keys reach the in-guest agent without appearing in guest ps
            ssh += ["-o", "SendEnv=%s" % env_name]
        ssh.append("%s@%s" % (config.vm_guest_user(), self.ip))
        ssh.append(remote)
        return ssh, Path(workspace)


class FakeVM:
    """CI stand-in behind the same port: a tmpdir is the 'guest', the
    'mount' is the identity mapping, and every exec stamps the guest's
    exec.log — tests prove agent.run routed through the sandbox without a
    Mac, lume, or a network."""

    name = "fake-vm"

    def __init__(self, root):
        self.root = Path(root)
        self.prepared = 0

    async def prepare(self):
        self.root.mkdir(parents=True, exist_ok=True)
        self.prepared += 1

    async def reset(self):
        shutil.rmtree(str(self.root), ignore_errors=True)

    def command(self, argv, workspace):
        script = 'echo exec >> "$0/exec.log" && cd "$1" && shift && exec "$@"'
        return (["sh", "-c", script, str(self.root), str(workspace)]
                + list(argv), Path(workspace))


_shared_vm = None  # warm policy: one VM per Echo process, kept booted


def shared_vm(workspace=None):
    global _shared_vm
    if _shared_vm is None:
        _shared_vm = LumeVM(workspace=workspace)
    return _shared_vm


def for_task(task, ctx):
    """Pick the sandbox for one agent.run task: injected (tests) >
    args.sandbox > ECHO_SANDBOX default. The vm tier returns the shared
    warm instance."""
    injected = ctx.extra.get("sandbox")
    if injected is not None:
        return injected
    tier = task.request.args.get("sandbox") or config.sandbox_tier()
    if tier == "vm":
        return shared_vm(workspace=ctx.workspace)
    return ShellSandbox()
