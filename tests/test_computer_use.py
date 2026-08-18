"""PR 14 GUI computer-use: the GuiDriver port (keystroke/screenshot building),
the FakeGuiDriver-driven computer.use worker (step sequence + screenshots into
the workspace), error handling, and conditional advertisement. The live
SshGuiDriver (osascript/screencapture over the VM) is Mac-only."""
import asyncio
import json
from pathlib import Path

import pytest

from echoecho_app import config
from echoecho_app.bus import Task, TaskRequest
from echoecho_app.orchestrator.core import Orchestrator, WorkerContext
from echoecho_app.services import gui as gui_mod
from echoecho_app.services.gui import FakeGuiDriver, SshGuiDriver, _keystroke_script
from echoecho_app.workers.base import kinds_enum, load_all


def run_task(args, tmp_path, extra):
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    orch = Orchestrator(registry=load_all(), log_path=tmp_path / "t.jsonl",
                        workspace=ws)
    orch.ctx.extra.update(extra)

    async def go():
        loop = asyncio.ensure_future(orch.run())
        orch.submit(TaskRequest(kind="computer.use", instructions="drive it",
                                args=args))
        assert await orch.drain(5.0)
        loop.cancel()

    asyncio.run(go())
    return orch.tasks["t1"], ws


# -- keystroke script building (the fiddly osascript bit) ---------------------

def test_keystroke_script_variants():
    assert 'keystroke "s" using {command down}' in _keystroke_script("cmd+s")
    assert ("using {command down, shift down}" in _keystroke_script("cmd+shift+4"))
    assert "key code 36" in _keystroke_script("return")      # named key
    assert 'keystroke "a"' in _keystroke_script("a")          # bare char
    assert "System Events" in _keystroke_script("tab")
    with pytest.raises(ValueError):
        _keystroke_script("cmd+nope")   # unknown multi-char key
    with pytest.raises(ValueError):
        _keystroke_script("meta+x")     # unknown modifier
    with pytest.raises(ValueError):
        _keystroke_script("")


def test_type_text_applescript_escaping():
    # SshGuiDriver builds an osascript literal; quotes/backslashes must escape
    from echoecho_app.services.gui import _as_str
    assert _as_str('say "hi"\\') == '"say \\"hi\\"\\\\"'


# -- FakeGuiDriver + computer.use end to end ----------------------------------

STEPS = [
    {"action": "launch", "app": "TextEdit"},
    {"action": "type", "text": "Notes from echoecho"},
    {"action": "key", "combo": "cmd+s"},
    {"action": "key", "combo": "return"},
    {"action": "screenshot", "name": "saved"},
]


def test_computer_use_runs_steps_and_captures_shots(tmp_path):
    fake = FakeGuiDriver(tmp_path / "ws")
    task, ws = run_task({"steps": STEPS}, tmp_path,
                        extra={"gui_driver": fake})
    assert task.status == "done"
    # every step ran, in order
    kinds = [a[0] for a in fake.actions if a[0] != "screenshot"]
    assert kinds == ["launch", "type", "key", "key"]
    assert ("launch", "TextEdit") in fake.actions
    assert ("type", "Notes from echoecho") in fake.actions
    # a screenshot after every step -> 5 shots, all real PNG files in workspace
    shots = task.result.data["screens"]
    assert len(shots) == 5
    for s in shots:
        assert s.startswith("screens/t1/")
        assert (ws / s).read_bytes().startswith(b"\x89PNG")
    # the named screenshot step used its name
    assert "screens/t1/saved.png" in shots
    assert task.result.artifacts_touched == shots
    assert "5 on-screen steps" in task.result.say


def test_computer_use_opens_only_workspace_relative_files(tmp_path):
    fake = FakeGuiDriver(tmp_path / "ws")
    task, _ = run_task({"steps": [{"action": "open", "path": "notes/today.md"}]},
                       tmp_path, extra={"gui_driver": fake})
    assert task.status == "done"
    assert ("open", "notes/today.md", "TextEdit") in fake.actions
    assert task.result.data["completed"] == ["opened notes/today.md"]

    rejected, _ = run_task({"steps": [{"action": "open", "path": "../private.md"}]},
                           tmp_path, extra={"gui_driver": FakeGuiDriver(tmp_path / "ws")})
    assert "workspace-relative" in rejected.result.data["error"]


def test_computer_use_stops_at_failing_step_keeps_shots(tmp_path):
    task, ws = run_task(
        {"steps": [{"action": "launch", "app": "TextEdit"},
                   {"action": "bogus"},                     # unknown action
                   {"action": "type", "text": "never runs"}]},
        tmp_path, extra={"gui_driver": FakeGuiDriver(tmp_path / "ws")})
    assert task.status == "done"  # a bad step is reported, not a crash
    assert task.result.data["error"]
    assert "Stopped on step 2" in task.result.say
    # the first step's shot was still captured
    assert task.result.data["screens"] == ["screens/t1/step00.png"]


def test_computer_use_no_driver_is_clean_error(tmp_path, monkeypatch):
    monkeypatch.delenv("ECHOECHO_SANDBOX", raising=False)  # no vm -> no driver
    task, _ = run_task({"steps": STEPS}, tmp_path, extra={})
    assert task.result.data["error"] == "no gui driver"
    assert "VM" in task.result.say


def test_computer_use_rejects_empty_and_oversized(tmp_path):
    fake = FakeGuiDriver(tmp_path / "ws")
    task, _ = run_task({"steps": []}, tmp_path, extra={"gui_driver": fake})
    assert task.result.data["error"] == "no steps"
    big = [{"action": "wait", "seconds": 0}] * 41
    task2, _ = run_task({"steps": big}, tmp_path, extra={"gui_driver": fake})
    assert task2.result.data["error"] == "too many steps"


