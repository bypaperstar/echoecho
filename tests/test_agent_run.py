"""agent.run end-to-end, fully offline: FakeAgentCLI replays recorded
stream-json through `cat`, so the worker's REAL subprocess + line-parse path
runs keyless. Touched-file detection, control-hook writes (subdirs included),
error paths, and the per-CLI adapters."""
import asyncio
import json
from pathlib import Path

from echo_app.bus import TaskRequest
from echo_app.orchestrator.core import Orchestrator, WorkerContext
from echo_app.services import agent_cli
from echo_app.services.agent_cli import ClaudeCLI, CodexCLI, FakeAgentCLI
from echo_app.workers.agent_run import _speakable, run_agent
from echo_app.workers.base import load_all

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"

RUN_OK = [
    {"type": "system", "subtype": "init", "session_id": "sess-1"},
    {"type": "assistant", "message": {"content": [
        {"type": "text", "text": "Reading the current list."}]}},
    {"type": "_write", "file": "notes/plan.md",
     "content": "# Plan\n\n## Goals\n- ship it\n"},
    {"type": "assistant", "message": {"content": [
        {"type": "text", "text": "Wrote the plan."}]}},
    {"type": "result", "subtype": "success", "is_error": False,
     "session_id": "sess-1",
     "result": "Drafted notes/plan.md with a Goals section."},
]


def write_script(tmp_path, events, name="script.jsonl"):
    path = tmp_path / name
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n",
                    encoding="utf-8")
    return path


def run_orch(requests, tmp_path, extra, timeout=5.0, sequential=False):
    """sequential=True drains between submits — touched-file detection diffs
    a per-task snapshot, so overlapping agent runs would cross-attribute
    writes (real dispatches are conversation-paced, not simultaneous)."""
    injections = []
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    orch = Orchestrator(registry=load_all(), on_injection=injections.append,
                        log_path=tmp_path / "tasks.jsonl", workspace=ws)
    orch.ctx.extra.update(extra)

    async def go():
        loop_task = asyncio.ensure_future(orch.run())
        for req in requests:
            orch.submit(req)
            if sequential:
                assert await orch.drain(timeout), "orchestrator did not drain"
        assert await orch.drain(timeout), "orchestrator did not drain in time"
        loop_task.cancel()

    asyncio.run(go())
    return orch, injections, ws


# -- the full loop: dispatch -> replayed agent -> files + speakable result ----

def test_agent_run_end_to_end_offline(tmp_path):
    script = write_script(tmp_path, RUN_OK)
    orch, injections, ws = run_orch(
        [TaskRequest(kind="agent.run", instructions="draft a plan")],
        tmp_path, extra={"agent_cli": FakeAgentCLI(script)})
    task = orch.tasks["t1"]
    assert task.status == "done"
    # the _write control hook landed a real file, in a subdirectory
    assert (ws / "notes" / "plan.md").read_text() == "# Plan\n\n## Goals\n- ship it\n"
    # touched files detected by snapshot diff, not declared by the agent
    assert task.result.artifacts_touched == ["notes/plan.md"]
    assert task.result.say == "Drafted notes/plan.md with a Goals section."
    assert task.result.priority == "interrupt"
    assert task.result.data["session_id"] == "sess-1"
    assert task.result.data["cli"] == "fake"
    assert task.result.data["progress"] == ["Reading the current list.",
                                            "Wrote the plan."]
    # ambient workspace snapshot fired for the touched doc
    assert injections[0].priority == "interrupt"
    snaps = [i for i in injections if i.text.startswith("[workspace]")]
    assert snaps and "notes/plan.md now contains: # Plan" in snaps[0].text


def test_agent_error_result_is_error_interrupt(tmp_path):
    script = write_script(tmp_path, [
        {"type": "system", "subtype": "init", "session_id": "sess-2"},
        {"type": "result", "subtype": "error_during_execution",
         "is_error": True, "session_id": "sess-2",
         "result": "hit the turn limit"},
    ])
    orch, injections, _ = run_orch(
        [TaskRequest(kind="agent.run", instructions="do a thing")],
        tmp_path, extra={"agent_cli": FakeAgentCLI(script)})
    result = orch.tasks["t1"].result
    assert result.data["error"] == "hit the turn limit"
    assert "failed" in result.say
    assert injections[0].priority == "interrupt"


def test_nonzero_exit_is_an_error_result(tmp_path):
    class ExplodingCLI(ClaudeCLI):
        name = "boom"

        def command(self, prompt):
            return ["sh", "-c", "echo not-json; echo doomed >&2; exit 3"]

    orch, _, _ = run_orch(
        [TaskRequest(kind="agent.run", instructions="x")],
        tmp_path, extra={"agent_cli": ExplodingCLI()})
    result = orch.tasks["t1"].result
    assert result.data["error"] == "boom exited 3"
    assert result.data["stderr"].strip() == "doomed"
    assert "failed" in result.say


def test_no_cli_installed_reports_instead_of_crashing(tmp_path, monkeypatch):
    monkeypatch.delenv("ECHO_FAKE_AGENT_SCRIPT", raising=False)
    monkeypatch.setattr(agent_cli.shutil, "which", lambda name: None)
    task_req = TaskRequest(kind="agent.run", instructions="x")
    orch, injections, _ = run_orch([task_req], tmp_path, extra={})
    result = orch.tasks["t1"].result
    assert result.data["error"] == "no agent cli"
    assert injections[0].priority == "interrupt"


# -- FakeAgentCLI fixture mechanics -------------------------------------------

