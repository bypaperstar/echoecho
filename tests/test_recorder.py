"""PR 9: per-session recordings (audio + events) — the dev feedback loop.

All headless: SessionRecorder is pure stdlib (wave/queue/threading), and the
AudioIO taps are exercised by invoking the PortAudio callbacks directly, so
no sounddevice and no audio hardware are needed. The scripted-run test drives
the real amain() wiring end to end.
"""
import asyncio
import json
import os
import queue
import threading
import time
import wave
from array import array

import pytest

import echoecho
from echoecho_app import config, events, recorder
from echoecho_app.conversation.audio import AudioIO
from echoecho_app.conversation.realtime import PlaybackTracker


@pytest.fixture
def rec_env(tmp_path, monkeypatch):
    """Isolated workspace + recordings root; never leaks an active recorder."""
    monkeypatch.setattr(config, "WORKSPACE_DIR", tmp_path / "ws")
    monkeypatch.setattr(config, "RECORDINGS_DIR", tmp_path / "rec")
    monkeypatch.delenv("ECHOECHO_RECORDINGS_DIR", raising=False)
    monkeypatch.delenv("ECHOECHO_RECORD", raising=False)
    yield tmp_path / "rec"
    recorder.stop()
    events.reset(mode="test", run_id=None)


def read_wav(path):
    """(params, samples) for a wav file."""
    with wave.open(str(path), "rb") as w:
        samples = array("h", w.readframes(w.getnframes()))
        return w, samples  # w is closed but params remain readable


def session_dirs(root):
    return sorted(p for p in root.iterdir() if p.is_dir()) if root.is_dir() else []


# -- config --------------------------------------------------------------------


def test_echoecho_record_defaults_to_voice_only(monkeypatch):
    monkeypatch.delenv("ECHOECHO_RECORD", raising=False)
    assert config.echoecho_record("voice") is True
    assert config.echoecho_record("text") is False
    assert config.echoecho_record("script") is False
    monkeypatch.setenv("ECHOECHO_RECORD", "0")
    assert config.echoecho_record("voice") is False
    monkeypatch.setenv("ECHOECHO_RECORD", "false")
    assert config.echoecho_record("voice") is False
    monkeypatch.setenv("ECHOECHO_RECORD", "1")
    assert config.echoecho_record("text") is True
    assert config.echoecho_record("script") is True


def test_recordings_dir_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("ECHOECHO_RECORDINGS_DIR", str(tmp_path / "elsewhere"))
    assert config.recordings_dir() == tmp_path / "elsewhere"
    monkeypatch.delenv("ECHOECHO_RECORDINGS_DIR")
    assert config.recordings_dir() == config.RECORDINGS_DIR


# -- events tee ------------------------------------------------------------------


def test_start_tees_events_and_still_feeds_the_viewer(rec_env, capsys):
    rec = recorder.start("voice")
    assert recorder.active() is rec
    events.emit("wake", via="voice")
    events.emit("user_text", text="add milk to the list")
    # the viewer's live feed keeps working unchanged
    feed = (config.WORKSPACE_DIR / events.FEED_NAME).read_text()
    assert "add milk" in feed
    recorder.stop(end_reason="end_phrase")
    # ...and the ordered writer drains a durable session copy at close.
    lines = (rec.dir / "events.jsonl").read_text().splitlines()
    assert [json.loads(ln)["type"] for ln in lines] == ["wake", "user_text"]
    assert recorder.active() is None
    assert events.TEE is None
    events.emit("user_text", text="after close")  # must not raise or leak in
    assert "after close" not in (rec.dir / "events.jsonl").read_text()
    out = capsys.readouterr().out
    assert "[record] session -> " in out and "[record] saved " in out


