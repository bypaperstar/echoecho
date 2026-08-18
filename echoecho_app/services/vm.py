"""Sandbox ladder tier 2: echoecho's own macOS VM (PLAN-GENERIC.md).

One port, three implementations. A sandbox never runs the agent itself — it
decides WHAT host argv agent.run spawns and WHERE the workspace lives, so the
worker's whole subprocess pipeline (streaming, budgets, process-group kill)
is identical across tiers:

  ShellSandbox  tier 1: the CLI runs on the host, cwd=workspace/ (default)
  LumeVM        tier 2: the CLI runs inside a macOS guest over ssh (separate
                stdout/stderr, so the JSONL parse and the stderr tail behave
                exactly as tier 1), the workspace virtiofs-mounted
                read-write; the VM is an APFS clone of a golden image
                (scripts/vm_golden.sh), so reset() = delete + re-clone —
                "undo that" is cheap by construction
  FakeVM        CI: same port, local tmpdir as the "guest"; keeps the whole
                vm code path keyless and Linux-runnable

Warm policy: one shared LumeVM per process, booted on first use and left
running between tasks (clone ~seconds, cold boot ~30s, warm exec ~instant).
Teardown on a forced kill (budget breach) can't reach guest children over a
dead ssh, so the worker calls discard() — reset() throws the whole disposable
VM away and the next task re-clones a clean one; that, not SIGHUP, is what
guarantees no guest orphan outlives its budget.
"""
import asyncio
import json
import posixpath
import re
import shlex
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from echoecho_app import config, diagnostics


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
        # reset() must reap the detached runner and delete the disposable VM
        # without diagnostic I/O in the middle of that safety-critical
        # boundary.  Keep this state internal so tests and callers can still
        # replace _lume with the existing ``async def fake_lume(*args)``.
        self._cleanup_lume_depth = 0

    # -- lume CLI plumbing --------------------------------------------------

    async def _lume(self, *args):
        operation = str(args[0]) if args else "unknown"
        started = time.monotonic()
        instrument = self._cleanup_lume_depth == 0
        exe = shutil.which("lume")
        if exe is None:
            if instrument:
                diagnostics.warning("vm.lume.unavailable",
                                    operation=operation)
            raise SandboxUnavailable(
                "lume is not installed — run scripts/vm_golden.sh first")
        try:
            proc = await asyncio.create_subprocess_exec(
                exe, *args, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT)
            out, _ = await proc.communicate()
        except Exception as exc:
            if instrument:
                diagnostics.exception("vm.lume.failed", exc=exc,
                                      operation=operation)
            raise
        if instrument:
            diagnostics.info(
                "vm.lume.finished", operation=operation,
                exit_code=proc.returncode, output_bytes=len(out),
                duration_ms=round((time.monotonic() - started) * 1000, 1))
        return proc.returncode, out.decode("utf-8", "replace")

    async def _get(self):
        """Parsed `lume get <vm> -f json` as a dict, or None while the VM is
        unknown. lume 0.5.x wraps the record in a JSON array ([{...}]) and may
        prefix log lines like '[2026-..] INFO: ...' — whose '[' is NOT the
        JSON — so try each '['/'{' start until one actually parses, then
        unwrap a one-element list."""
        rc, out = await self._lume("get", self.vm_name, "-f", "json")
        if rc != 0:
            return None
        starts = sorted(i for i, c in enumerate(out) if c in "[{")
        for i in starts:
            try:
                data = json.loads(out[i:])
            except ValueError:
                continue
            if isinstance(data, list):
                data = data[0] if data else None
            return data if isinstance(data, dict) else None
        return None

    # -- lifecycle ------------------------------------------------------------

    async def prepare(self):
        started = time.monotonic()
        diagnostics.info("vm.prepare.started",
                         user_doc_mount_count=len(config.user_docs()),
                         workspace_mounted=self.workspace is not None)
        self._require_ssh_key()
        info = await self._get()
        if info is None:  # first use (or after reset): clone from golden
            diagnostics.info("vm.clone.started")
            await self._clone_from_golden()
            diagnostics.info("vm.clone.finished")
            info = await self._get()
        if not self._is_running(info):
            diagnostics.info("vm.boot.required")
            await self._boot()
        try:
            await self._wait_ready()
        except SandboxUnavailable as exc:
            diagnostics.exception("vm.ready.failed", exc=exc,
                                  recovery="one_shot")
            await self._recover_once()
        mounts_ok = await self._mounts_ok()
        diagnostics.info("vm.mount_check.finished", ok=mounts_ok)
        if not mounts_ok:
            # virtiofs shares bind at `lume run` time: a VM booted by an
            # older session serves ITS workspace at the guest mount, and
            # every write from this session silently lands in the wrong
            # host directory — reboot with this session's mounts attached
            await self._lume("stop", self.vm_name)
            await self._boot()
            await self._wait_ready()
            mounts_ok = await self._mounts_ok()
            diagnostics.info("vm.mount_check.finished", ok=mounts_ok,
                             after_reboot=True)
            if not mounts_ok:
                raise SandboxUnavailable(
                    "workspace %s is not visible inside VM %r at %s even "
                    "after a reboot with the share attached (virtiofs "
                    "mount failed)" % (self.workspace, self.vm_name,
                                       config.vm_guest_workspace()))
        diagnostics.info(
            "vm.prepare.finished",
            duration_ms=round((time.monotonic() - started) * 1000, 1))

    async def _mounts_ok(self):
        """Prove end-to-end that the guest sees THIS session's shares (lume
        get reports sharedDirectories as null even while one is live, so
        metadata can't be trusted): drop a nonce file host-side and look
        for it through the guest, and compare the mount root's listing to
        the expected share names — a doc folder added to (or revoked from)
        ECHOECHO_USER_DOCS only takes effect at boot time."""
        if self.workspace is None:
            return True
        root = posixpath.dirname(config.vm_guest_workspace().rstrip("/"))
        nonce = ".echoecho-mount-%s" % uuid.uuid4().hex[:8]
        self.workspace.mkdir(parents=True, exist_ok=True)
        host = self.workspace / nonce
        host.write_text("mount-check", encoding="utf-8")
        try:
            proc = await asyncio.create_subprocess_exec(
                *self.ssh_argv("test -f %s && ls -1 %s"
                               % (self.guest_path(nonce), shlex.quote(root))),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL)
            out, _ = await asyncio.wait_for(proc.communicate(), 20.0)
            if proc.returncode != 0:
                return False
            seen = {n for n in out.decode("utf-8", "replace").splitlines()
                    if n and not n.startswith(".")}  # a stray .DS_Store must
            expected = {self.workspace.name}         # not force reboot loops
            expected.update(d.name for d in config.user_docs())
            return seen == expected
        except (OSError, asyncio.TimeoutError):
            return False
        finally:
            try:
                host.unlink()
            except OSError:
                pass

    def _require_ssh_key(self):
        """Fail before boot, with the fix in the message: a missing key
        otherwise surfaces minutes later as an opaque ssh auth error."""
        key = Path(config.vm_ssh_key()).expanduser()
        if not key.exists():
            raise SandboxUnavailable(
                "SSH key %s not found — scripts/vm_golden.sh creates it, or "
                "point ECHOECHO_VM_SSH_KEY at the key your golden image was "
                "built with" % key)

    async def _clone_from_golden(self):
        rc, out = await self._lume("clone", self.golden, self.vm_name)
        if rc == 0:
            return
        names = await self._vm_names()
        raise SandboxUnavailable(
            "could not clone %r -> %r (existing VMs: %s — is the golden "
            "image built and named right? scripts/vm_golden.sh, or set "
            "ECHOECHO_VM_GOLDEN): %s"
            % (self.golden, self.vm_name, ", ".join(names) or "none",
               out.strip()[-300:]))

    async def _vm_names(self):
        """Names of every VM lume knows about — the clone-failure message
        shows them so a renamed golden image is obvious at a glance."""
        rc, out = await self._lume("ls", "-f", "json")
        if rc != 0:
            return []
        for i in sorted(j for j, c in enumerate(out) if c in "[{"):
            try:
                data = json.loads(out[i:])
            except ValueError:
                continue
            if isinstance(data, dict):
                data = [data]
            return [str(d.get("name")) for d in data
                    if isinstance(d, dict) and d.get("name")]
        return []

    async def _recover_once(self):
        """One shot at self-healing the two boot failures seen in the wild
        before giving up:

        * "Failed to lock auxiliary storage" — a detached `lume run` from a
          dead echoecho (plus its Virtualization.framework child) still owns
          the VM while `lume get` reports "stopped"; every boot dies
          instantly until the orphans are reaped (found live: a runner from
          three days earlier held the lock at 27% CPU).
        * A plain timeout with lume claiming "running" — lume's session
          state drifted (stale IP, daemon restart); `lume stop` resets it so
          a fresh boot hands out a live address.
        """
        if self._boot_log_mentions_lock():
            diagnostics.warning("vm.recovery.started", reason="stale_lock")
            await self._kill_stale_holders()
        else:
            diagnostics.warning("vm.recovery.started", reason="state_drift")
            await self._lume("stop", self.vm_name)
            await asyncio.sleep(1.0)
        await self._boot()
        await self._wait_ready()
        diagnostics.info("vm.recovery.finished")

    async def _kill_stale_holders(self):
        """Best-effort reap of orphaned owners of this (disposable, scratch)
        VM: the detached runner by command line, then whatever still holds
        the aux-storage file."""
        await self._run_quiet(
            "pkill", "-f", r"lume run %s( |$)" % re.escape(self.vm_name))
        await asyncio.sleep(2.0)
        nvram = Path.home() / ".lume" / self.vm_name / "nvram.bin"
        rc, out = await self._run_quiet("lsof", "-t", str(nvram))
        pids = [p for p in out.split() if p.strip().isdigit()]
        if pids:
            await self._run_quiet("kill", "-9", *pids)
            await asyncio.sleep(1.0)

    @staticmethod
    async def _run_quiet(exe, *args):
        """(rc, combined output) of a host command; a missing binary is
        rc=127 — recovery paths are best-effort, never a crash."""
        try:
            proc = await asyncio.create_subprocess_exec(
                exe, *args, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT)
        except OSError:
            return 127, ""
        out, _ = await proc.communicate()
        return proc.returncode, out.decode("utf-8", "replace")

    def _is_running(self, info):
        return bool(info) and str(info.get("status", "")).lower() == "running"

    def _boot_argv(self, exe="lume"):
        """`lume run` argv: the workspace mounts read-WRITE; each shared
        user-doc folder (config.user_docs()) mounts read-ONLY, so an agent can
        read the user's documents but the only write path back is the outbox +
        spoken approval (workers/outbox.py)."""
        argv = [exe, "run", self.vm_name, "--no-display"]
        if self.workspace is not None:
            argv += ["--shared-dir", "%s:rw" % self.workspace]
        for doc in config.user_docs():
            argv += ["--shared-dir", "%s:ro" % doc]
        return argv

    def _boot_log_path(self):
        return Path(tempfile.gettempdir()) / (
            "echoecho-lume-run-%s.log" % self.vm_name)

    def _boot_log_tail(self, limit=300):
        try:
            text = self._boot_log_path().read_text("utf-8", "replace").strip()
        except OSError:
            return ""
        errs = [line for line in text.splitlines()
                if "ERROR" in line or line.startswith("Error")]
        tail = " | ".join(errs[-3:]) if errs else text[-limit:]
        return tail[-limit:]

    def _boot_log_mentions_lock(self):
        try:
            text = self._boot_log_path().read_text("utf-8", "replace")
        except OSError:
            return False
        return "failed to lock" in text.lower()

    async def _boot(self):
        exe = shutil.which("lume")
        if exe is None:
            raise SandboxUnavailable("lume is not installed")
        # detached: `lume run` stays alive for the VM's lifetime in some lume
        # versions; the daemon owns the VM either way, we only poll state.
        # Its output goes to a log file, NOT devnull: when the boot dies
        # (e.g. the aux-storage lock), _wait_ready needs its last words.
        with open(self._boot_log_path(), "wb") as log:
            self._runner = await asyncio.create_subprocess_exec(
                *self._boot_argv(exe), stdout=log,
                stderr=asyncio.subprocess.STDOUT, start_new_session=True)
        diagnostics.info("vm.boot.spawned", pid=self._runner.pid,
                         share_count=(1 if self.workspace is not None else 0)
                         + len(config.user_docs()))

    async def _wait_ready(self, timeout=None):
        """Poll until the VM reports running + an IP, then until SSH answers.
        A `lume run` that exits nonzero fails FAST with its own error text
        instead of burning the whole timeout in silence; rc=0 keeps polling
        (some lume versions hand the VM to the daemon and return)."""
        budget = timeout or config.vm_boot_timeout()
        deadline = time.monotonic() + budget
        status = None
        polls = 0
        started = time.monotonic()
        while time.monotonic() < deadline:
            polls += 1
            runner_rc = self._runner.returncode if self._runner else None
            if runner_rc not in (None, 0):
                raise SandboxUnavailable(
                    "`lume run %s` exited (rc=%s) during boot: %s"
                    % (self.vm_name, runner_rc,
                       self._boot_log_tail() or "no output"))
            info = await self._get()
            status = (info or {}).get("status")
            self.ip = (info or {}).get("ipAddress") or (info or {}).get("ip")
            if self._is_running(info) and self.ip:
                if await self._ssh_up():
                    diagnostics.info(
                        "vm.ready", polls=polls,
                        duration_ms=round(
                            (time.monotonic() - started) * 1000, 1))
                    return
            await asyncio.sleep(1.0)
        tail = self._boot_log_tail()
        diagnostics.error(
            "vm.ready.timed_out", polls=polls, budget_s=budget,
            last_status=status, boot_log_available=bool(tail))
        raise SandboxUnavailable(
            "VM %r did not become reachable within %.0fs (last lume state: "
            "status=%s ip=%s)%s"
            % (self.vm_name, budget, status, self.ip,
               "; lume run said: " + tail if tail else ""))

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
        re-clones from the golden image (APFS clone: seconds). Loud when the
        delete leaves the VM behind — swallowing that would hand the NEXT
        task a dirty VM with the killed task's guest processes still alive,
        breaking the no-orphan-outlives-its-budget guarantee."""
        started = time.monotonic()
        self._cleanup_lume_depth += 1
        try:
            await self._lume("stop", self.vm_name)
            if self._runner is not None:  # reap our detached `lume run`: a
                try:                      # lingering one holds the aux-storage
                    self._runner.terminate()  # lock and blocks every next boot
                except ProcessLookupError:
                    pass
                self._runner = None
            rc, out = await self._lume("delete", self.vm_name, "--force")
            self.ip = None
            if rc != 0 and await self._get() is not None:
                raise SandboxUnavailable(
                    "could not delete VM %r (a dirty scratch VM would leak "
                    "into the next task): %s"
                    % (self.vm_name, out.strip()[-300:]))
        finally:
            self._cleanup_lume_depth -= 1
        diagnostics.warning("vm.reset.finished", exit_code=rc,
                            duration_ms=round(
                                (time.monotonic() - started) * 1000, 1))

    # -- ssh into the guest ---------------------------------------------------

    def ssh_argv(self, remote):
        """ssh argv running one command string in the guest. NO -tt: a PTY
        would merge stdout+stderr (empties the stderr tail, can splice stderr
        into the JSONL stream). Shared by command() and the GUI driver
        (services/gui.py); guest teardown on a kill is discard()/reset()."""
        if not self.ip:
            raise SandboxUnavailable("VM not prepared")
        ssh = ["ssh",
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
        return ssh

    # -- the one job: wrap the agent argv ------------------------------------

    def command(self, argv, workspace):
        # sshd gives non-login commands a bare PATH (/usr/bin:/bin:...) that
        # misses /usr/local/bin, where vm_golden.sh installs node + the
        # claude CLI — without the prepend, bare `claude` exits 127
        prepend = config.vm_guest_path_prepend()
        path = 'PATH=%s:"$PATH" ' % shlex.quote(prepend) if prepend else ""
        remote = "cd %s && %sexec %s" % (
            shlex.quote(config.vm_guest_workspace()), path,
            " ".join(shlex.quote(a) for a in argv))
        return self.ssh_argv(remote), Path(workspace)

    def guest_path(self, name):
        """Absolute path inside the guest for a workspace-relative name (the
        virtiofs mount), shell-quoted."""
        return shlex.quote(config.vm_guest_workspace().rstrip("/") + "/" + name)


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


# -- VNC endpoint discovery (viewer /vnc-info, PR 15) -------------------------

def parse_lume_record(out):
    """Parsed record from `lume ... -f json` combined output, or None — the
    same tolerance as LumeVM._get: lume 0.5.x wraps the record in a JSON
    array ([{...}]) and may prefix log lines like '[2026-..] INFO: ...',
    whose '[' is NOT the JSON, so try each '['/'{' start until one parses,
    then unwrap a one-element list."""
    starts = sorted(i for i, c in enumerate(out) if c in "[{")
    for i in starts:
        try:
            data = json.loads(out[i:])
        except ValueError:
            continue
        if isinstance(data, list):
            data = data[0] if data else None
        return data if isinstance(data, dict) else None
    return None


def _lume_get_sync(vm_name):
    """(rc, combined output) of `lume get <vm> -f json` — a blocking twin of
    LumeVM._lume for callers with no event loop (the viewer's HTTP threads).
    Tests monkeypatch this to feed fake lume output."""
    exe = shutil.which("lume")
    if exe is None:
        raise SandboxUnavailable(
            "lume is not installed — run scripts/vm_golden.sh first")
    proc = subprocess.run(
        [exe, "get", vm_name, "-f", "json"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=15)
    return proc.returncode, proc.stdout.decode("utf-8", "replace")


def vnc_url(vm_name=None):
    """The running VM's vncUrl (vnc://[:pass@]ip:port), read fresh from lume
    on every call — a re-cloned VM gets a new address/password, so caching
    would hand the portal a dead endpoint. Raises SandboxUnavailable with a
    human-readable reason on any failure."""
    name = vm_name or config.vm_name()
    rc, out = _lume_get_sync(name)
    info = parse_lume_record(out) if rc == 0 else None
    if info is None:
        raise SandboxUnavailable(
            "no VM %r (lume get: %s)" % (name, out.strip()[-200:] or "rc=%s" % rc))
    if str(info.get("status", "")).lower() != "running":
        raise SandboxUnavailable(
            "VM %r is not running (status: %s)"
            % (name, info.get("status") or "unknown"))
    url = info.get("vncUrl")
    if not url:
        raise SandboxUnavailable("VM %r reports no vncUrl" % name)
    return str(url)


_shared_vm = None  # warm policy: one VM per echoecho process, kept booted


def shared_vm(workspace=None):
    global _shared_vm
    if _shared_vm is None:
        _shared_vm = LumeVM(workspace=workspace)
    return _shared_vm


async def discard(sandbox):
    """Called by the worker after it had to FORCE-KILL an agent (budget
    breach, exception): a dead local ssh can't reap guest children, so throw
    the whole disposable VM away. reset() is a no-op tier for shell (host
    process-group kill already took the tree). Clears the warm singleton so
    the next task re-clones a clean guest."""
    global _shared_vm
    reset = getattr(sandbox, "reset", None)
    if reset is not None:
        try:
            await reset()
        except Exception as exc:
            # discard runs in kill/error paths and must not raise, but a
            # swallowed failed delete means the next task may inherit a
            # dirty VM — say so where the operator can see it
            diagnostics.exception("vm.discard.failed", exc=exc,
                                  sandbox=getattr(sandbox, "name", "sandbox"))
            print("[vm] WARNING: discard of %s failed: %s"
                  % (getattr(sandbox, "name", "sandbox"), exc))
    if sandbox is _shared_vm:
        _shared_vm = None


def for_task(task, ctx):
    """Pick the sandbox for one agent.run task: injected (tests) >
    args.sandbox > ECHOECHO_SANDBOX default. The vm tier returns the shared
    warm instance."""
    injected = ctx.extra.get("sandbox")
    if injected is not None:
        diagnostics.info("sandbox.selected", sandbox=injected.name,
                         source="injected")
        return injected
    tier = task.request.args.get("sandbox") or config.sandbox_tier()
    if tier == "vm":
        # Voice models sometimes ask for the VM on a machine that has none.
        # When the operator's own default is the shell tier and lume isn't
        # installed, run there instead of killing the task; an explicit
        # ECHOECHO_SANDBOX=vm with lume missing stays a loud failure.
        if config.sandbox_tier() != "vm" and shutil.which("lume") is None:
            diagnostics.warning("sandbox.fallback", requested="vm",
                                selected="shell", reason="lume unavailable")
            return ShellSandbox()
        diagnostics.info("sandbox.selected", sandbox="vm", source="task")
        return shared_vm(workspace=ctx.workspace)
    diagnostics.info("sandbox.selected", sandbox="shell", source="task")
    return ShellSandbox()
