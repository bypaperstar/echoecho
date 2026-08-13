"""PR 12 sandbox ladder tier 2: the SandboxPort, tier selection, and the
FakeVM that keeps the whole vm code path keyless + Linux-runnable. The real
LumeVM lifecycle (lume clone/run/ssh) is Mac-only; here we prove agent.run
routes its argv through whatever sandbox is chosen, unchanged pipeline."""
import asyncio
import json
from pathlib import Path

import pytest

from echoecho_app import config
from echoecho_app.bus import Task, TaskRequest, TaskResult
from echoecho_app.orchestrator.core import Orchestrator, WorkerContext
from echoecho_app.services import vm as vm_mod
from echoecho_app.services.agent_cli import ClaudeCLI, FakeAgentCLI
from echoecho_app.services.vm import FakeVM, LumeVM, ShellSandbox, for_task
from echoecho_app.workers.base import load_all


def write_script(path, events):
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n",
                    encoding="utf-8")
    return path


def run_orch(requests, tmp_path, extra, timeout=5.0):
    injections = []
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    orch = Orchestrator(registry=load_all(), on_injection=injections.append,
                        log_path=tmp_path / "tasks.jsonl", workspace=ws)
    orch.ctx.extra.update(extra)

    async def go():
        loop = asyncio.ensure_future(orch.run())
        for req in requests:
            orch.submit(req)
        assert await orch.drain(timeout), "did not drain"
        loop.cancel()

    asyncio.run(go())
    return orch, injections, ws


# -- tier selection -----------------------------------------------------------

def test_for_task_defaults_to_shell(tmp_path, monkeypatch):
    monkeypatch.delenv("ECHOECHO_SANDBOX", raising=False)
    ctx = WorkerContext(workspace=tmp_path)
    task = Task(id="t1", request=TaskRequest(kind="agent.run"))
    assert isinstance(for_task(task, ctx), ShellSandbox)


def test_env_and_arg_select_vm(tmp_path, monkeypatch):
    monkeypatch.setenv("ECHOECHO_SANDBOX", "vm")
    vm_mod._shared_vm = None
    ctx = WorkerContext(workspace=tmp_path)
    assert isinstance(for_task(Task(id="t1", request=TaskRequest(
        kind="agent.run")), ctx), LumeVM)
    # a per-task arg overrides the env default either way
    monkeypatch.delenv("ECHOECHO_SANDBOX", raising=False)
    assert isinstance(for_task(Task(id="t2", request=TaskRequest(
        kind="agent.run", args={"sandbox": "vm"})), ctx), LumeVM)
    assert isinstance(for_task(Task(id="t3", request=TaskRequest(
        kind="agent.run", args={"sandbox": "shell"})), ctx), ShellSandbox)


def test_injected_sandbox_wins(tmp_path):
    fake = FakeVM(tmp_path / "guest")
    ctx = WorkerContext(workspace=tmp_path)
    ctx.extra["sandbox"] = fake
    assert for_task(Task(id="t1", request=TaskRequest(
        kind="agent.run", args={"sandbox": "vm"})), ctx) is fake


def test_vm_tier_is_a_shared_warm_instance(tmp_path, monkeypatch):
    monkeypatch.setenv("ECHOECHO_SANDBOX", "vm")
    vm_mod._shared_vm = None
    ctx = WorkerContext(workspace=tmp_path)
    a = for_task(Task(id="t1", request=TaskRequest(kind="agent.run")), ctx)
    b = for_task(Task(id="t2", request=TaskRequest(kind="agent.run")), ctx)
    assert a is b  # one warm VM per process, reused across tasks


# -- the port: ShellSandbox / FakeVM command wrapping -------------------------

def test_shell_sandbox_is_identity():
    sb = ShellSandbox()
    argv, cwd = sb.command(["claude", "-p", "hi"], Path("/ws"))
    assert argv == ["claude", "-p", "hi"] and cwd == Path("/ws")


def test_fake_vm_wraps_and_logs_exec(tmp_path):
    guest = tmp_path / "guest"
    sb = FakeVM(guest)
    asyncio.run(sb.prepare())
    argv, cwd = sb.command(["echo", "hello"], tmp_path / "ws")
    assert argv[0] == "sh" and str(guest) in argv
    assert argv[-2:] == ["echo", "hello"]  # original argv preserved at the tail


# -- agent.run routed through the vm port (offline) ---------------------------

