"""PR 11 long-task plumbing: throttled progress injections, the needs_input
voice round-trip, resume/steering by task_id, task-table persistence with
orphan re-attach, wall-clock budgets, and check_tasks elapsed/progress."""
import asyncio
import json
import time
from pathlib import Path

from echoecho_app import config
from echoecho_app.bus import TaskRequest, TaskResult
from echoecho_app.orchestrator import log as tasklog
from echoecho_app.orchestrator.core import Orchestrator, _elapsed, _title
from echoecho_app.services.agent_cli import ClaudeCLI, FakeAgentCLI
from echoecho_app.workers.base import load_all


def write_script(path, events):
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n",
                    encoding="utf-8")
    return path


def run_orch(requests, tmp_path, extra=None, timeout=5.0, seed_log=None):
    injections = []
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    log_path = tmp_path / "tasks.jsonl"
    if seed_log is not None:
        log_path.write_text(seed_log, encoding="utf-8")
    orch = Orchestrator(registry=load_all(), on_injection=injections.append,
                        log_path=log_path, workspace=ws)
    interrupted = orch.rehydrate()
    orch.ctx.extra.update(extra or {})

    async def go():
        loop_task = asyncio.ensure_future(orch.run())
        for req in requests:
            orch.submit(req)
            assert await orch.drain(timeout), "did not drain"
        loop_task.cancel()

    asyncio.run(go())
    return orch, injections, interrupted


# -- progress streaming -> throttled ambient injections -----------------------

def test_progress_streams_first_line_then_throttles(tmp_path, monkeypatch):
    monkeypatch.setenv("ECHOECHO_PROGRESS_INTERVAL", "999")  # only the first fires
    script = write_script(tmp_path / "s.jsonl", [
        {"type": "system", "subtype": "init", "session_id": "s1"},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "reading clause 7"}]}},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "drafting the summary"}]}},
        {"type": "result", "subtype": "success", "is_error": False,
         "session_id": "s1", "result": "done reviewing the lease"},
    ])
    orch, injections, _ = run_orch(
        [TaskRequest(kind="agent.run", instructions="review the lease")],
        tmp_path, extra={"agent_cli": FakeAgentCLI(script)})
    prog = [i for i in injections if "progress]" in i.text]
    assert len(prog) == 1  # first heartbeat only; second throttled
    assert "reading clause 7" in prog[0].text
    assert prog[0].priority == "ambient"
    # but check_tasks still reflects the LATEST progress line
    task = orch.tasks["t1"]
    assert task.progress == "drafting the summary"


def test_check_tasks_shows_elapsed_and_handle_while_running(tmp_path):
    injections = []
    orch = Orchestrator(registry=None,  # set inside go(): needs the loop
                        on_injection=injections.append,
                        log_path=tmp_path / "t.jsonl", workspace=tmp_path)

    async def go():
        started, release = asyncio.Event(), asyncio.Event()

        async def slow(task, ctx):
            ctx.report("halfway through")
            started.set()
            await release.wait()
            return TaskResult(say="done", priority="interrupt")

        orch.registry = {"agent.run": slow}
        loop = asyncio.ensure_future(orch.run())
        t = orch.submit(TaskRequest(kind="agent.run",
                                    instructions="review the whole lease"))
        t.created_at = time.time() - 5  # pretend it's been going 5s
        await started.wait()
        lines = orch.summaries("t1")
        release.set()
        await orch.drain()
        loop.cancel()
        return lines

    lines = asyncio.run(go())
    assert "review the whole lease" in lines[0]  # spoken handle, not raw id
    assert "running" in lines[0] and "elapsed" in lines[0]
    assert "halfway through" in lines[0]


def test_elapsed_and_title_helpers():
    assert _elapsed(5) == "5s"
    assert _elapsed(75) == "1m15s"
    assert _elapsed(3725) == "1h02m"
    assert _title("write a one page proposal for the offsite") \
        == "write a one page proposal…"
    assert _title("short ask") == "short ask"