def test_recording_tee_serializes_concurrent_events_in_feed_order(rec_env):
    events.reset(mode="test", run_id="run-recording-order")
    rec = recorder.start("voice")
    threads = [threading.Thread(
        target=events.emit, args=("concurrent",), kwargs={"worker": index})
               for index in range(80)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    recorder.stop(end_reason="test")

    recorded = [json.loads(line) for line in
                (rec.dir / "events.jsonl").read_text().splitlines()]
    assert [item["seq"] for item in recorded] == list(range(2, 82))
    assert [item["seq"] for item in rec.events] == list(range(2, 82))


def test_stop_boundary_cannot_strand_an_event_behind_the_writer_sentinel(
        rec_env):
    rec = recorder.start("voice")
    rec._event_lock.acquire()
    emitter = threading.Thread(
        target=events.emit, args=("boundary",), kwargs={"value": 1})
    stopper = threading.Thread(
        target=recorder.stop, kwargs={"end_reason": "test"})
    try:
        emitter.start()
        feed = config.WORKSPACE_DIR / events.FEED_NAME
        deadline = time.monotonic() + 2
        while (not feed.exists() or '"boundary"' not in feed.read_text()) \
                and time.monotonic() < deadline:
            time.sleep(0.01)
        assert feed.exists() and '"boundary"' in feed.read_text()
        stopper.start()
        time.sleep(0.05)
        assert stopper.is_alive()
    finally:
        rec._event_lock.release()
    emitter.join(timeout=2)
    stopper.join(timeout=2)

    assert not emitter.is_alive() and not stopper.is_alive()
    recorded = [json.loads(line) for line in
                (rec.dir / "events.jsonl").read_text().splitlines()]
    assert [item["type"] for item in recorded] == ["boundary"]


def test_constructor_rolls_back_first_writer_if_second_thread_start_fails(
        rec_env, monkeypatch):
    real_start = threading.Thread.start
    started = []

    def fail_second(thread):
        if not started:
            real_start(thread)
            started.append(thread)
            return
        raise RuntimeError("event writer would not start")

    monkeypatch.setattr(threading.Thread, "start", fail_second)
    with pytest.raises(RuntimeError, match="would not start"):
        recorder.SessionRecorder("voice", root=rec_env)

    assert len(started) == 1
    assert not started[0].is_alive()


def test_stop_writes_transcript_and_meta(rec_env):
    rec = recorder.start("voice")
    events.emit("wake", via="voice")
    events.emit("audio", input="MacBook Pro Microphone", output="AirPods Pro")
    events.emit("session", event="connected", model="gpt-realtime-2.1-mini")
    events.emit("user_text", text="add milk")
    events.emit("tool_call", name="dispatch_task", args={"kind": "grocery.merge"})
    events.emit("assistant_text", text="On it.")
    events.emit("state", frm="ACTIVE", to="IDLE", reason="end_phrase")
    recorder.stop()  # no explicit reason: falls back to the state event's
    transcript = (rec.dir / "transcript.md").read_text()
    assert "you:** add milk" in transcript
    assert "echoecho:** On it." in transcript
    assert "⚙ dispatch_task" in transcript and "grocery.merge" in transcript
    assert "🎧 mic: MacBook Pro Microphone" in transcript
    meta = json.loads((rec.dir / "meta.json").read_text())
    assert meta["mode"] == "voice"
    assert meta["end_reason"] == "end_phrase"
    assert meta["wake_via"] == "voice"
    assert meta["model"] == "gpt-realtime-2.1-mini"
    assert meta["devices"] == {"input": "MacBook Pro Microphone",
                               "output": "AirPods Pro"}
    assert meta["user_turns"] == 1 and meta["assistant_turns"] == 1
    assert meta["tool_calls"] == ["dispatch_task"]
    assert meta["event_counts"]["user_text"] == 1
    assert meta["write_errors"] == 0


def test_render_transcript_offsets_and_lines():
    evs = [
        {"ts": 1000.0, "type": "wake", "via": "voice"},
        {"ts": 1002.0, "type": "user_text", "text": "hi"},
        {"ts": 1065.0, "type": "assistant_text", "text": "hello"},
        {"ts": 1000.0 + 3661, "type": "injection", "priority": "ambient",
         "text": "[task t1 done] ok"},
        {"ts": 1001.0, "type": "totally_new_event", "detail": 7},
    ]
    text = recorder.render_transcript(evs, started=1000.0)
    assert "[00:00] ⏰ wake (voice)" in text
    assert "**[00:02] you:** hi" in text
    assert "**[01:05] echoecho:** hello" in text
    assert "[1:01:01] ↪ injected (ambient): [task t1 done] ok" in text
    # unknown event types are never dropped from the review sheet
    assert "totally_new_event" in text and '"detail": 7' in text


# -- audio tracks -----------------------------------------------------------------


def test_wavs_written_and_mixed_stereo(rec_env):
    rec = recorder.start("voice")
    rec.write_mic(b"\x01\x00" * 2400)   # 0.1 s of sample value 1
    rec.write_echoecho(b"\x02\x00" * 4800)  # 0.2 s of sample value 2
    recorder.stop()
    w, mic = read_wav(rec.dir / "mic.wav")
    assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, 24000)
    assert len(mic) == 2400 and mic[0] == 1
    w, echo_track = read_wav(rec.dir / "echoecho.wav")
    assert len(echo_track) == 4800 and echo_track[0] == 2
    w, mix = read_wav(rec.dir / "session.wav")
    assert w.getnchannels() == 2
    assert len(mix) == 2 * 4800  # padded to the longer track
    assert mix[0] == 1 and mix[1] == 2          # frame 0: L=mic, R=echoecho
    assert mix[2 * 2400] == 0                   # past mic EOF: left is silence
    assert mix[2 * 2400 + 1] == 2               # ...echoecho continues on the right
    meta = json.loads((rec.dir / "meta.json").read_text())
    assert meta["mic_s"] == 0.1 and meta["echoecho_s"] == 0.2


