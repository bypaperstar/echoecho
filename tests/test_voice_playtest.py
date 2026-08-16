"""Silent voice E2E harness (scripts/voice_playtest.py) — the pure parts.

The harness itself only runs on a Mac with a loopback audio device; its
audio math, scenario checks, and event-feed tailing are plain Python and are
pinned down here so a refactor can't silently bend the assertions the Mac
run depends on.
"""
import array
import importlib.util
import json
import math
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "voice_playtest.py"


@pytest.fixture(scope="module")
def vp():
    spec = importlib.util.spec_from_file_location("voice_playtest", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def tone(seconds, rate=24000, amp=8000):
    return array.array("h", (int(amp * math.sin(i / 10.0))
                             for i in range(int(rate * seconds))))


# -- audio math ----------------------------------------------------------------


def test_voiced_seconds_ignores_silence_and_counts_speech(vp):
    silence = array.array("h", [0] * 24000)
    assert vp.voiced_seconds(silence, 24000) == 0.0
    assert vp.voiced_seconds(tone(1.0), 24000) >= 0.9


def test_voiced_seconds_threshold_is_int16_rms(vp):
    quiet = array.array("h", [50, -50] * 12000)  # RMS 50: below threshold
    assert vp.voiced_seconds(quiet, 24000) == 0.0


def test_resample_and_normalize_roundtrip(vp):
    src = tone(1.0, rate=22050, amp=2000)
    out = vp.resample_pcm(src, 22050, 16000)
    assert abs(len(out) - 16000) <= 2
    normed = vp.normalize_pcm(out, peak=0.7)
    top = max(abs(s) for s in normed)
    # gain is capped at 4x, so a quiet source lands at 4x, not full peak
    assert top == pytest.approx(min(0.7 * 32767, 2000 * 4), rel=0.05)


def test_voiced_outside_subtracts_injection_windows(vp):
    mon = vp.LoopMonitor.__new__(vp.LoopMonitor)
    mon.spans = [(10.0, 13.0), (20.0, 21.0)]
    # first span: 2 of 3 s overlap an injection window (plus 0.1 s slop each
    # side); second span is entirely echoecho
    got = mon.voiced_outside([(10.0, 12.0)])
    assert got == pytest.approx((13.0 - 12.1) + 1.0)


# -- scenario checks -----------------------------------------------------------


def test_check_files_bullets_glob_and_exists(vp, tmp_path):
    (tmp_path / "list.md").write_text(
        "# packing\n- tent\n- sleeping bag\n* water\n- sunscreen\n",
        encoding="utf-8")
    (tmp_path / "screens" / "t1").mkdir(parents=True)
    (tmp_path / "screens" / "t1" / "shot.png").write_bytes(b"\x89PNG")
    res = vp.check_files({"checks": [
        {"file_glob": "*.md", "contains_any": ["tent"], "min_bullets": 4},
        {"file_glob": "*.md", "contains_any": ["kayak"]},
        {"file_glob": "screens/**/*.png", "exists_only": True},
        {"file": "missing.md", "contains_any": ["x"]},
    ]}, tmp_path)
    assert [c["pass"] for c in res] == [True, False, True, False]


def test_check_events_counts_with_where_filter(vp, tmp_path):
    feed = tmp_path / ".events.jsonl"
    feed.write_text("\n".join(json.dumps(e) for e in [
        {"type": "wake", "via": "voice"},
        {"type": "wake", "via": "manual"},
        {"type": "task", "status": "done"},
    ]) + "\n", encoding="utf-8")
    tail = vp.EventTail(feed)
    res = vp.check_events({"event_checks": [
        {"type": "wake", "where": {"via": "voice"}, "count_min": 1,
         "count_max": 1},
        {"type": "task", "where": {"status": "done"}, "count_min": 2},
    ]}, tail)
    assert [c["pass"] for c in res] == [True, False]


def test_check_audio_reads_recording_meta(vp, tmp_path):
    rec = tmp_path / "2026-01-01_000000_voice"
    rec.mkdir()
    (rec / "meta.json").write_text(json.dumps(
        {"user_turns": 2, "assistant_turns": 3}), encoding="utf-8")
    mon = vp.LoopMonitor.__new__(vp.LoopMonitor)
    mon.spans = []
    inj = vp.Injector.__new__(vp.Injector)
    inj.windows = []
    res = vp.check_audio({"audio_checks": {
        "meta_user_turns_min": 2, "meta_assistant_turns_min": 4,
    }}, mon, inj, tmp_path)
    assert [c["pass"] for c in res] == [True, False]


# -- event feed tail -----------------------------------------------------------


def test_event_tail_incremental_wait_and_count(vp, tmp_path):
    feed = tmp_path / ".events.jsonl"
    tail = vp.EventTail(feed)
    assert tail.wait(vp.ev_match("wake"), 0.3) is None  # missing file: no crash
    feed.write_text(json.dumps({"type": "wake", "via": "voice"}) + "\n",
                    encoding="utf-8")
    assert tail.wait(vp.ev_match("wake", via="voice"), 2, start=0) is not None
    with open(feed, "a", encoding="utf-8") as f:
        f.write("not json\n")
        f.write(json.dumps({"type": "task", "status": "done"}) + "\n")
    assert tail.wait(vp.ev_match("task", status="done"), 2, start=0) is not None
    assert tail.count(vp.ev_match("wake")) == 1


def test_scenarios_ship_valid_and_slow_is_opt_in(vp):
    default = {s["name"] for s in vp.load_scenarios()}
    everything = {s["name"] for s in vp.load_scenarios(include_slow=True)}
    assert default  # fixtures parse
    assert "60_vm_computer_use" in everything - default
    for s in vp.load_scenarios(include_slow=True):
        assert s["turns"] and s["name"]
        assert "~wake" in s["turns"][0:2]  # every scenario begins by waking