# -- needs_input voice round-trip ---------------------------------------------

def test_agent_question_becomes_needs_input_interrupt(tmp_path):
    script = write_script(tmp_path / "q.jsonl", [
        {"type": "system", "subtype": "init", "session_id": "sq"},
        {"type": "result", "subtype": "success", "is_error": False,
         "session_id": "sq",
         "result": "I can format the report two ways.\n"
                   "QUESTION: Do you want it as a table or prose?"},
    ])
    orch, injections, _ = run_orch(
        [TaskRequest(kind="agent.run", instructions="write the report")],
        tmp_path, extra={"agent_cli": FakeAgentCLI(script)})
    result = orch.tasks["t1"].result
    assert result.data["needs_input"] is True
    assert "table or prose" in result.say
    assert injections[0].priority == "interrupt"  # a question interrupts
    # the prompt taught the agent the convention
    from echoecho_app.workers import agent_run
    assert agent_run.QUESTION_PREFIX in agent_run.PROMPT_SUFFIX


# -- resume / steering by task_id ---------------------------------------------

def test_task_id_resumes_the_prior_agent_session(tmp_path):
    first = write_script(tmp_path / "a.jsonl", [
        {"type": "system", "subtype": "init", "session_id": "sess-lease"},
        {"type": "result", "subtype": "success", "is_error": False,
         "session_id": "sess-lease", "result": "reviewed the lease"},
    ])
    second = write_script(tmp_path / "b.jsonl", [
        {"type": "result", "subtype": "success", "is_error": False,
         "session_id": "sess-lease", "result": "added a budget section"},
    ])
    ws = tmp_path / "ws"
    ws.mkdir()
    fake = FakeAgentCLI(first)
    orch = Orchestrator(registry=load_all(), log_path=tmp_path / "t.jsonl",
                        workspace=ws)
    orch.ctx.extra["agent_cli"] = fake

    async def go():
        loop = asyncio.ensure_future(orch.run())
        orch.submit(TaskRequest(kind="agent.run", instructions="review lease"))
        assert await orch.drain()
        fake.source = second  # the "resumed" run replays a different script
        orch.submit(TaskRequest(kind="agent.run",
                                instructions="add a budget section too",
                                args={"task_id": "t1"}))
        assert await orch.drain()
        loop.cancel()

    asyncio.run(go())
    assert orch.tasks["t1"].session_id == "sess-lease"
    # the second run was invoked with --resume on that session
    _, resume = fake.resumes[-1]
    assert resume == "sess-lease"
    assert orch.tasks["t2"].result.say == "added a budget section"


def test_serialized_kind_runs_one_at_a_time_in_order(tmp_path):
    # agent.run is serialize=True: two live dispatches editing the same doc
    # must run in dispatch order, never concurrently (the later dispatch
    # carries the later intent and must win the file)
    trace = []

    async def slow(task, ctx):
        trace.append(("start", task.id))
        await asyncio.sleep(0.05)
        trace.append(("end", task.id))
        return TaskResult(say="ok")
    slow.serialize = True

    orch = Orchestrator(registry={"slow.edit": slow},
                        log_path=tmp_path / "t.jsonl",
                        workspace=tmp_path)

    async def go():
        loop = asyncio.ensure_future(orch.run())
        orch.submit(TaskRequest(kind="slow.edit", instructions="draft"))
        orch.submit(TaskRequest(kind="slow.edit", instructions="revise"))
        assert await orch.drain()
        loop.cancel()

    asyncio.run(go())
    assert trace == [("start", "t1"), ("end", "t1"),
                     ("start", "t2"), ("end", "t2")]
    from echoecho_app.workers import agent_run, doc_edit
    assert agent_run.run_agent.serialize == "workspace.write"
    assert doc_edit.run.serialize == "workspace.write"  # shared lock group