# -- driver selection + advertisement -----------------------------------------

def test_for_ctx_uses_injected_then_vm(tmp_path, monkeypatch):
    ctx = WorkerContext(workspace=tmp_path)
    monkeypatch.delenv("ECHOECHO_SANDBOX", raising=False)
    assert gui_mod.for_ctx(ctx) is None            # no vm, no driver
    fake = FakeGuiDriver(tmp_path)
    ctx.extra["gui_driver"] = fake
    assert gui_mod.for_ctx(ctx) is fake            # injected wins

    from echoecho_app.services.vm import LumeVM
    ctx2 = WorkerContext(workspace=tmp_path)
    ctx2.extra["sandbox"] = LumeVM(vm_name="echoecho-vm")
    drv = gui_mod.for_ctx(ctx2)
    assert isinstance(drv, SshGuiDriver)           # a LumeVM -> ssh driver


def test_computer_use_advertised_only_with_vm(monkeypatch):
    load_all()
    monkeypatch.delenv("ECHOECHO_SANDBOX", raising=False)
    monkeypatch.delenv("ECHOECHO_PLUGINS", raising=False)
    assert "computer.use" not in kinds_enum()      # hidden by default
    assert "computer.use" in load_all()            # but dispatchable
    monkeypatch.setenv("ECHOECHO_SANDBOX", "vm")
    assert "computer.use" in kinds_enum()          # advertised with the vm tier


# -- SshGuiDriver builds the right guest commands (no VM needed) ---------------

def test_ssh_gui_driver_builds_guest_commands(monkeypatch, tmp_path):
    from echoecho_app.services.vm import LumeVM
    monkeypatch.setenv("ECHOECHO_VM_GUEST_WORKSPACE", "/Volumes/Shared/ws")
    vm = LumeVM(vm_name="echoecho-vm")
    vm.ip = "10.0.0.9"
    driver = SshGuiDriver(vm, tmp_path)
    sent = {}

    async def fake_run(argv, capture=False):
        sent.setdefault("argvs", []).append(argv)
        return b""
    driver._run = fake_run

    asyncio.run(driver.launch("Safari"))
    asyncio.run(driver.open_file("notes/today.md"))
    asyncio.run(driver.key("cmd+t"))
    asyncio.run(driver.screenshot("screens/t1/a.png"))
    argvs = sent["argvs"]
    assert argvs[0] == ["open", "-a", "Safari"]
    assert argvs[1] == ["open", "-a", "TextEdit", "/Volumes/Shared/ws/notes/today.md"]
    assert argvs[2][0] == "osascript" and "command down" in argvs[2][-1]
    # screenshot targets the guest mount path so the PNG lands on the host
    assert "screencapture" in argvs[3][-1]
    assert "/Volumes/Shared/ws/screens/t1/a.png" in argvs[3][-1]


def test_ssh_gui_driver_run_times_out_instead_of_hanging(monkeypatch, tmp_path):
    """A keystroke blocked on an Accessibility TCC prompt never returns; the
    driver must bound it and raise, not hang the whole task. Simulated with a
    real sleeping subprocess in place of ssh."""
    from echoecho_app.services import gui as gm
    from echoecho_app.services.vm import LumeVM
    monkeypatch.setattr(gm, "GUI_TIMEOUT", 0.3)
    vm = LumeVM(vm_name="echoecho-vm")
    vm.ip = "10.0.0.9"
    vm.ssh_argv = lambda remote: ["sh", "-c", "sleep 30"]  # "hung" guest cmd
    driver = SshGuiDriver(vm, tmp_path)
    import time
    t0 = time.monotonic()
    with pytest.raises(gm.GuiError) as ei:
        asyncio.run(driver.key("cmd+s"))
    assert time.monotonic() - t0 < 5.0  # bounded, not a 30s hang
    assert "Accessibility" in str(ei.value)


def test_computer_use_prepares_a_cold_vm_before_stepping(tmp_path):
    """A cold daemon's first computer.use must boot the VM itself (the silent
    Mac playtest caught 'VM not prepared'): a driver exposing .vm.prepare()
    gets prepared once before any step; a prepare failure is a clean spoken
    error, not a crash."""
    class PreparedFake(FakeGuiDriver):
        def __init__(self, ws):
            super().__init__(ws)
            outer = self

            class Vm:
                prepared = 0

                async def prepare(self):
                    Vm.prepared += 1
                    outer.prepared_before_steps = not outer.actions
            self.vm = Vm()

    fake = PreparedFake(tmp_path / "ws")
    task, _ = run_task({"steps": [{"action": "launch", "app": "TextEdit"}]},
                       tmp_path, extra={"gui_driver": fake})
    assert task.status == "done"
    assert type(fake.vm).prepared == 1
    assert fake.prepared_before_steps

    class BrokenVmFake(FakeGuiDriver):
        def __init__(self, ws):
            super().__init__(ws)

            class Vm:
                async def prepare(self):
                    raise RuntimeError("no golden image")
            self.vm = Vm()

    broken = BrokenVmFake(tmp_path / "ws")
    task, _ = run_task({"steps": [{"action": "launch", "app": "TextEdit"}]},
                       tmp_path, extra={"gui_driver": broken})
    assert task.status == "done"  # a reported failure, not a worker crash
    assert "couldn't start my Mac VM" in task.result.say
    assert broken.actions == []  # never stepped without a VM
