"""PR 6: daemon hardening + demo polish.

Chaos: kill the FakeTransport mid-session -> reconnect with backoff, or (no
factory / attempts exhausted) a clean return to IDLE with the wake loop
re-armed. Plus: '[since last session]' wake injection, code_stub PATH-gated
registration, prompt tuning, viewer section-flash assets.
Sync tests calling asyncio.run internally (no pytest-asyncio).
"""
import asyncio
import importlib
import os
import stat
from pathlib import Path

from echo_app import config
from echo_app.bus import Task, TaskRequest, TaskResult
from echo_app.conversation.realtime import (FakeTransport, RealtimeClient,
                                            VOICE_PROMPT)
from echo_app.conversation.session import Session
from echo_app.orchestrator.core import Orchestrator, WorkerContext


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def kill(transport):
    """Simulate the WS dying mid-session: next recv/send raises TransportClosed."""
    transport.closed = True


END_TAIL = [
    {"type": "conversation.item.input_audio_transcription.completed",
     "item_id": "u9", "transcript": "that's it"},
    {"type": "response.done", "response": {"id": "r9", "output": []}},
]


def system_texts(sent):
    return [e["item"]["content"][0]["text"] for e in sent
            if e.get("type") == "conversation.item.create"
            and e.get("item", {}).get("role") == "system"]


# -- chaos: transport killed mid-session ---------------------------------------


def test_chaos_kill_mid_session_returns_idle_with_wake_rearmed():
    """No transport_factory: a dead WS must NOT hang or crash the daemon —
    the session ends cleanly and the wake loop is re-armed."""
    rearmed = []
    transport = FakeTransport([
        {"type": "session.created", "session": {"id": "s"}},
        {"type": "response.done", "response": {"id": "r1", "output": []}},
        {"type": "_kill"},
    ])
    transport.hooks["_kill"] = lambda ev: kill(transport)
    session = Session(clock=FakeClock(), silence_timeout=600,
                      wake_resume=lambda: rearmed.append(1))
    client = RealtimeClient(transport, session=session, poll_interval=0.01,
                            out=lambda *_: None)
    asyncio.run(client.run())
    assert session.state == "IDLE"
    assert session.end_reason == "transport_closed"
    assert rearmed == [1]  # wake feed resumed exactly once


def test_chaos_kill_then_reconnect_via_factory_session_survives():
    """With a transport_factory the client reconnects (with backoff), replays
    the [reconnected] context item, and the session continues to a normal end."""
    second = FakeTransport(list(END_TAIL))
    first = FakeTransport([
        {"type": "session.created", "session": {"id": "s"}},
        {"type": "response.done", "response": {"id": "r1", "output": []}},
        {"type": "_kill"},
    ])
    first.hooks["_kill"] = lambda ev: kill(first)
    session = Session(clock=FakeClock(), silence_timeout=600)
    client = RealtimeClient(first, session=session, poll_interval=0.01,
                            out=lambda *_: None, reconnect_backoff=0.01,
                            transport_factory=lambda: second)
    asyncio.run(client.run())
    assert client._reconnects == 1
    assert second.sent_types()[0] == "session.update"  # fresh session.update
    assert any(t.startswith("[reconnected]") for t in system_texts(second.sent))
    assert session.end_reason == "end_phrase"  # session survived the kill
    assert session.state == "IDLE"
    assert second.closed


def test_chaos_reconnect_attempts_exhausted_ends_cleanly():
    """Factory keeps producing dead transports: after max_reconnects the
    client gives up and still lands in a clean IDLE (daemon re-arms wake)."""
    rearmed = []

    def dead_factory():
        t = FakeTransport([])
        t.closed = True  # connect-time send fails immediately
        return t

    transport = FakeTransport([{"type": "session.created", "session": {"id": "s"}},
                               {"type": "_kill"}])
    transport.hooks["_kill"] = lambda ev: kill(transport)
    session = Session(clock=FakeClock(), silence_timeout=600,
                      wake_resume=lambda: rearmed.append(1))
    client = RealtimeClient(transport, session=session, poll_interval=0.01,
                            out=lambda *_: None, reconnect_backoff=0.001,
                            max_reconnects=2, transport_factory=dead_factory)
    asyncio.run(client.run())
    assert client._reconnects == 2  # tried, with backoff, then gave up
    assert session.state == "IDLE"
    assert session.end_reason == "transport_closed"
    assert rearmed == [1]


def test_no_reconnect_when_session_already_ending():
    """A transport death during ENDING must not reconnect — just finish."""
    calls = []
    transport = FakeTransport([
        {"type": "session.created", "session": {"id": "s"}},
        {"type": "conversation.item.input_audio_transcription.completed",
         "item_id": "u1", "transcript": "that's it"},
        {"type": "_kill"},
    ])
    transport.hooks["_kill"] = lambda ev: kill(transport)
    session = Session(clock=FakeClock(), silence_timeout=600)
    client = RealtimeClient(transport, session=session, poll_interval=0.01,
                            out=lambda *_: None, reconnect_backoff=0.001,
                            transport_factory=lambda: calls.append(1) or FakeTransport([]))
    asyncio.run(client.run())
    assert calls == []  # factory never used
    assert session.state == "IDLE"


