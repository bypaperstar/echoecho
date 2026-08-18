import asyncio

import pytest

from echoecho_app.bus import Injection
from echoecho_app.conversation.session import ACTIVE, ENDING, IDLE, Session


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def advance(self, secs):
        self.t += secs


def make(silence_timeout=600):
    clock = FakeClock()
    events = []
    hooks = {"paused": 0, "resumed": 0}
    s = Session(clock=clock, silence_timeout=silence_timeout,
                on_state_change=lambda old, new, reason: events.append((old, new, reason)),
                wake_pause=lambda: hooks.__setitem__("paused", hooks["paused"] + 1),
                wake_resume=lambda: hooks.__setitem__("resumed", hooks["resumed"] + 1))
    return s, clock, events, hooks


def test_wake_and_end_transitions_with_hooks():
    s, _, events, hooks = make()
    assert s.state == IDLE
    assert s.wake() is True
    assert s.state == ACTIVE
    assert hooks["paused"] == 1
    assert s.wake() is False  # already active
    assert s.begin_ending("end_session_tool") is True
    assert s.state == ENDING
    assert s.begin_ending("again") is False
    assert hooks["resumed"] == 0  # not until we're back to IDLE
    assert s.finish() is True
    assert s.state == IDLE
    assert hooks["resumed"] == 1
    assert events == [(IDLE, ACTIVE, "wake"),
                      (ACTIVE, ENDING, "end_session_tool"),
                      (ENDING, IDLE, "end_session_tool")]
    # can wake again for a fresh session
    assert s.wake() is True
    assert hooks["paused"] == 2


def test_finish_only_from_ending():
    s, _, _, _ = make()
    assert s.finish() is False
    s.wake()
    assert s.finish() is False


@pytest.mark.parametrize("phrase", [
    "that's it",
    "That's it, thanks!",
    "thats it",
    "that's all",
    "that is all",
    "that is it",
    "ok THAT'S ALL for today",
])
def test_end_phrase_variants(phrase):
    s, _, _, _ = make()
    s.wake()
    assert s.handle_transcript(phrase) is True
    assert s.state == ENDING
    assert s.end_reason == "end_phrase"


@pytest.mark.parametrize("phrase", [
    "that's iterating nicely",
    "is that it or not",
    "i like that sitar",
    "that's italian food",
    "thats allowed here",
    "tell me about it all",
])
def test_end_phrase_negatives(phrase):
    s, _, _, _ = make()
    s.wake()
    assert s.handle_transcript(phrase) is False
    assert s.state == ACTIVE


def test_end_phrase_ignored_when_idle():
    s, _, _, _ = make()
    assert s.handle_transcript("that's it") is False
    assert s.state == IDLE


def test_silence_timeout_with_fake_clock():
    s, clock, _, _ = make(silence_timeout=600)
    s.wake()
    clock.advance(599)
    assert s.check_silence() is False
    assert s.state == ACTIVE
    clock.advance(2)
    assert s.check_silence() is True
    assert s.state == ENDING
    assert s.end_reason == "silence_timeout"


def test_silence_clock_resets_on_user_speech_and_assistant_done():
    s, clock, _, _ = make(silence_timeout=600)
    s.wake()
    clock.advance(500)
    s.note_user_speech_started()  # reset
    s.note_user_speech_stopped()
    clock.advance(599)
    assert s.check_silence() is False
    s.note_assistant_response_done()  # reset (user listening shouldn't time out)
    clock.advance(599)
    assert s.check_silence() is False
    clock.advance(1)
    assert s.check_silence() is True


def test_no_silence_timeout_when_idle():
    s, clock, _, _ = make(silence_timeout=600)
    clock.advance(10000)
    assert s.check_silence() is False
    assert s.state == IDLE


def test_injection_gate_waits_for_turn_boundary():
    s, _, _, _ = make()
    s.wake()
    inj = Injection(text="[task t1 done] result", priority="interrupt")
    s.queue_injection(inj)
    assert s.can_inject() is False  # no completed response yet
    assert s.drain_injections() == []
    s.note_assistant_response_done()
    assert s.can_inject() is True
    s.note_user_speech_started()  # user talking closes the gate
    assert s.can_inject() is False
    s.note_user_speech_stopped()
    assert s.can_inject() is False  # still mid-turn until a response completes
    s.note_assistant_response_done()
    assert s.drain_injections() == [inj]
    assert s.drain_injections() == []  # drained
    assert s.pending_injections == []