def test_events_only_session_has_no_wavs(rec_env):
    rec = recorder.start("text")
    events.emit("user_text", text="hi")
    recorder.stop(end_reason="end_phrase")
    names = sorted(p.name for p in rec.dir.iterdir())
    assert names == ["events.jsonl", "meta.json", "transcript.md"]


def test_in_callback_taps_mic_even_when_not_sending(rec_env):
    rec = recorder.start("voice")
    audio = AudioIO()
    audio._in_callback(b"\x07\x00" * 10, 10, None, None)  # no loop/send wired
    audio.muted_capture = True  # teardown tail: still audible, still recorded
    audio._in_callback(b"\x07\x00" * 10, 10, None, None)
    recorder.stop()
    _, mic = read_wav(rec.dir / "mic.wav")
    assert len(mic) == 20 and set(mic) == {7}


def test_out_callback_records_exactly_what_played(rec_env):
    rec = recorder.start("voice")
    audio = AudioIO()
    audio.tracker = PlaybackTracker()
    audio.tracker.append("item-1", 100.0)
    audio.feed_pcm(b"\x05\x00" * 100)  # only 100 of 2400 frames available
    outdata = bytearray(4800)
    audio._out_callback(outdata, 2400, None, None)
    # speaker got the audio + zero-fill, exactly as before
    got = array("h", bytes(outdata))
    assert got[0] == 5 and got[99] == 5 and got[100] == 0
    # tracker cursor still advances by REAL audio only (barge-in math intact):
    # 200 bytes played = 100 samples at 24 kHz ≈ 4.17 ms
    assert audio.tracker._played_ms == pytest.approx(200 / (24000 * 2.0) * 1000.0)
    recorder.stop()
    _, echo_track = read_wav(rec.dir / "echoecho.wav")
    assert len(echo_track) == 2400  # zero-fill recorded: timeline == wall clock
    assert echo_track[0] == 5 and echo_track[100] == 0


def test_callbacks_are_noops_without_active_recording(rec_env):
    audio = AudioIO()
    audio._in_callback(b"\x01\x00" * 10, 10, None, None)
    audio._out_callback(bytearray(200), 100, None, None)
    assert not rec_env.exists()  # nothing recorded, no dir created


# -- robustness -------------------------------------------------------------------


def test_recording_failures_never_reach_the_app(rec_env):
    rec = recorder.start("voice")
    rec._events_fh.close()  # simulate the disk going away mid-session
    events.emit("user_text", text="still fine")  # must not raise
    recorder.stop(end_reason="end_phrase")  # close of a broken recorder: fine
    assert rec.errors >= 1
    meta = json.loads((rec.dir / "meta.json").read_text())
    assert meta["write_errors"] >= 1
    # the event still made the transcript (memory copy) and the live feed
    assert "still fine" in (rec.dir / "transcript.md").read_text()
    assert "still fine" in (config.WORKSPACE_DIR / events.FEED_NAME).read_text()


def test_start_failure_disables_recording_politely(rec_env, monkeypatch, capsys):
    blocked = rec_env.parent / "blocked"
    blocked.write_text("")  # a FILE where the recordings root should be
    monkeypatch.setattr(config, "RECORDINGS_DIR", blocked)
    assert recorder.start("voice") is None
    assert recorder.active() is None and events.TEE is None
    assert "[record] disabled for this session" in capsys.readouterr().out
    events.emit("user_text", text="app still works")  # tee-less emit is fine


def test_invalid_queue_setting_falls_back_before_filesystem_side_effects(
        rec_env, monkeypatch):
    monkeypatch.setenv("ECHOECHO_RECORD_QUEUE_BLOCKS", "not-a-number")
    rec = recorder.start("voice")
    assert rec is not None
    assert rec._q.maxsize == 10000
    recorder.stop(end_reason="test")
    assert len(session_dirs(rec_env)) == 1


class StuckThread(object):
    """Stands in for a drain thread wedged on a hung disk."""
    def join(self, timeout=None):
        pass

    def is_alive(self):
        return True


