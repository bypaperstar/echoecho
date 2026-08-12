"""events.py UI feed: parseable JSONL, reset semantics, and the hard rule
that emit()/reset() NEVER raise — a broken feed must never crash the app."""
import json

import pytest

from echo_app import config, events


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WORKSPACE_DIR", tmp_path)
    return tmp_path


def read_feed(workspace):
    path = workspace / events.FEED_NAME
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


def test_emit_writes_parseable_json_lines_with_ts_and_type(workspace):
    events.emit("user_text", text="hello there")
    events.emit("task", task_id="t1", kind="sleep.echo", status="queued")
    recs = read_feed(workspace)
    assert len(recs) == 2
    for rec in recs:
        assert isinstance(rec["ts"], float)
        assert rec["type"]
    assert recs[0] == {"ts": recs[0]["ts"], "type": "user_text",
                       "text": "hello there"}
    assert recs[1]["task_id"] == "t1" and recs[1]["status"] == "queued"


def test_emit_creates_missing_workspace_dir(tmp_path, monkeypatch):
    nested = tmp_path / "sub" / "ws"
    monkeypatch.setattr(config, "WORKSPACE_DIR", nested)
    events.emit("state", frm="IDLE", to="ACTIVE", reason="wake")
    assert read_feed(nested)[0]["to"] == "ACTIVE"


def test_emit_never_raises_with_unwritable_workspace(tmp_path, monkeypatch):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("a plain file where the workspace dir should be")
    # workspace "dir" is a file: mkdir and open both blow up internally
    monkeypatch.setattr(config, "WORKSPACE_DIR", blocker / "ws")
    events.emit("user_text", text="must not raise")
    events.reset(mode="text")  # reset must be equally unkillable
    monkeypatch.setattr(config, "WORKSPACE_DIR", blocker)
    events.emit("user_text", text="still must not raise")


def test_emit_never_raises_on_unserializable_fields(workspace):
    events.emit("weird", obj=object())  # default=str, not a crash
    assert read_feed(workspace)[0]["type"] == "weird"


def test_reset_truncates_and_writes_run_marker(workspace):
    for i in range(5):
        events.emit("user_text", text="old line %d" % i)
    assert len(read_feed(workspace)) == 5
    events.reset(mode="script")
    recs = read_feed(workspace)
    assert len(recs) == 1
    assert recs[0]["type"] == "run" and recs[0]["mode"] == "script"
    events.emit("user_text", text="fresh")  # feed keeps working after reset
    assert [r["type"] for r in read_feed(workspace)] == ["run", "user_text"]


# -- end-to-end: the PR1 smoke script populates the feed --------------------


def test_smoke_script_populates_event_feed():
    """ECHO_TEXT=1 ECHO_FAKE_LLM=1 echo.py --script fixtures/smoke.txt must
    leave a feed telling the whole story in a sane order: run marker, wake,
    the chat, the dispatched task's lifecycle, and the injected result."""
    import os
    import subprocess
    import sys

    env = dict(os.environ, ECHO_TEXT="1", ECHO_FAKE_LLM="1")
    proc = subprocess.run(
        [sys.executable, str(config.REPO_ROOT / "echo.py"),
         "--script", str(config.FIXTURES_DIR / "smoke.txt")],
        capture_output=True, text=True, timeout=60, env=env,
        cwd=str(config.REPO_ROOT))
    assert proc.returncode == 0, proc.stderr
    feed = config.REPO_ROOT / "workspace" / events.FEED_NAME
    recs = [json.loads(ln) for ln in feed.read_text().splitlines()
            if ln.strip()]
    types = [r["type"] for r in recs]
    # run marker first (reset() ran at app start)
    assert types[0] == "run" and recs[0]["mode"] == "script"
    for expected in ("state", "user_text", "assistant_text", "tool_call",
                     "task", "injection"):
        assert expected in types, "missing %r in %s" % (expected, types)

    def first(pred):
        return next(i for i, r in enumerate(recs) if pred(r))

    woke = first(lambda r: r["type"] == "state" and r.get("to") == "ACTIVE")
    dispatched = first(lambda r: r["type"] == "tool_call"
                       and r.get("name") == "dispatch_task")
    queued = first(lambda r: r["type"] == "task"
                   and r.get("status") == "queued")
    done = first(lambda r: r["type"] == "task" and r.get("status") == "done")
    injected = first(lambda r: r["type"] == "injection")
    ended = first(lambda r: r["type"] == "state"
                  and r.get("to") == "ENDING"
                  and r.get("reason") == "end_phrase")
    idle = first(lambda r: r["type"] == "state" and r.get("to") == "IDLE")
    assert woke < dispatched < queued < done < injected < ended < idle
    assert recs[done]["kind"] == "sleep.echo"
    assert recs[done]["priority"] in config.PRIORITIES
    assert recs[injected]["priority"] == recs[done]["priority"]
