"""PR 4 spec-by-test: full scripted Realtime sessions replayed over
FakeTransport, asserting every client event name/payload against the GA docs.
No key, no audio, no network — sync tests calling asyncio.run internally."""
import asyncio
import copy
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from echoecho_app import config, diagnostics
from echoecho_app.bus import Injection
from echoecho_app.conversation.port import TOOL_NAMES
from echoecho_app.conversation.realtime import (FakeTransport, PlaybackTracker,
                                            RealtimeClient, build_session_update,
                                            pcm16_ms)
from echoecho_app.conversation.session import Session
from echoecho_app.conversation.textmode import build_tools

FIX = config.FIXTURES_DIR / "realtime"


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def advance(self, secs):
        self.t += secs


def run_client(fixture, tool_cb=None, wire=None, clock=None,
               silence_timeout=600, **kw):
    """Replay a fixture through a RealtimeClient; return (transport, client)."""
    transport = FakeTransport(fixture)
    session = Session(clock=clock or FakeClock(), silence_timeout=silence_timeout)
    client = RealtimeClient(transport, session=session, poll_interval=0.01,
                            out=lambda *_: None, **kw)
    if tool_cb:
        client.on_tool(tool_cb)
    transport.hooks["_inject"] = lambda ev: client.inject(
        Injection(text=ev["text"], priority=ev["priority"]))
    transport.hooks["_play"] = lambda ev: client.tracker.advance(ev["ms"])
    if isinstance(client.clock, FakeClock):
        transport.hooks["_clock.advance"] = (
            lambda ev: client.clock.advance(ev["seconds"]))
    if wire:
        wire(transport, client)
    asyncio.run(client.run())
    return transport, client


def system_items(sent):
    return [e for e in sent if e.get("type") == "conversation.item.create"
            and e.get("item", {}).get("role") == "system"]


# -- session.update contents + 4-tool Contract A schema -------------------------


def test_session_update_is_first_send_with_full_config():
    transport, client = run_client(str(FIX / "end_phrase.jsonl"))
    first = transport.sent[0]
    assert first["type"] == "session.update"
    s = first["session"]
    assert s["type"] == "realtime"
    assert "dispatch_task" in s["instructions"]  # system prompt present
    assert s["output_modalities"] == ["audio"]
    # pcm16 24kHz both directions
    assert s["audio"]["input"]["format"] == {"type": "audio/pcm", "rate": 24000}
    assert s["audio"]["output"]["format"] == {"type": "audio/pcm", "rate": 24000}
    assert s["audio"]["input"]["noise_reduction"] == {"type": "far_field"}
    # semantic VAD with barge-in + input transcription enabled
    assert s["audio"]["input"]["turn_detection"] == {
        "type": "semantic_vad", "interrupt_response": True}
    assert s["audio"]["input"]["transcription"].get("model")
    assert s["tool_choice"] == "auto"


def test_explicit_connect_is_idempotent_before_run():
    """Voice mode configures Realtime before enabling mic callback uploads."""
    transport = FakeTransport(str(FIX / "end_phrase.jsonl"))
    client = RealtimeClient(transport, session=Session(clock=FakeClock()),
                            poll_interval=0.01, out=lambda *_: None)

    async def scenario():
        await client.connect()
        assert transport.sent_types() == ["session.update"]
        await client.run()

    asyncio.run(scenario())
    assert transport.sent_types().count("session.update") == 1


def test_input_audio_sender_follows_replaced_transport():
    first, second = FakeTransport([]), FakeTransport([])
    client = RealtimeClient(first)
    event = {"type": "input_audio_buffer.append", "audio": "AAAA"}

    async def scenario():
        await client.send_input_audio(event)
        client.transport = second
        await client.send_input_audio(event)

    asyncio.run(scenario())
    assert first.sent == [event]
    assert second.sent == [event]