def test_close_survives_a_stalled_writer_thread(rec_env, capsys):
    rec = recorder.start("voice")
    events.emit("user_text", text="hi")
    rec.write_mic(b"\x01\x00" * 240)
    rec._thread.join(timeout=5.0)  # let the real writes land first
    rec._thread = StuckThread()
    recorder.stop(end_reason="end_phrase")  # must not raise, must not hang
    assert "writer thread stalled" in capsys.readouterr().out
    meta = json.loads((rec.dir / "meta.json").read_text())
    assert meta["writer_stalled"] is True
    assert meta["end_reason"] == "end_phrase"
    # the review sheet still exists; wavs are left as-is (never touched live)
    assert "you:** hi" in (rec.dir / "transcript.md").read_text()
    assert not (rec.dir / "session.wav").exists()


def test_close_does_not_race_review_files_with_stalled_event_writer(
        rec_env, capsys):
    rec = recorder.SessionRecorder("voice", root=rec_env)
    rec._event_q.put(None)
    rec._event_thread.join(timeout=1.0)
    rec._event_q = queue.Queue()
    rec._event_thread = StuckThread()
    rec.on_event({"ts": time.time(), "type": "user_text"},
                 '{"type":"user_text"}')

    rec.close(end_reason="test")

    assert rec.event_writer_stalled is True
    assert not (rec.dir / "transcript.md").exists()
    assert not (rec.dir / "meta.json").exists()
    assert "event writer stalled" in capsys.readouterr().out
    rec._events_fh.close()


def test_full_audio_queue_cannot_block_close_or_log_from_callback(
        rec_env, monkeypatch):
    rec = recorder.SessionRecorder("voice", root=rec_env)
    # Retire the real drain thread, then model a writer wedged while a bounded
    # callback queue is full.
    rec._q.put(None)
    rec._thread.join(timeout=1.0)
    rec._q = queue.Queue(maxsize=1)
    rec._q.put(("mic", b"\x00\x00"))
    rec._thread = StuckThread()

    warnings = []
    monkeypatch.setattr(
        recorder.diagnostics, "warning",
        lambda event, **fields: warnings.append((event, fields)))
    rec._enqueue("mic", b"\x01\x00")
    assert warnings == []  # PortAudio callback path remains memory-only.

    started = time.monotonic()
    rec.close(end_reason="test")
    assert time.monotonic() - started < 1.0
    assert rec.dropped_blocks >= 2
    assert any(fields.get("deferred_from_realtime_callback")
               for event, fields in warnings
               if event == "recorder.write.failed")


def test_stop_swallows_close_failures(rec_env, monkeypatch, capsys):
    rec = recorder.start("voice")

    def explode(end_reason=None):
        raise RuntimeError("dictionary changed size during iteration")
    monkeypatch.setattr(rec, "close", explode)
    recorder.stop(end_reason="end_phrase")  # the daemon must survive this
    assert recorder.active() is None and events.TEE is None
    assert "[record] finalize failed" in capsys.readouterr().out


def test_echo_delay_pads_the_echo_track(rec_env):
    rec = recorder.start("voice")
    rec.set_echoecho_delay(0.1)  # 100 ms in+out device latency
    rec.write_echoecho(b"\x02\x00" * 240)
    rec.write_mic(b"\x01\x00" * 240)
    recorder.stop()
    _, echo_track = read_wav(rec.dir / "echoecho.wav")
    assert len(echo_track) == 2400 + 240  # latency shim + real audio
    assert set(echo_track[:2400]) == {0} and echo_track[2400] == 2
    _, mic = read_wav(rec.dir / "mic.wav")
    assert len(mic) == 240  # mic track unshifted
    meta = json.loads((rec.dir / "meta.json").read_text())
    assert meta["echoecho_delay_s"] == 0.1
    rec2 = recorder.SessionRecorder("voice", root=rec_env)
    rec2.set_echoecho_delay("not a number")  # never raises, never shifts
    assert rec2._echoecho_pad_frames == 0 and rec2.errors == 1
    rec2.set_echoecho_delay(99)  # absurd latency: capped at 2 s
    assert rec2._echoecho_pad_frames == 2 * 24000
    rec2.close()


def test_status_flags_land_in_meta(rec_env):
    rec = recorder.start("voice")
    audio = AudioIO()
    audio._in_callback(b"\x00\x00" * 10, 10, None, "input overflow")
    audio._out_callback(bytearray(20), 10, None, "output underflow")
    audio._out_callback(bytearray(20), 10, None, None)  # clean: not counted
    recorder.stop()
    meta = json.loads((rec.dir / "meta.json").read_text())
    assert meta["stream_status"] == {"input": 1, "output": 1}


