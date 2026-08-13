import asyncio
import json

from echoecho_app import config, events
from echoecho_app.bus import TaskRequest, TaskResult
from echoecho_app.orchestrator import log as tasklog
from echoecho_app.orchestrator.core import Orchestrator
from echoecho_app.orchestrator.ranker import rank
from echoecho_app.workers.base import load_all


def run_orch(registry, requests, tmp_path, timeout=3.0):
    injections = []
    orch = Orchestrator(registry=registry, on_injection=injections.append,
                        log_path=tmp_path / "tasks.jsonl", workspace=tmp_path)

    async def go():
        loop_task = asyncio.ensure_future(orch.run())
        for req in requests:
            orch.submit(req)
        assert await orch.drain(timeout), "orchestrator did not drain in time"
        loop_task.cancel()

    asyncio.run(go())
    return orch, injections


def test_dispatch_to_result_roundtrip(tmp_path):
    orch, injections = run_orch(
        load_all(),
        [TaskRequest(kind="sleep.echoecho", instructions="hi", args={"sleep": 0.01})],
        tmp_path)
    task = orch.tasks["t1"]
    assert task.status == "done"
    assert task.result.say == "echoechoing back: hi"
    assert task.finished_at >= task.created_at
    assert len(injections) == 1
    assert injections[0].text == "[task t1 done] echoechoing back: hi"
    assert injections[0].priority == "interrupt"


def test_follow_up_chaining(tmp_path):
    async def worker_a(task, ctx):
        return TaskResult(say="A done", priority="ambient",
                          follow_ups=[TaskRequest(kind="b", instructions="from a")])

    async def worker_b(task, ctx):
        return TaskResult(say="B done (%s)" % task.request.instructions,
                          priority="ambient")

    orch, injections = run_orch({"a": worker_a, "b": worker_b},
                                [TaskRequest(kind="a")], tmp_path)
    assert set(orch.tasks) == {"t1", "t2"}
    t2 = orch.tasks["t2"]
    assert t2.kind == "b"
    assert t2.request.source == "follow_up"
    assert t2.status == "done"
    assert [i.text for i in injections] == \
        ["[task t1 done] A done", "[task t2 done] B done (from a)"]


def test_worker_exception_becomes_error_interrupt(tmp_path):
    async def boom(task, ctx):
        raise RuntimeError("kaput")

    orch, injections = run_orch({"boom": boom}, [TaskRequest(kind="boom")], tmp_path)
    task = orch.tasks["t1"]
    assert task.status == "error"
    assert task.result.data["error"] == "kaput"
    assert injections[0].priority == "interrupt"
    assert "kaput" in injections[0].text


def test_unknown_kind_is_error(tmp_path):
    orch, injections = run_orch({}, [TaskRequest(kind="nope")], tmp_path)
    assert orch.tasks["t1"].status == "error"
    assert injections[0].priority == "interrupt"


def test_silent_results_only_hit_task_table(tmp_path):
    async def quiet(task, ctx):
        return TaskResult(say="", data={"note": "bookkeeping"})

    async def hushed(task, ctx):
        return TaskResult(say="done quietly", priority="silent")

    orch, injections = run_orch({"q": quiet, "h": hushed},
                                [TaskRequest(kind="q"), TaskRequest(kind="h")],
                                tmp_path)
    assert injections == []
    assert all(t.status == "done" for t in orch.tasks.values())
    # still surfaced by check_tasks summaries
    lines = orch.summaries()
    assert any("done quietly" in ln for ln in lines)


def test_workspace_snapshot_fanout_is_capped(tmp_path):
    """An agent touching a whole tree must not flood the conversation with
    one ambient injection per file: cap + one summary line."""
    from echoecho_app.services import artifacts

    names = ["f%02d.md" % i for i in range(8)]
    for n in names:
        artifacts.write_atomic(tmp_path, n, "content of " + n)

    async def sweeper(task, ctx):
        return TaskResult(say="swept", priority="ambient",
                          artifacts_touched=names)

    _, injections = run_orch({"sweep": sweeper}, [TaskRequest(kind="sweep")],
                             tmp_path)
    snaps = [i for i in injections if i.text.startswith("[workspace]")]
    assert len(snaps) == 6  # 5 file snapshots + 1 "and N more" summary
    assert "and 3 more files changed" in snaps[-1].text
    assert all(i.priority == "ambient" for i in snaps)


def test_task_feed_event_carries_artifacts_touched(tmp_path, monkeypatch):
    """The viewer's transcript reads the UI feed, so the terminal task event
    must name the files the task touched."""
    monkeypatch.setattr(config, "WORKSPACE_DIR", tmp_path)

    async def writer(task, ctx):
        return TaskResult(say="wrote", priority="ambient",
                          artifacts_touched=["doc.md", "notes.md"])

    run_orch({"w": writer}, [TaskRequest(kind="w")], tmp_path)
    recs = [json.loads(ln) for ln in
            (tmp_path / events.FEED_NAME).read_text().splitlines()
            if ln.strip()]
    done = [r for r in recs
            if r["type"] == "task" and r.get("status") == "done"]
    assert done and done[0]["artifacts_touched"] == ["doc.md", "notes.md"]


def test_summaries_single_task(tmp_path):
    orch, _ = run_orch(load_all(),
                       [TaskRequest(kind="sleep.echoecho", instructions="x",
                                    args={"sleep": 0.01})], tmp_path)
    # a finished task shows its spoken handle + say-line (PR 11)
    assert orch.summaries("t1") == ["t1 sleep.echoecho 'x': done — echoechoing back: x"]


def test_ranker_heuristics():
    assert rank(TaskResult(say="x", priority="ambient",
                           data={"error": "boom"})) == "interrupt"
    assert rank(TaskResult(say="x", data={"needs_input": True})) == "interrupt"
    assert rank(TaskResult(say="")) == "silent"
    assert rank(TaskResult(say="x", priority="interrupt")) == "interrupt"
    assert rank(TaskResult(say="x", priority="ambient")) == "ambient"
    assert rank(TaskResult(say="x", priority="bogus-tier")) == "ambient"


def test_jsonl_log_and_replay(tmp_path):
    async def worker_a(task, ctx):
        return TaskResult(say="A done", priority="ambient",
                          follow_ups=[TaskRequest(kind="b")],
                          artifacts_touched=["doc.md"])

    async def worker_b(task, ctx):
        return TaskResult(say="B done", priority="ambient")

    run_orch({"a": worker_a, "b": worker_b}, [TaskRequest(kind="a")], tmp_path)
    events = tasklog.replay(tmp_path / "tasks.jsonl")
    seq = [(e["event"], e["task_id"]) for e in events]
    assert seq == [("queued", "t1"), ("done", "t1"), ("queued", "t2"), ("done", "t2")]
    done_t1 = events[1]
    assert done_t1["say"] == "A done"
    assert done_t1["follow_ups"] == ["b"]
    assert done_t1["artifacts_touched"] == ["doc.md"]
    assert events[2]["source"] == "follow_up"
    assert all("ts" in e for e in events)


def test_replay_missing_file(tmp_path):
    assert tasklog.replay(tmp_path / "absent.jsonl") == []