def test_four_tool_schema_matches_contract_a(monkeypatch):
    monkeypatch.delenv("ECHOECHO_PLUGINS", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)  # hides doc.edit
    monkeypatch.delenv("ECHOECHO_FAKE_LLM", raising=False)
    from echoecho_app.workers.base import load_all
    load_all()
    tools = build_session_update()["session"]["tools"]
    assert [t["name"] for t in tools] == list(TOOL_NAMES)
    assert tools == build_tools()  # identical to the Contract A schema (textmode)
    by_name = {t["name"]: t for t in tools}
    for t in tools:
        assert t["type"] == "function"
        assert t["parameters"]["type"] == "object"
    dispatch = by_name["dispatch_task"]["parameters"]
    assert dispatch["required"] == ["kind", "instructions"]
    # the enum is GENERATED from the registry: one generic kind by default,
    # demo plugins stay dispatchable but unadvertised (PR 10)
    assert dispatch["properties"]["kind"]["enum"] == ["agent.run"]
    assert by_name["read_artifact"]["parameters"]["required"] == ["name"]
    assert "required" not in by_name["check_tasks"]["parameters"]
    assert by_name["end_session"]["parameters"]["properties"] == {}


# -- dispatch round-trip ---------------------------------------------------------


def test_dispatch_round_trip_instant_output_and_response_create():
    calls = []

    def cb(name, args):
        calls.append((name, args))
        return {"task_id": "t1", "status": "queued"}

    transport, client = run_client(str(FIX / "dispatch.jsonl"), tool_cb=cb)
    # the tool call reached the orchestrator-side handler with parsed args
    assert calls[0] == ("dispatch_task",
                        {"kind": "recipe.search", "instructions": "pad thai"})
    types = transport.sent_types()
    i = types.index("conversation.item.create")  # first item.create = the ack
    ack = transport.sent[i]
    assert ack["item"]["type"] == "function_call_output"
    assert ack["item"]["call_id"] == "call_1"
    assert json.loads(ack["item"]["output"]) == {"task_id": "t1",
                                                 "status": "queued"}
    # IMMEDIATELY followed by response.create (model acks and keeps talking)
    assert transport.sent[i + 1] == {"type": "response.create"}


def test_malformed_tool_arguments_degrade_but_still_ack():
    """Model-generated arguments JSON can be malformed; the loop must survive
    and the server must still get a function_call_output for the call_id."""
    fixture = [
        {"type": "session.created", "session": {"id": "s"}},
        {"type": "response.done", "response": {"id": "r1", "output": [
            {"type": "function_call", "name": "dispatch_task", "call_id": "c1",
             "arguments": "{\"kind\": \"recipe.search\", "}]}},  # truncated JSON
        {"type": "conversation.item.input_audio_transcription.completed",
         "item_id": "u1", "transcript": "that's it"},
        {"type": "response.done", "response": {"id": "r2", "output": []}},
    ]
    calls = []

    def cb(name, args):
        calls.append((name, args))
        return {"task_id": "t1", "status": "queued"}

    transport, client = run_client(fixture, tool_cb=cb)
    assert calls == [("dispatch_task", {})]  # degraded to empty args, no crash
    acks = [e for e in transport.sent
            if e.get("type") == "conversation.item.create"
            and e["item"]["type"] == "function_call_output"]
    assert acks and acks[0]["item"]["call_id"] == "c1"
    assert client.session.state == "IDLE"  # session survived and ended cleanly


def test_tool_handler_exception_still_sends_function_call_output():
    def cb(name, args):
        raise KeyError("kind")

    transport, client = run_client(str(FIX / "dispatch.jsonl"), tool_cb=cb)
    acks = [e for e in transport.sent
            if e.get("type") == "conversation.item.create"
            and e["item"]["type"] == "function_call_output"]
    assert len(acks) == 1
    assert "error" in json.loads(acks[0]["item"]["output"])
    assert client.session.state == "IDLE"  # loop survived the handler bug


# -- injection gating: adversarial orderings ---------------------------------------