def test_ws_transport_wraps_connection_closed_on_send_and_recv():
    """A WS death must surface as TransportClosed from BOTH send and recv, or
    the client's reconnect path never triggers on the real transport. (Also
    guards the websockets-15 lazy-import gotcha: `websockets.exceptions` is
    only importable as a submodule, so a raw ConnectionClosed inside the
    except clause would otherwise become an AttributeError.)"""
    import pytest
    import websockets.exceptions

    from echo_app.conversation.realtime import (TransportClosed,
                                                WebSocketTransport)

    class DeadWS:
        async def send(self, data):
            raise websockets.exceptions.ConnectionClosedError(None, None)

        async def recv(self):
            raise websockets.exceptions.ConnectionClosedOK(None, None)

    tr = WebSocketTransport("gpt-realtime-2.1-mini", api_key="x")
    tr._ws = DeadWS()
    with pytest.raises(TransportClosed):
        asyncio.run(tr.send({"type": "session.update"}))
    with pytest.raises(TransportClosed):
        asyncio.run(tr.recv())


# -- "[since last session]" wake injection --------------------------------------


def test_since_last_session_item_sent_right_after_session_update():
    text = ("[since last session] Background tasks finished while Echo was "
            "asleep: t1 (recipe.search): Found a pad thai.")
    transport = FakeTransport(list(END_TAIL))
    client = RealtimeClient(transport, session=Session(clock=FakeClock()),
                            poll_interval=0.01, out=lambda *_: None,
                            since_last_session=text)
    asyncio.run(client.run())
    assert transport.sent_types()[0] == "session.update"
    assert system_texts(transport.sent)[0] == text


def test_no_since_item_when_nothing_missed():
    transport = FakeTransport(list(END_TAIL))
    client = RealtimeClient(transport, session=Session(clock=FakeClock()),
                            poll_interval=0.01, out=lambda *_: None)
    asyncio.run(client.run())
    assert system_texts(transport.sent) == []


def test_orchestrator_results_since():
    orch = Orchestrator(registry={})
    t1 = Task(id="t1", request=TaskRequest(kind="recipe.search"))
    t1.result = TaskResult(say="Found a pad thai.", priority="interrupt")
    t1.finished_at = 100.0
    t2 = Task(id="t2", request=TaskRequest(kind="grocery.merge"))
    t2.result = TaskResult(say="", priority="silent")  # silent: excluded
    t2.finished_at = 150.0
    t3 = Task(id="t3", request=TaskRequest(kind="doc.edit"))  # still running
    orch.tasks = {"t1": t1, "t2": t2, "t3": t3}
    assert orch.results_since(50.0) == [
        "t1 (recipe.search): Found a pad thai."]
    assert orch.results_since(120.0) == []  # t1 finished before the marker


# -- code_stub stretch worker -----------------------------------------------------


def _reload_code_stub():
    import echo_app.workers.code_stub as cs
    return importlib.reload(cs)


def test_code_stub_registers_only_if_cli_on_path(tmp_path):
    from echo_app.workers.base import REGISTRY
    old_path = os.environ.get("PATH", "")
    try:
        # no CLIs on PATH -> not registered
        REGISTRY.pop("code", None)
        os.environ["PATH"] = str(tmp_path / "nowhere")
        cs = _reload_code_stub()
        assert cs.find_cli() is None
        assert "code" not in REGISTRY
        # fake `codex` on PATH -> registered, and run_code shells out to it
        fake = tmp_path / "codex"
        fake.write_text("#!/bin/sh\necho \"patched hello.py in $PWD\"\n")
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        os.environ["PATH"] = str(tmp_path)
        cs = _reload_code_stub()
        assert cs.find_cli() == ["codex", "exec"]
        assert "code" in REGISTRY
        task = Task(id="t1", request=TaskRequest(kind="code",
                                                 instructions="add a hello script"))
        ctx = WorkerContext(workspace=tmp_path)
        result = asyncio.run(REGISTRY["code"](task, ctx))
        assert result.say.startswith("Code task finished: patched hello.py")
        assert str(tmp_path) in result.data["output"]  # ran with cwd=workspace
        assert result.priority == "interrupt"
    finally:
        os.environ["PATH"] = old_path
        REGISTRY.pop("code", None)
        _reload_code_stub()  # re-register (or not) per the real PATH


def test_code_stub_nonzero_exit_is_an_error_result(tmp_path):
    import echo_app.workers.code_stub as cs
    fake = tmp_path / "codex"
    fake.write_text("#!/bin/sh\necho boom\nexit 3\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    old_path = os.environ.get("PATH", "")
    try:
        os.environ["PATH"] = str(tmp_path)
        task = Task(id="t1", request=TaskRequest(kind="code", instructions="x"))
        result = asyncio.run(cs.run_code(task, WorkerContext(workspace=tmp_path)))
        assert result.data["error"] == "exit 3"
        assert "failed" in result.say
    finally:
        os.environ["PATH"] = old_path


# -- prompt tuning + viewer flash polish -----------------------------------------


def test_system_prompt_tuned_for_voice():
    p = config.SYSTEM_PROMPT
    assert "short" in p                       # short utterances
    assert "ack BEFORE dispatching" in p      # verbal ack before dispatch
    assert "weave" in p                       # weave results naturally
    assert "Never read URLs" in p             # never read URLs aloud
    assert "dispatch_task" in p and "end_session" in p
    # the realtime voice prompt builds on the same tuned prompt
    assert p in VOICE_PROMPT
    from echo_app.conversation import textmode
    assert textmode.SYSTEM_PROMPT == p


def test_viewer_index_has_section_flash():
    html = (Path(config.REPO_ROOT) / "echo_app" / "viewer"
            / "index.html").read_text(encoding="utf-8")
    assert "@keyframes flash" in html   # CSS flash highlight
    assert "sectionMap" in html         # h2 section diffing
    assert "classList.add('flash')" in html