def test_agent_run_through_fake_vm_end_to_end(tmp_path):
    """The whole worker pipeline runs the agent 'inside' the FakeVM: prepare()
    is called, the exec is logged in the guest, and the recorded stream still
    parses to a result and writes a workspace file — same as tier 1."""
    script = write_script(tmp_path / "s.jsonl", [
        {"type": "system", "subtype": "init", "session_id": "vm-1"},
        {"type": "_write", "file": "out/report.md", "content": "# Report\nok\n"},
        {"type": "result", "subtype": "success", "is_error": False,
         "session_id": "vm-1", "result": "ran inside the VM"},
    ])
    guest = tmp_path / "guest"
    orch, injections, ws = run_orch(
        [TaskRequest(kind="agent.run", instructions="write a report",
                     args={"sandbox": "vm"})],
        tmp_path, extra={"agent_cli": FakeAgentCLI(script),
                         "sandbox": FakeVM(guest)})
    task = orch.tasks["t1"]
    assert task.status == "done"
    assert task.result.say == "ran inside the VM"
    assert task.result.data["sandbox"] == "fake-vm"
    # the exec was routed through the sandbox (guest exec.log written)...
    assert (guest / "exec.log").read_text().strip() == "exec"
    # ...and workspace writes still land on the host (virtiofs-equivalent)
    assert (ws / "out" / "report.md").read_text() == "# Report\nok\n"
    assert task.result.artifacts_touched == ["out/report.md"]


def test_sandbox_prepare_failure_is_a_clean_error(tmp_path):
    class BrokenVM(FakeVM):
        async def prepare(self):
            raise vm_mod.SandboxUnavailable("golden image not built")

    orch, injections, _ = run_orch(
        [TaskRequest(kind="agent.run", instructions="x",
                     args={"sandbox": "vm"})],
        tmp_path, extra={"agent_cli": FakeAgentCLI(
            write_script(tmp_path / "s.jsonl",
                         [{"type": "result", "is_error": False, "result": "x"}])),
                         "sandbox": BrokenVM(tmp_path / "g")})
    result = orch.tasks["t1"].result
    assert result.data["error"].startswith("sandbox:")
    assert "couldn't start" in result.say
    assert injections[0].priority == "interrupt"


# -- LumeVM argv shape (no VM needed: pure string building) -------------------

def test_lume_vm_wraps_argv_in_ssh(monkeypatch):
    monkeypatch.setenv("ECHOECHO_VM_USER", "lume")
    monkeypatch.setenv("ECHOECHO_VM_SSH_KEY", "/keys/echoecho")
    # the real default guest mount has a space ("My Shared Files"): it MUST
    # be shell-quoted into the remote command
    monkeypatch.setenv("ECHOECHO_VM_GUEST_WORKSPACE",
                       "/Volumes/My Shared Files/workspace")
    monkeypatch.setenv("ECHOECHO_VM_PASS_ENV", "ANTHROPIC_API_KEY")
    vm = LumeVM(vm_name="echoecho-vm")
    vm.ip = "192.168.64.7"
    argv, cwd = vm.command(["claude", "-p", "do a thing", "--resume", "s1"],
                           Path("/host/ws"))
    assert argv[0] == "ssh"
    # NO -tt: a PTY would merge guest stdout+stderr (corrupts the JSONL parse
    # + empties the stderr tail); teardown is via discard(), not tty HUP
    assert "-tt" not in argv
    assert "lume@192.168.64.7" in argv
    assert "-i" in argv and "/keys/echoecho" in argv
    assert any("SendEnv=ANTHROPIC_API_KEY" in a for a in argv)
    remote = argv[-1]
    assert remote.startswith("cd '/Volumes/My Shared Files/workspace' && exec ")
    # the agent argv is shell-quoted into the remote command, intact
    assert "'do a thing'" in remote and "--resume" in remote
    # cwd stays the host workspace (touched-file detection runs host-side)
    assert cwd == Path("/host/ws")


def test_vm_pass_env_defaults_to_anthropic_only(monkeypatch):
    """Least privilege: the untrusted VM gets ONLY the key its guest runtime
    (claude) needs — never echoecho's OpenAI voice key by default."""
    monkeypatch.delenv("ECHOECHO_VM_PASS_ENV", raising=False)
    assert config.vm_pass_env() == ["ANTHROPIC_API_KEY"]
    monkeypatch.setenv("ECHOECHO_VM_PASS_ENV", "OPENAI_API_KEY FOO_KEY")
    assert config.vm_pass_env() == ["OPENAI_API_KEY", "FOO_KEY"]
    vm = LumeVM(vm_name="echoecho-vm")
    vm.ip = "10.0.0.2"
    argv, _ = vm.command(["claude", "-p", "x"], Path("/ws"))
    monkeypatch.delenv("ECHOECHO_VM_PASS_ENV", raising=False)
    argv2, _ = vm.command(["claude", "-p", "x"], Path("/ws"))
    assert not any("OPENAI_API_KEY" in a for a in argv2)  # default keeps it out