def test_injection_gating_adversarial_orderings():
    snaps = {}

    def wire(transport, client):
        transport.hooks["_probe"] = (
            lambda ev: snaps.__setitem__(ev["label"],
                                         copy.deepcopy(transport.sent)))

    transport, client = run_client(str(FIX / "gating.jsonl"), wire=wire)

    # scenario 1: interrupt result arrived mid-user-speech -> held
    assert system_items(snaps["mid_speech"]) == []
    # still held after speech_stopped: no completed response yet
    assert system_items(snaps["after_stop_before_done"]) == []
    # delivered only after response.done, with response.create (interrupt)
    delivered = system_items(snaps["after_done_1"])
    assert len(delivered) == 1
    text = delivered[0]["item"]["content"][0]["text"]
    assert text.startswith("[task t1 done] Found a 30-minute pad thai.")
    assert "Weave in naturally." in text
    assert delivered[0]["item"]["content"][0]["type"] == "input_text"
    done1_types = [e["type"] for e in snaps["after_done_1"]]
    assert done1_types.count("response.create") == 1
    assert done1_types.index("response.create") > done1_types.index(
        "conversation.item.create")

    # scenario 2: ambient result arrived mid-response -> held until done
    assert len(system_items(snaps["mid_response"])) == 1  # still only t1
    delivered2 = system_items(snaps["after_done_2"])
    assert len(delivered2) == 2
    assert delivered2[1]["item"]["content"][0]["text"].startswith(
        "[task t2 done] Grocery list merged.")
    # ambient sends the item only: no extra response.create for t2
    assert [e["type"] for e in snaps["after_done_2"]].count("response.create") == 1


def test_silent_injection_never_sent():
    fixture = [
        {"type": "session.created", "session": {"id": "s"}},
        {"type": "_inject", "text": "[task t9 done] bookkeeping",
         "priority": "silent"},
        {"type": "response.done", "response": {"id": "r1", "output": []}},
        {"type": "conversation.item.input_audio_transcription.completed",
         "item_id": "u1", "transcript": "that's it"},
        {"type": "response.done", "response": {"id": "r2", "output": []}},
    ]
    transport, client = run_client(fixture)
    assert system_items(transport.sent) == []  # task table only


# -- barge-in truncate math ----------------------------------------------------------


def test_barge_in_sends_truncate_with_exact_audio_end_ms():
    flushed = []
    transport, client = run_client(str(FIX / "barge_in.jsonl"),
                                   flush_playback=lambda: flushed.append(1))
    truncs = [e for e in transport.sent
              if e["type"] == "conversation.item.truncate"]
    # 2x50ms appended for item_a1, play cursor advanced to 60ms -> 60
    assert truncs == [{"type": "conversation.item.truncate",
                       "item_id": "item_a1", "content_index": 0,
                       "audio_end_ms": 60}]
    assert flushed  # local playback queue flushed on speech_started
    assert client.tracker.total_ms() == 0  # tracker reset after truncate


def test_no_truncate_when_all_audio_was_played():
    fixture = [
        {"type": "session.created", "session": {"id": "s"}},
        {"type": "response.created", "response": {"id": "r1"}},
        {"type": "response.output_audio.delta", "item_id": "a1",
         "content_index": 0,
         "delta": __import__("base64").b64encode(b"\x00" * 480).decode()},
        {"type": "_play", "ms": 10},  # all 10ms played
        {"type": "input_audio_buffer.speech_started", "item_id": "u1"},
        {"type": "conversation.item.input_audio_transcription.completed",
         "item_id": "u1", "transcript": "that's it"},
        {"type": "response.done", "response": {"id": "r2", "output": []}},
    ]
    transport, client = run_client(fixture)
    assert "conversation.item.truncate" not in transport.sent_types()


def test_playback_tracker_math():
    tr = PlaybackTracker()
    tr.append("a", 100)
    tr.append("a", 50)  # coalesces: a = 150ms
    tr.append("b", 200)
    assert tr.total_ms() == 350
    tr.advance(180)  # 150 through a, 30 into b
    assert tr.truncate() == ("b", 30)
    assert tr.total_ms() == 0  # flushed
    # over-advance clamps: everything played -> nothing to truncate
    tr.append("c", 40)
    tr.advance(9999)
    assert tr.truncate() is None
    # nothing appended
    assert tr.truncate() is None
    assert pcm16_ms(2400) == 50.0  # 24kHz mono pcm16: 48 bytes/ms