def test_injection_gate_closed_outside_active():
    s, _, _, _ = make()
    s.queue_injection(Injection(text="x"))
    s.note_assistant_response_done()
    assert s.can_inject() is False  # IDLE
    s.wake()
    s.note_assistant_response_done()
    s.begin_ending("end_phrase")
    assert s.can_inject() is False  # ENDING


def test_injection_timing_is_best_effort_when_custom_clock_fails():
    s, _, _, _ = make()
    s.wake()
    s.note_assistant_response_done()

    def broken_clock():
        raise RuntimeError("clock unavailable")

    s.clock = broken_clock
    injection = Injection(text="x")
    s.queue_injection(injection)
    assert s.pending_injections == [injection]
    assert s.drain_injections() == [injection]
    assert s.pending_injections == []


def test_silence_timeout_env_default(monkeypatch):
    monkeypatch.setenv("ECHOECHO_SILENCE_TIMEOUT", "5")
    s = Session()
    assert s.silence_timeout == 5.0
    monkeypatch.delenv("ECHOECHO_SILENCE_TIMEOUT")
    assert Session().silence_timeout == 600.0


def test_scripted_smoke_session(tmp_path):
    """Full 3-layer round trip: scripted agent -> orchestrator -> demo worker."""
    import echoecho
    from echoecho_app.conversation.scripted import ScriptedAgent
    from echoecho_app.orchestrator.core import Orchestrator
    from echoecho_app.workers.base import load_all

    script = tmp_path / "script.txt"
    script.write_text(
        "echoecho\n"
        '!dispatch_task {"kind": "sleep.echoecho", "instructions": "ping", '
        '"args": {"sleep": 0.05}}\n'
        "~wait 0.15\n"
        "that's it\n")
    lines = []
    agent = ScriptedAgent(str(script), out=lines.append)
    orch = Orchestrator(registry=load_all(), on_injection=agent.inject,
                        log_path=tmp_path / "tasks.jsonl", workspace=tmp_path)
    agent.on_tool(echoecho.make_tool_handler(orch, agent))

    async def go():
        loop_task = asyncio.ensure_future(orch.run())
        await agent.run()
        loop_task.cancel()

    asyncio.run(go())
    text = "\n".join(lines)
    assert "[state] IDLE -> ACTIVE (wake)" in text
    assert '"task_id": "t1", "status": "queued"' in text.replace("'", '"')
    assert "[task t1 done] echoechoing back: ping" in text
    assert "[state] ACTIVE -> ENDING (end_phrase)" in text
    assert "[state] ENDING -> IDLE (end_phrase)" in text
    # ack printed before the result injection (voice loop never blocks)
    assert text.index("queued") < text.index("[task t1 done]")


def test_dispatch_task_lifts_instructions_nested_in_args(monkeypatch):
    """Voice models sometimes nest instructions inside args instead of the
    top-level field; the worker must still see them (live playtest: doc.edit
    fell back to its 'start a draft' default and wrote an empty draft)."""
    import echoecho
    from echoecho_app import diagnostics

    diagnostic_records = []
    monkeypatch.setattr(
        diagnostics, "info",
        lambda event, **fields: diagnostic_records.append((event, fields)))

    class FakeOrch:
        def submit(self, request):
            self.request = request

            class T:
                id = "t1"
            return T()

    orch = FakeOrch()
    handle = echoecho.make_tool_handler(orch, port=None)

    handle("dispatch_task", {"kind": "doc.edit",
                             "args": {"file": "grocery.md",
                                      "instructions": "add eggs"}})
    assert orch.request.instructions == "add eggs"
    assert orch.request.args == {"file": "grocery.md"}
    nested = [fields for event, fields in diagnostic_records
              if event == "tool.dispatch.instructions_resolved"][-1]
    assert nested == {
        "source": "nested_args", "instruction_chars": len("add eggs"),
        "task_arg_count": 1}
    assert "add eggs" not in repr(diagnostic_records)

    # the top-level field still wins when both are present
    handle("dispatch_task", {"kind": "doc.edit", "instructions": "top",
                             "args": {"instructions": "nested"}})
    assert orch.request.instructions == "top"
    top = [fields for event, fields in diagnostic_records
           if event == "tool.dispatch.instructions_resolved"][-1]
    assert top["source"] == "top_level"
    assert top["instruction_chars"] == 3


def test_env_flags_case_insensitive(monkeypatch):
    from echoecho_app import config
    monkeypatch.setenv("ECHOECHO_TEXT", "False")
    assert config.echoecho_text() is False
    monkeypatch.setenv("ECHOECHO_TEXT", "NO")
    assert config.echoecho_text() is False
    monkeypatch.setenv("ECHOECHO_TEXT", "1")
    assert config.echoecho_text() is True