def test_clean_session_meta_has_no_alarm_fields(rec_env):
    rec = recorder.start("voice")
    recorder.stop(end_reason="end_phrase")
    meta = json.loads((rec.dir / "meta.json").read_text())
    for key in ("stream_status", "writer_stalled", "echoecho_delay_s"):
        assert key not in meta


def test_same_second_wakes_never_clobber(rec_env):
    a = recorder.SessionRecorder("voice", root=rec_env, clock=lambda: 1000.0)
    b = recorder.SessionRecorder("voice", root=rec_env, clock=lambda: 1000.0)
    assert a.dir != b.dir and a.dir.exists() and b.dir.exists()
    a.close()
    b.close()
    assert b.dir.name.endswith("-2")


def test_stop_is_idempotent_and_start_replaces_stale(rec_env):
    rec = recorder.start("voice")
    rec2 = recorder.start("voice")  # forgot to stop: old one is finalized
    assert rec.closed and not rec2.closed
    recorder.stop()
    recorder.stop()  # double stop: no-op
    rec2.close()     # double close: no-op
    assert recorder.active() is None


# -- end-to-end wiring --------------------------------------------------------------


def scripted_args(tmp_path, lines):
    script = tmp_path / "script.txt"
    script.write_text("\n".join(lines) + "\n")
    return echoecho.parse_args(["--script", str(script)])


def test_scripted_run_records_when_opted_in(rec_env, tmp_path, monkeypatch):
    monkeypatch.setenv("ECHOECHO_RECORD", "1")
    monkeypatch.setenv("ECHOECHO_FAKE_LLM", "1")
    args = scripted_args(tmp_path, ["echoecho", "hello there", "that's it"])
    asyncio.run(echoecho.amain(args))
    dirs = session_dirs(rec_env)
    assert len(dirs) == 1 and dirs[0].name.endswith("_script")
    meta = json.loads((dirs[0] / "meta.json").read_text())
    assert meta["end_reason"] == "end_phrase"
    assert meta["mode"] == "script"
    transcript = (dirs[0] / "transcript.md").read_text()
    assert "you:** hello there" in transcript
    assert "state ACTIVE → ENDING (end_phrase)" in transcript


def test_scripted_run_does_not_record_by_default(rec_env, tmp_path, monkeypatch):
    monkeypatch.setenv("ECHOECHO_FAKE_LLM", "1")
    args = scripted_args(tmp_path, ["echoecho", "hi", "that's it"])
    asyncio.run(echoecho.amain(args))
    assert session_dirs(rec_env) == []


def test_amain_setup_failure_leaves_no_recording(rec_env, tmp_path, monkeypatch):
    """A crash during setup (before the try/finally) must not strand a
    forever-'(incomplete)' recording dir."""
    import echoecho_app.workers.base as workers_base
    monkeypatch.setenv("ECHOECHO_RECORD", "1")

    def boom():
        raise RuntimeError("registry exploded")
    monkeypatch.setattr(workers_base, "load_all", boom)
    args = scripted_args(tmp_path, ["echoecho", "hi", "that's it"])
    with pytest.raises(RuntimeError):
        asyncio.run(echoecho.amain(args))
    assert session_dirs(rec_env) == []
    assert recorder.active() is None


# -- the --recordings listing --------------------------------------------------------


def test_list_recordings_empty_and_table(rec_env, capsys):
    echoecho.list_recordings()
    assert "no recordings yet" in capsys.readouterr().out
    rec = recorder.start("voice")
    events.emit("user_text", text="hi")
    events.emit("assistant_text", text="hey")
    rec.write_mic(b"\x00\x00" * 24000)  # 1 s
    recorder.stop(end_reason="end_phrase")
    (rec_env / "2026-01-01_000000_voice").mkdir()  # crashed session: no meta
    echoecho.list_recordings()
    out = capsys.readouterr().out
    assert rec.dir.name in out
    assert "0:01" in out and "1/1" in out and "end_phrase" in out
    assert "(incomplete)" in out
    assert "session.wav" in out  # the how-to-review pointer


def test_no_record_flag_maps_to_env(rec_env, monkeypatch, capsys):
    # keep the repo's real .env.local out of os.environ (hidden test-order
    # dependency on the Mac, where it holds OPENAI_API_KEY etc.)
    monkeypatch.setattr(config, "load_env_local", lambda path=None: None)
    monkeypatch.setenv("ECHOECHO_RECORD", "sentinel")  # restored by monkeypatch
    echoecho.main(["--no-record", "--recordings"])     # exits after the listing
    assert os.environ["ECHOECHO_RECORD"] == "0"
    assert "no recordings yet" in capsys.readouterr().out