@pytest.mark.parametrize("event", [
    None,
    {"type": []},
    {"type": "response.output_audio.delta", "delta": None},
    {"type": "response.output_audio.delta", "delta": "not base64!"},
])
def test_malformed_upstream_events_are_ignored_without_telemetry_crashes(event):
    client = RealtimeClient(FakeTransport([]))
    asyncio.run(client._handle_event(event))


def test_malformed_upstream_logging_is_power_of_two_sampled(monkeypatch):
    emitted = []
    monkeypatch.setattr(
        diagnostics, "error",
        lambda event, **fields: emitted.append((event, fields)))
    client = RealtimeClient(FakeTransport([]))
    for _ in range(9):
        asyncio.run(client._handle_event(None))
    assert [fields["occurrences"] for _event, fields in emitted] == [1, 2, 3, 4, 8]
    assert client._protocol_errors["invalid_event"] == 9


# -- session closes: end phrase, end_session tool, silence timeout ---------------------


def test_end_phrase_regex_closes_with_spoken_sign_off():
    transport, client = run_client(str(FIX / "end_phrase.jsonl"))
    assert client.session.end_reason == "end_phrase"
    assert client.session.state == "IDLE"  # ENDING -> finish() -> IDLE
    assert transport.closed
    signoffs = [e for e in transport.sent if e["type"] == "response.create"]
    assert len(signoffs) == 1
    assert "goodbye" in signoffs[0]["response"]["instructions"]


def test_end_session_tool_closes_with_sign_off():
    calls = []

    def cb(name, args):
        calls.append(name)
        return {"status": "ending"}

    transport, client = run_client(str(FIX / "end_session_tool.jsonl"),
                                   tool_cb=cb)
    assert calls == ["end_session"]
    assert client.session.end_reason == "end_session_tool"
    assert transport.closed
    types = transport.sent_types()
    # protocol correctness: function_call_output for end_session, then the
    # sign-off response.create (and no bare response.create racing it)
    i = types.index("conversation.item.create")
    assert transport.sent[i]["item"]["call_id"] == "call_9"
    assert json.loads(transport.sent[i]["item"]["output"]) == {
        "status": "ending"}
    creates = [e for e in transport.sent if e["type"] == "response.create"]
    assert len(creates) == 1
    assert creates[0]["response"]["instructions"]  # the sign-off, not a bare ack


def test_silence_timeout_closes_without_sign_off():
    clock = FakeClock()
    transport, client = run_client(str(FIX / "silence.jsonl"), clock=clock,
                                   silence_timeout=600)
    assert client.session.end_reason == "silence_timeout"
    assert client.session.state == "IDLE"
    assert transport.closed
    # sign-off skipped on timeout: no response.create, no goodbye item
    assert "response.create" not in transport.sent_types()
    assert system_items(transport.sent) == []


def test_silence_timer_resets_on_speech_events():
    clock = FakeClock()
    fixture = [
        {"type": "session.created", "session": {"id": "s"}},
        {"type": "_clock.advance", "seconds": 590},
        {"type": "input_audio_buffer.speech_started", "item_id": "u1"},  # reset
        {"type": "input_audio_buffer.speech_stopped", "item_id": "u1"},
        {"type": "_clock.advance", "seconds": 590},
        {"type": "response.done", "response": {"id": "r1", "output": []}},  # reset
        {"type": "_clock.advance", "seconds": 601},  # now it expires
    ]
    transport, client = run_client(fixture, clock=clock, silence_timeout=600)
    assert client.session.end_reason == "silence_timeout"
    assert transport.closed


# -- 55-min summary + reconnect stub ---------------------------------------------------