def test_lume_vm_command_requires_prepared_ip():
    vm = LumeVM(vm_name="echoecho-vm")
    with pytest.raises(vm_mod.SandboxUnavailable):
        vm.command(["claude"], Path("/ws"))


# real `lume get -f json` output (0.5.3): a JSON ARRAY, prefixed by nothing
# here but log lines in practice — captured live on the Mac
LUME_GET_ARRAY = '''[
  {
    "status" : "running",
    "os" : "macOS",
    "ipAddress" : "192.168.64.3",
    "name" : "echoecho-vm",
    "sharedDirectories" : null
  }
]
'''


def test_lume_get_parses_array_output(monkeypatch):
    """Regression (found live): lume 0.5.3 wraps the record in a JSON array;
    the old `json.loads(out[out.index('{'):])` choked on the trailing ']'."""
    vm = LumeVM(vm_name="echoecho-vm")

    async def fake_lume(*args):
        return 0, LUME_GET_ARRAY
    vm._lume = fake_lume
    info = asyncio.run(vm._get())
    assert info["status"] == "running"
    assert info["ipAddress"] == "192.168.64.3"

    # log-line prefix (lume emits INFO lines before the JSON) is tolerated
    async def fake_lume_prefixed(*args):
        return 0, "[2026-08-13T02:00:00Z] INFO: fetching\n" + LUME_GET_ARRAY
    vm._lume = fake_lume_prefixed
    assert asyncio.run(vm._get())["ipAddress"] == "192.168.64.3"

    # non-zero rc or garbage -> None, never a crash
    async def fake_fail(*args):
        return 1, "not found"
    vm._lume = fake_fail
    assert asyncio.run(vm._get()) is None


# -- forced-kill disposes the VM (guest orphans can't outlive the budget) -----

def test_budget_kill_discards_the_vm_via_reset(tmp_path, monkeypatch):
    """On a budget breach the local ssh dies but can't reap guest children,
    so the worker must dispose the whole VM. Proven with a FakeVM whose
    reset() records it, driven through the real timeout+kill path."""
    monkeypatch.setenv("ECHOECHO_AGENT_TIMEOUT", "0.3")

    class RecordingVM(FakeVM):
        def __init__(self, root):
            super().__init__(root)
            self.reset_calls = 0

        async def reset(self):
            self.reset_calls += 1

        def command(self, argv, workspace):
            # ignore the (hanging) agent argv: simulate an ssh that hangs
            return ["sh", "-c", "sleep 30"], Path(workspace)

    vm = RecordingVM(tmp_path / "guest")
    orch, _, _ = run_orch(
        [TaskRequest(kind="agent.run", instructions="loop", args={"sandbox": "vm"})],
        tmp_path, extra={"agent_cli": ClaudeCLI(), "sandbox": vm}, timeout=10.0)
    assert "budget" in orch.tasks["t1"].result.say
    assert vm.reset_calls == 1  # the VM was thrown away on the kill


def test_discard_clears_the_warm_singleton(monkeypatch):
    monkeypatch.setenv("ECHOECHO_SANDBOX", "vm")
    vm_mod._shared_vm = None
    ctx = WorkerContext(workspace=Path("/tmp"))
    warm = for_task(Task(id="t1", request=TaskRequest(kind="agent.run")), ctx)
    assert vm_mod._shared_vm is warm

    async def fake_reset():
        fake_reset.called = True
    fake_reset.called = False
    warm.reset = fake_reset
    asyncio.run(vm_mod.discard(warm))
    assert fake_reset.called
    assert vm_mod._shared_vm is None  # next task re-clones a clean guest


def test_clean_completion_does_not_discard(tmp_path):
    """A normally-finished task must NOT reset the warm VM."""
    calls = {"reset": 0}

    class CountingVM(FakeVM):
        async def reset(self):
            calls["reset"] += 1

    script = write_script(tmp_path / "s.jsonl", [
        {"type": "result", "is_error": False, "session_id": "s", "result": "ok"}])
    run_orch([TaskRequest(kind="agent.run", instructions="x",
                          args={"sandbox": "vm"})],
             tmp_path, extra={"agent_cli": FakeAgentCLI(script),
                              "sandbox": CountingVM(tmp_path / "g")})
    assert calls["reset"] == 0  # no forced kill -> VM kept warm