def test_resume_unknown_task_id_runs_fresh_instead_of_refusing(tmp_path):
    # Voice models invent label-like ids for NEW work; an unknown id must
    # start a fresh run (no --resume), never a refusal loop.
    fake = FakeAgentCLI(write_script(tmp_path / "s.jsonl", [
        {"type": "result", "is_error": False, "result": "wrote the doc"}]))
    orch, _, _ = run_orch(
        [TaskRequest(kind="agent.run", instructions="write the doc",
                     args={"task_id": "speech_update_v2"})],
        tmp_path, extra={"agent_cli": fake})
    result = orch.tasks["t1"].result
    assert result.say == "wrote the doc"
    assert not result.data.get("error")
    _, resume = fake.resumes[-1]
    assert resume is None  # fresh session, not a resume


def test_resume_unfinished_task_is_a_clean_refusal(tmp_path):
    # a genuinely known-but-still-running task still refuses politely
    fake = FakeAgentCLI(write_script(tmp_path / "s.jsonl", [
        {"type": "result", "is_error": False, "result": "x"}]))

    class Running:
        status = "running"
        title = "the lease review"
        session_id = None
        result = None

    orch, _, _ = run_orch(
        [TaskRequest(kind="agent.run", instructions="steer it",
                     args={"task_id": "t7"})],
        tmp_path, extra={"agent_cli": fake, "tasks": {"t7": Running()}})
    result = orch.tasks["t1"].result
    assert "still running" in result.say
    assert result.data["error"] == "task t7 still running"


# -- persistence + orphan re-attach -------------------------------------------

def test_rehydrate_rebuilds_table_and_marks_orphans_interrupted(tmp_path):
    # a log left behind by a previous run: t1 finished, t2 was mid-flight
    # with a checkpointed agent session, t3 only queued
    log = "\n".join(json.dumps(e) for e in [
        {"ts": 100.0, "event": "queued", "task_id": "t1", "kind": "agent.run",
         "instructions": "review the lease", "source": "user"},
        {"ts": 101.0, "event": "session", "task_id": "t1",
         "session_id": "sess-1"},
        {"ts": 160.0, "event": "done", "task_id": "t1", "kind": "agent.run",
         "say": "reviewed the lease", "priority": "interrupt",
         "session_id": "sess-1"},
        {"ts": 200.0, "event": "queued", "task_id": "t2", "kind": "agent.run",
         "instructions": "summarize the deck", "source": "user"},
        {"ts": 201.0, "event": "session", "task_id": "t2",
         "session_id": "sess-2"},
        {"ts": 300.0, "event": "queued", "task_id": "t3", "kind": "agent.run",
         "instructions": "book a flight", "source": "user"},
    ]) + "\n"
    orch, _, interrupted = run_orch([], tmp_path, seed_log=log)
    assert orch.tasks["t1"].status == "done"
    assert orch.tasks["t1"].session_id == "sess-1"
    assert orch.tasks["t1"].title == "review the lease"
    # t2 + t3 were mid-flight at shutdown -> interrupted errors
    assert {t.id for t in interrupted} == {"t2", "t3"}
    assert orch.tasks["t2"].status == "error"
    assert orch.tasks["t2"].session_id == "sess-2"  # resumable
    assert "resumed" in orch.tasks["t2"].result.say
    assert "resumed" not in orch.tasks["t3"].result.say  # no session
    # the interruption was logged (so a further restart is idempotent)
    evs = tasklog.replay(tmp_path / "tasks.jsonl")
    assert sum(1 for e in evs if e["event"] == "error"
               and e["task_id"] == "t2") == 1
    # the id counter resumes past the highest seen id: next submit -> t4
    assert orch._seq == 3


def test_rehydrate_survives_a_malformed_log_line(tmp_path):
    log = (json.dumps({"ts": 1.0, "event": "queued", "task_id": "t1",
                       "kind": "agent.run", "instructions": "x"}) + "\n"
           + "{ this is not json\n"
           + json.dumps({"ts": 2.0, "event": "done", "task_id": "t1",
                         "kind": "agent.run", "say": "ok",
                         "priority": "ambient"}) + "\n")
    orch, _, _ = run_orch([], tmp_path, seed_log=log)
    assert orch.tasks["t1"].status == "done"