def test_55min_reconnect_stub_reopens_and_resends_session_update():
    clock = FakeClock()
    second = FakeTransport([
        {"type": "conversation.item.input_audio_transcription.completed",
         "item_id": "u9", "transcript": "that's it"},
        {"type": "response.done", "response": {"id": "r9", "output": []}},
    ])
    first_fixture = [
        {"type": "session.created", "session": {"id": "s"}},
        {"type": "response.done", "response": {"id": "r1", "output": []}},
        {"type": "_clock.advance", "seconds": 55 * 60 + 5},
    ]
    transport, client = run_client(first_fixture, clock=clock,
                                   silence_timeout=10 ** 6,
                                   transport_factory=lambda: second)
    assert transport.closed  # old connection closed at the cap
    assert second.sent_types()[0] == "session.update"  # reconfigured
    # best-effort summary stub injected on the fresh connection
    assert "[reconnected]" in system_items(second.sent)[0]["item"]["content"][0]["text"]
    assert second.closed  # session then ended normally over the new transport
    assert client.session.end_reason == "end_phrase"


# -- full scripted session over FakeTransport (dispatch + late result + end) ------------


def test_full_session_dispatch_then_result_then_end():
    """One coherent session: dispatch -> instant ack -> result injected at the
    next turn boundary -> end phrase -> sign-off -> close."""
    fixture = [
        {"type": "session.created", "session": {"id": "s"}},
        {"type": "response.done", "response": {"id": "r1", "output": [
            {"type": "function_call", "name": "dispatch_task",
             "call_id": "c1",
             "arguments": "{\"kind\": \"sleep.echoecho\", \"instructions\": \"hi\"}"}]}},
        {"type": "response.created", "response": {"id": "r2"}},  # the spoken ack
        {"type": "_inject", "text": "[task t1 done] echoechoing back: hi",
         "priority": "interrupt"},  # result lands while ack is being spoken
        {"type": "_probe", "label": "mid_ack"},
        {"type": "response.done", "response": {"id": "r2",
                                               "output": [{"type": "message"}]}},
        {"type": "_probe", "label": "after_ack"},
        {"type": "conversation.item.input_audio_transcription.completed",
         "item_id": "u2", "transcript": "that's all"},
        {"type": "response.done", "response": {"id": "r3", "output": []}},
    ]
    snaps = {}

    def wire(transport, client):
        transport.hooks["_probe"] = (
            lambda ev: snaps.__setitem__(ev["label"],
                                         copy.deepcopy(transport.sent)))

    transport, client = run_client(
        fixture, tool_cb=lambda n, a: {"task_id": "t1", "status": "queued"},
        wire=wire)
    assert system_items(snaps["mid_ack"]) == []  # held during the ack response
    assert len(system_items(snaps["after_ack"])) == 1  # delivered at boundary
    types = transport.sent_types()
    assert types[0] == "session.update"
    assert types.count("conversation.item.create") == 2  # tool ack + injection
    assert client.session.state == "IDLE"
    assert transport.closed


# -- echoecho.py --voice wiring -----------------------------------------------------------


@pytest.mark.skipif(
    importlib.util.find_spec("sounddevice") is not None,
    reason="asserts the no-sounddevice fallback; sounddevice is installed")
def test_echo_voice_fails_politely_without_audio_deps():
    # PR 5 note: --voice is fully wired now, but on Linux CI (no sounddevice)
    # it must still fail politely and point at a working fallback.
    proc = subprocess.run(
        [sys.executable, str(config.REPO_ROOT / "echoecho.py"), "--voice"],
        capture_output=True, text=True, timeout=30)
    assert proc.returncode != 0
    err = proc.stderr + proc.stdout
    assert "sounddevice" in err
    assert "--text" in err  # points at a working fallback


def test_client_constructible_with_fake_transport():
    client = RealtimeClient(FakeTransport([]))
    assert client.session.state == "IDLE"
    client.inject(Injection(text="[task t1 done] x", priority="ambient"))
    assert client.session.pending_injections  # queued, held for the gate