def test_fake_dir_fixtures_consumed_in_order_then_exhausted(tmp_path):
    fixdir = tmp_path / "runs"
    fixdir.mkdir()
    for i, text in enumerate(["first", "second"]):
        write_script(fixdir, [
            {"type": "result", "subtype": "success", "is_error": False,
             "session_id": "s%d" % i, "result": text}], "%02d.jsonl" % i)
    reqs = [TaskRequest(kind="agent.run", instructions="one"),
            TaskRequest(kind="agent.run", instructions="two"),
            TaskRequest(kind="agent.run", instructions="three")]
    orch, _, _ = run_orch(reqs, tmp_path,
                          extra={"agent_cli": FakeAgentCLI(fixdir)},
                          sequential=True)
    says = [orch.tasks[t].result.say for t in ("t1", "t2", "t3")]
    assert says[:2] == ["first", "second"]
    assert "exhausted" in orch.tasks["t3"].result.data["error"]


def test_env_var_selects_shared_fake_runtime(tmp_path, monkeypatch):
    script = write_script(tmp_path, RUN_OK)
    monkeypatch.setenv("ECHO_FAKE_AGENT_SCRIPT", str(script))
    agent_cli._fakes.clear()
    ctx = WorkerContext(workspace=tmp_path)
    runtime = agent_cli.for_ctx(ctx)
    assert isinstance(runtime, FakeAgentCLI)
    assert agent_cli.for_ctx(ctx) is runtime  # cached: dir counters survive
    ctx.extra["agent_cli"] = ClaudeCLI()
    assert agent_cli.for_ctx(ctx) is ctx.extra["agent_cli"]  # injected wins


def test_demo_generic_fixtures_replay(tmp_path):
    """The committed generic-demo fixtures: three agent runs, consumed in
    order — the PLAN-GENERIC gate for rewriting demos as agent.run."""
    fake = FakeAgentCLI(FIXTURES / "agent" / "demo_generic")
    reqs = [TaskRequest(kind="agent.run", instructions=s) for s in
            ("write the proposal", "add goals and budget", "grocery list")]
    orch, _, ws = run_orch(reqs, tmp_path, extra={"agent_cli": fake},
                           sequential=True)
    proposal = (ws / "offsite" / "proposal.md").read_text()
    assert "## Goals" in proposal and "- Team bonding" in proposal
    assert "## Agenda" in proposal and "Day 2" in proposal
    assert (ws / "offsite" / "budget.csv").read_text().startswith("item,")
    assert "## Meals" in (ws / "grocery.md").read_text()
    assert sorted(orch.tasks["t2"].result.artifacts_touched) == [
        "offsite/budget.csv", "offsite/proposal.md"]


# -- per-CLI adapters ----------------------------------------------------------

def test_claude_adapter_normalizes_events():
    cli = ClaudeCLI()
    assert cli.parse_event({"type": "system", "subtype": "init",
                            "session_id": "s"}) == {"event": "init",
                                                    "session_id": "s"}
    assert cli.parse_event({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Edit"},
        {"type": "text", "text": " reading "}]}}) == {"event": "progress",
                                                      "text": "reading"}
    # tool-use-only assistant turns produce no progress noise
    assert cli.parse_event({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Edit"}]}}) is None
    done = cli.parse_event({"type": "result", "subtype": "success",
                            "is_error": False, "session_id": "s",
                            "result": "done"})
    assert done == {"event": "result", "text": "done", "error": False,
                    "session_id": "s"}
    assert cli.parse_event({"type": "user", "message": {}}) is None
    assert "--permission-mode" in cli.command("x")  # tier-1 policy flags


def test_codex_adapter_normalizes_events():
    cli = CodexCLI()
    assert cli.parse_event({"type": "thread.started", "thread_id": "th"}) == {
        "event": "init", "session_id": "th"}
    assert cli.parse_event({"type": "item.completed", "item": {
        "item_type": "agent_message", "text": "did it"}}) == {
        "event": "progress", "text": "did it"}
    assert cli.parse_event({"type": "item.completed", "item": {
        "item_type": "command_execution", "command": "ls"}}) is None
    err = cli.parse_event({"type": "error", "message": "boom"})
    assert err["event"] == "result" and err["error"]
    assert "--sandbox" in cli.command("x")  # tier-1 policy flags


def test_codex_style_run_takes_last_message_as_result(tmp_path):
    """Codex has no result event: exit 0 + progress -> last message speaks."""
    class FakeCodex(CodexCLI):
        def __init__(self, script):
            self.script = script

        def command(self, prompt):
            return ["cat", str(self.script)]

    script = write_script(tmp_path, [
        {"type": "thread.started", "thread_id": "th-1"},
        {"type": "item.completed", "item": {"item_type": "agent_message",
                                            "text": "Working on it."}},
        {"type": "item.completed", "item": {"item_type": "agent_message",
                                            "text": "All done, plan drafted."}},
        {"type": "turn.completed", "usage": {}},
    ])
    orch, _, _ = run_orch(
        [TaskRequest(kind="agent.run", instructions="draft")],
        tmp_path, extra={"agent_cli": FakeCodex(script)})
    result = orch.tasks["t1"].result
    assert result.say == "All done, plan drafted."
    assert result.data["session_id"] == "th-1"


# -- say-line hygiene ----------------------------------------------------------

def test_speakable_squashes_and_caps():
    text = "# Heading\n\n- bullet one\n- bullet two\n\n" + "word " * 100
    line = _speakable(text)
    assert "\n" not in line
    assert len(line) <= 241 and line.endswith("…")
    assert _speakable("short answer") == "short answer"