def test_resume_after_restart_uses_persisted_session(tmp_path):
    """The full loop: a task's session is logged, a fresh orchestrator
    rehydrates it, and a task_id resume reaches the persisted session."""
    log = "\n".join(json.dumps(e) for e in [
        {"ts": 1.0, "event": "queued", "task_id": "t1", "kind": "agent.run",
         "instructions": "review the lease", "source": "user"},
        {"ts": 2.0, "event": "done", "task_id": "t1", "kind": "agent.run",
         "say": "reviewed", "priority": "interrupt", "session_id": "sess-x"},
    ]) + "\n"
    resumed = write_script(tmp_path / "r.jsonl", [
        {"type": "result", "is_error": False, "session_id": "sess-x",
         "result": "extended the review"}])
    fake = FakeAgentCLI(resumed)
    orch, _, _ = run_orch(
        [TaskRequest(kind="agent.run", instructions="add more",
                     args={"task_id": "t1"})],
        tmp_path, extra={"agent_cli": fake}, seed_log=log)
    assert orch.tasks["t2"].result.say == "extended the review"
    assert fake.resumes[-1][1] == "sess-x"  # reached the persisted session


# -- announcement watermark: announce once, even across a restart -------------

def test_collect_missed_announces_once_and_survives_restart(tmp_path):
    """A task that finished while echoecho was away announces on the next wake,
    and having a restart between the finish and the wake must not lose it —
    nor announce it twice."""
    log = "\n".join(json.dumps(e) for e in [
        {"ts": 1.0, "event": "queued", "task_id": "t1", "kind": "agent.run",
         "instructions": "review the lease", "source": "user"},
        {"ts": 2.0, "event": "done", "task_id": "t1", "kind": "agent.run",
         "say": "reviewed the lease", "priority": "interrupt"},
    ]) + "\n"
    orch, _, _ = run_orch([], tmp_path, seed_log=log)
    missed = orch.collect_missed()
    assert len(missed) == 1 and "reviewed the lease" in missed[0]
    assert orch.collect_missed() == []  # marked announced -> not repeated
    # a SECOND restart re-reads the log incl. the persisted 'announced' row
    orch2, _, _ = run_orch([], tmp_path)
    assert orch2.collect_missed() == []  # still not re-announced


def test_interrupted_task_surfaces_via_collect_missed(tmp_path):
    log = (json.dumps({"ts": 1.0, "event": "queued", "task_id": "t1",
                       "kind": "agent.run", "instructions": "review the lease",
                       "source": "user"}) + "\n"
           + json.dumps({"ts": 2.0, "event": "session", "task_id": "t1",
                         "session_id": "sess-1"}) + "\n")
    orch, _, interrupted = run_orch([], tmp_path, seed_log=log)
    assert [t.id for t in interrupted] == ["t1"]
    missed = orch.collect_missed()
    assert len(missed) == 1 and "interrupted by a restart" in missed[0]
    assert orch.collect_missed() == []


def test_live_result_is_marked_announced_not_re_surfaced(tmp_path):
    """A result delivered to an ACTIVE session (live=True) counts as spoken,
    so the next wake does not re-announce it; a result finishing while IDLE
    (live=False) does surface on the next wake."""
    injections = []
    orch = Orchestrator(registry=None, on_injection=injections.append,
                        log_path=tmp_path / "t.jsonl", workspace=tmp_path)

    async def quick(task, ctx):
        return TaskResult(say="done " + task.request.instructions,
                          priority="interrupt")

    async def go():
        orch.registry = {"agent.run": quick}
        loop = asyncio.ensure_future(orch.run())
        orch.live = True  # ACTIVE session
        orch.submit(TaskRequest(kind="agent.run", instructions="live one"))
        assert await orch.drain()
        orch.live = False  # session ended
        orch.submit(TaskRequest(kind="agent.run", instructions="idle one"))
        assert await orch.drain()
        loop.cancel()

    asyncio.run(go())
    missed = orch.collect_missed()
    assert len(missed) == 1  # only the idle-finished task waits for the wake
    assert "idle one" in missed[0]


