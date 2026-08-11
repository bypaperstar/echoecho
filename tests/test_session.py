import asyncio

import pytest

from echo_app.bus import Injection
from echo_app.conversation.session import ACTIVE, ENDING, IDLE, Session


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


def test_silence_timeout_env_default(monkeypatch):
    monkeypatch.setenv("ECHO_SILENCE_TIMEOUT", "5")
    s = Session()
    assert s.silence_timeout == 5.0
    monkeypatch.delenv("ECHO_SILENCE_TIMEOUT")
    assert Session().silence_timeout == 600.0


def test_scripted_smoke_session(tmp_path):
    """Full 3-layer round trip: scripted agent -> orchestrator -> demo worker."""
    import echo
    from echo_app.conversation.scripted import ScriptedAgent
    from echo_app.orchestrator.core import Orchestrator
    from echo_app.workers.base import load_all

    script = tmp_path / "script.txt"
    script.write_text(
        "echo echo\n"
        '!dispatch_task {"kind": "sleep.echo", "instructions": "ping", '
        '"args": {"sleep": 0.05}}\n'
        "~wait 0.15\n"
        "that's it\n")
    lines = []
    agent = ScriptedAgent(str(script), out=lines.append)
    orch = Orchestrator(registry=load_all(), on_injection=agent.inject,
                        log_path=tmp_path / "tasks.jsonl", workspace=tmp_path)
    agent.on_tool(echo.make_tool_handler(orch, agent))

    async def go():
        loop_task = asyncio.ensure_future(orch.run())
        await agent.run()
        loop_task.cancel()

    asyncio.run(go())
    text = "\n".join(lines)
    assert "[state] IDLE -> ACTIVE (wake)" in text
    assert '"task_id": "t1", "status": "queued"' in text.replace("'", '"')
    assert "[task t1 done] Echoing back: ping" in text
    assert "[state] ACTIVE -> ENDING (end_phrase)" in text
    assert "[state] ENDING -> IDLE (end_phrase)" in text
    # ack printed before the result injection (voice loop never blocks)
    assert text.index("queued") < text.index("[task t1 done]")


def test_env_flags_case_insensitive(monkeypatch):
    from echo_app import config
    monkeypatch.setenv("ECHO_TEXT", "False")
    assert config.echo_text() is False
    monkeypatch.setenv("ECHO_TEXT", "NO")
    assert config.echo_text() is False
    monkeypatch.setenv("ECHO_TEXT", "1")
    assert config.echo_text() is True