def test_summaries_without_id_is_bounded_to_active_plus_recent(tmp_path):
    """check_tasks over a long-lived persisted table returns active tasks +
    only the most recent finished ones, never a growing wall of history."""
    lines = []
    for i in range(1, 26):  # 25 finished tasks in the log
        lines.append(json.dumps({"ts": float(i), "event": "queued",
                                 "task_id": "t%d" % i, "kind": "agent.run",
                                 "instructions": "task %d" % i,
                                 "source": "user"}))
        lines.append(json.dumps({"ts": float(i) + 0.5, "event": "done",
                                 "task_id": "t%d" % i, "kind": "agent.run",
                                 "say": "did %d" % i, "priority": "ambient"}))
    orch, _, _ = run_orch([], tmp_path, seed_log="\n".join(lines) + "\n")
    summ = orch.summaries()
    assert len(summ) == 10  # RECENT_SUMMARIES, not 25
    # the most recent finished tasks, not the oldest
    assert any("did 25" in s for s in summ)
    assert not any("did 1'" in s or ": did 1 " in s for s in summ)
    # a specific id is always available regardless of the recency window
    assert orch.summaries("t3") == ["t3 agent.run 'task 3': done — did 3"]


# -- wall-clock budget --------------------------------------------------------

def test_budget_breach_kills_and_reports_resumable(tmp_path, monkeypatch):
    monkeypatch.setenv("ECHOECHO_AGENT_TIMEOUT", "0.3")

    class HangCLI(ClaudeCLI):
        name = "hang"

        def command(self, prompt, resume=None):
            # emit a session id, then hang well past the budget
            return ["sh", "-c",
                    "printf '{\"type\": \"system\", \"subtype\": \"init\", "
                    "\"session_id\": \"sess-hang\"}\\n'; sleep 30"]

    t0 = time.monotonic()
    orch, injections, _ = run_orch(
        [TaskRequest(kind="agent.run", instructions="loop forever")],
        tmp_path, extra={"agent_cli": HangCLI()}, timeout=10.0)
    assert time.monotonic() - t0 < 5.0  # killed at the budget, not hung
    result = orch.tasks["t1"].result
    assert "budget" in result.say and "resumed" in result.say
    assert result.data["error"].startswith("hit the")
    assert result.data["session_id"] == "sess-hang"  # checkpointed -> resumable
    assert injections[0].priority == "interrupt"


def test_budget_kill_takes_the_whole_process_tree(tmp_path, monkeypatch):
    """A real agent spawns children (builds, dev servers); the budget kill
    must take the process GROUP, not just the CLI, or orphans outlive it."""
    monkeypatch.setenv("ECHOECHO_AGENT_TIMEOUT", "0.3")
    marker = tmp_path / "grandchild_alive"

    class ForkingCLI(ClaudeCLI):
        name = "forking"

        def command(self, prompt, resume=None):
            # a backgrounded grandchild that touches a marker AFTER the budget
            # would have elapsed, then the foreground process hangs. A single
            # -PID kill of the shell leaves the grandchild to write the marker;
            # a process-group kill takes it out first.
            return ["sh", "-c",
                    "(sleep 1.5; touch %s) & sleep 30" % marker]

    orch, _, _ = run_orch(
        [TaskRequest(kind="agent.run", instructions="build the world")],
        tmp_path, extra={"agent_cli": ForkingCLI()}, timeout=10.0)
    assert "budget" in orch.tasks["t1"].result.say
    time.sleep(2.0)  # well past the grandchild's 1.5s timer
    assert not marker.exists(), "grandchild survived the budget kill (orphaned)"
