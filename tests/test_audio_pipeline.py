"""Headless tests for full-duplex WebRTC echo cancellation.

The Linux LiveKit wheel contains the same WebRTC APM interface used on macOS,
so the actual native processor is exercised when installed.  Dependency-free
tests use a tiny fake to keep the adapter/callback contract covered in the
base test environment too.
"""
import asyncio
import base64
import math
import random
import struct
import sys
import types
from array import array

import pytest

from echoecho_app.conversation.audio import AudioIO
from echoecho_app.conversation.audio_pipeline import AudioPipeline, _rms


def pcm(samples):
    return struct.pack("<%dh" % len(samples), *samples)


class FakeFrame:
    def __init__(self, data, sample_rate, num_channels, samples_per_channel):
        self._data = bytearray(data)
        self.data = memoryview(self._data).cast("h")


class FakeApm:
    def __init__(self, **options):
        self.options = options
        self.reverse = []
        self.delays = []
        self.capture = []

    def process_reverse_stream(self, frame):
        self.reverse.append(bytes(frame._data))

    def set_stream_delay_ms(self, delay):
        self.delays.append(delay)

    def process_stream(self, frame):
        self.capture.append(bytes(frame._data))
        # Obvious deterministic mutation: halve every sample.
        for i, sample in enumerate(frame.data):
            frame.data[i] = sample // 2


@pytest.fixture
def fake_livekit(monkeypatch):
    rtc = types.SimpleNamespace(AudioFrame=FakeFrame,
                                AudioProcessingModule=FakeApm)
    module = types.ModuleType("livekit")
    module.rtc = rtc
    monkeypatch.setitem(sys.modules, "livekit", module)
    return module


def test_pipeline_slices_10ms_and_preserves_capture_length(fake_livekit):
    pipeline = AudioPipeline(rate=1000, stream_delay_ms=27,
                             near_speech_rms=2)
    raw = pcm(range(25))  # 25 ms => two processed frames + 5 ms pass-through
    cleaned = pipeline.process_capture(raw)
    got = list(array("h", cleaned))
    assert len(cleaned) == len(raw)
    assert got[:20] == [sample // 2 for sample in range(20)]
    assert got[20:] == list(range(20, 25))
    assert pipeline._apm.delays == [27, 27]
    assert pipeline.stats()["capture_frames"] == 2


def test_residual_gate_covers_render_tail_but_passes_near_speech(fake_livekit):
    now = [10.0]
    pipeline = AudioPipeline(rate=1000, clock=lambda: now[0],
                             render_active_rms=80, near_speech_rms=180,
                             echo_tail_ms=200, near_speech_hangover_ms=250)
    pipeline.process_render(pcm([500] * 10))

    # Fake APM halves capture: 100-RMS residual is below near-speech threshold.
    assert pipeline.process_capture(pcm([200] * 10)) == pcm([0] * 10)
    assert pipeline.stats()["gated_frames"] == 1

    # A strong independent near signal survives while render is active.
    assert pipeline.process_capture(pcm([800] * 10)) == pcm([400] * 10)
    assert pipeline.stats()["double_talk_frames"] == 1
    # Brief syllable gaps pass during near-speech hangover.
    assert pipeline.process_capture(pcm([100] * 10)) == pcm([50] * 10)

    # Advance through the remaining frame-accounted render/tail window, then
    # advance the wall clock past the same tail.
    for _ in range(30):
        pipeline.process_capture(pcm([100] * 10))
    now[0] += 0.5
    # After render and near-speech tails expire, quiet input passes normally.
    assert pipeline.process_capture(pcm([100] * 10)) == pcm([50] * 10)


def test_missing_apm_fallback_suppresses_only_during_playback(monkeypatch,
                                                              fake_livekit):
    now = [2.0]

    def broken(**options):
        raise RuntimeError("no native APM")

    fake_livekit.rtc.AudioProcessingModule = broken
    pipeline = AudioPipeline(rate=1000, clock=lambda: now[0])
    raw = pcm([900] * 10)
    pipeline.process_render(raw)
    assert not pipeline.enabled
    assert pipeline.process_capture(raw) == pcm([0] * 10)
    assert pipeline.stats()["fallback_gated_frames"] == 1
    now[0] += 1.0
    assert pipeline.process_capture(raw) == raw


def test_stream_delay_is_finite_clamped_and_observable(fake_livekit):
    pipeline = AudioPipeline(rate=1000)
    raw = pcm([500] * 10)
    pipeline.process_capture(raw, -5)
    pipeline.process_capture(raw, 9999)
    pipeline.process_capture(raw, float("nan"))
    assert pipeline._apm.delays == [0, 500, 0]
    stats = pipeline.stats()
    assert stats["delay_min_ms"] == 0
    assert stats["delay_max_ms"] == 500


def test_render_reference_is_frame_aligned_across_callbacks(fake_livekit):
    pipeline = AudioPipeline(rate=1000)
    pipeline.process_render(pcm(range(7)))
    assert pipeline._apm.reverse == []
    pipeline.process_render(pcm(range(7, 25)))
    assert len(pipeline._apm.reverse) == 2
    assert list(array("h", pipeline._apm.reverse[0])) == list(range(10))
    assert list(array("h", pipeline._apm.reverse[1])) == list(range(10, 20))
    assert list(array("h", pipeline._render_tail)) == list(range(20, 25))


def test_pipeline_is_fail_open_when_dependency_or_native_dsp_fails(monkeypatch,
                                                                   fake_livekit):
    class BrokenApm(FakeApm):
        def process_stream(self, frame):
            raise RuntimeError("native boom")

    fake_livekit.rtc.AudioProcessingModule = BrokenApm
    pipeline = AudioPipeline(rate=1000)
    raw = pcm([100] * 10)
    assert pipeline.process_capture(raw) == raw
    assert not pipeline.enabled
    assert pipeline.stats()["errors"] == 1
    # Disabled pipeline stays pass-through without active speaker audio.
    assert pipeline.process_capture(raw) == raw


def test_runtime_failure_uses_playback_gate_for_whole_callback(fake_livekit):
    class BrokenApm(FakeApm):
        def process_stream(self, frame):
            raise RuntimeError("native boom")

    fake_livekit.rtc.AudioProcessingModule = BrokenApm
    pipeline = AudioPipeline(rate=1000)
    raw = pcm([500] * 20)
    pipeline.process_render(raw)
    assert pipeline.process_capture(raw) == pcm([0] * 20)
    assert not pipeline.enabled
    assert pipeline.stats()["fallback_gated_frames"] == 2
    assert pipeline.stats()["errors"] == 1
    # Disabled pipeline remains conservatively gated for the echo tail.
    assert pipeline.process_capture(raw) == pcm([0] * 20)


def test_pcm_rms_handles_silence_and_signal():
    assert _rms(b"") == 0
    assert _rms(pcm([0] * 20)) == 0
    assert _rms(pcm([-100, 100] * 10)) == pytest.approx(100)


def test_audioio_records_raw_but_sends_processed(fake_livekit, monkeypatch):
    pipeline = AudioPipeline(rate=1000)
    audio = AudioIO(rate=1000, pipeline=pipeline)
    sent = []
    recorded = []
    rec = types.SimpleNamespace(write_mic=recorded.append,
                                note_status=lambda *args: None)
    monkeypatch.setattr("echoecho_app.conversation.audio.recorder.active",
                        lambda: rec)

    async def scenario():
        async def send(event):
            sent.append(event)

        audio.loop = asyncio.get_event_loop()
        audio.send_event = send
        audio._in_callback(pcm([100] * 10), 10, None, None)
        await asyncio.sleep(0)

    asyncio.run(scenario())
    assert len(sent) == 1
    assert recorded == [pcm([100] * 10)]
    assert list(array("h", base64.b64decode(sent[0]["audio"]))) == [50] * 10


def test_audioio_sender_can_be_enabled_after_stream_start(fake_livekit):
    pipeline = AudioPipeline(rate=1000)
    audio = AudioIO(rate=1000, pipeline=pipeline)
    sent = []

    async def scenario():
        async def send(event):
            sent.append(event)

        audio.loop = asyncio.get_event_loop()
        audio._in_callback(pcm([100] * 10), 10, None, None)
        audio.set_sender(send)
        audio._in_callback(pcm([100] * 10), 10, None, None)
        await asyncio.sleep(0)

    asyncio.run(scenario())
    assert len(sent) == 1


def test_output_callback_references_only_audio_that_reached_speaker(fake_livekit):
    pipeline = AudioPipeline(rate=1000)
    audio = AudioIO(rate=1000, pipeline=pipeline)
    audio.feed_pcm(pcm([9] * 20))
    audio.flush()  # queued but never rendered: it cannot be an AEC reference
    out = bytearray(20)
    audio._out_callback(out, 10, None, None)
    assert bytes(out) == pcm([0] * 10)
    assert pipeline._apm.reverse == [pcm([0] * 10)]


def _speech(seed, blocks, samples_per_block):
    """Deterministic, speech-band coloured noise for native APM tests."""
    rng = random.Random(seed)
    out, last = [], 0
    for _ in range(blocks):
        block = []
        for _ in range(samples_per_block):
            last = int(0.85 * last + rng.randint(-5000, 5000))
            last = max(-16000, min(16000, last))
            block.append(last)
        out.append(block)
    return out


def _native_pipeline_or_skip():
    try:
        from livekit import rtc  # noqa: F401
    except Exception as exc:
        pytest.skip("native LiveKit APM not installed: %s" % exc)
    return AudioPipeline(rate=24000, stream_delay_ms=30,
                         enable_noise_suppression=False,
                         enable_high_pass=False,
                         # These tests isolate native APM behavior; the
                         # separately tested residual gate must not mask it.
                         render_active_rms=float("inf"))


def test_native_apm_strongly_reduces_delayed_render_echo():
    pipeline = _native_pipeline_or_skip()
    n, delay = 240, 3
    render = _speech(7, 260, n)
    before, after = [], []
    try:
        for block in range(len(render)):
            pipeline.process_render(pcm(render[block]))
            echoed = ([int(0.55 * sample) for sample in render[block - delay]]
                      if block >= delay else [0] * n)
            cleaned = list(array("h", pipeline.process_capture(pcm(echoed))))
            if block >= 120:  # compare after the adaptive filter converges
                before.extend(echoed)
                after.extend(cleaned)
    finally:
        pipeline.close()
    assert _rms(pcm(after)) <= _rms(pcm(before)) * 0.25  # >=12 dB ERLE


def test_native_apm_preserves_near_only_speech_without_render():
    pipeline = _native_pipeline_or_skip()
    source = _speech(19, 80, 240)
    before, after = [], []
    try:
        for samples in source:
            pipeline.process_render(pcm([0] * 240))
            cleaned = list(array("h", pipeline.process_capture(pcm(samples))))
            before.extend(samples)
            after.extend(cleaned)
    finally:
        pipeline.close()
    # High-pass/AGC/NS are disabled for this assertion; AEC without a reverse
    # signal must not turn a real user into silence.
    assert _rms(pcm(after)) >= _rms(pcm(before)) * 0.80


def test_native_apm_preserves_voiced_double_talk():
    """Independent voiced near speech remains detectable over delayed echoecho."""
    pipeline = _native_pipeline_or_skip()
    n, delay, blocks = 240, 3, 300

    def voiced(freq, amplitude):
        result = []
        for block in range(blocks):
            samples = []
            for sample in range(n):
                t = (block * n + sample) / 24000.0
                env = 0.55 + 0.45 * math.sin(2 * math.pi * 3.1 * t) ** 2
                value = amplitude * env * (
                    0.65 * math.sin(2 * math.pi * freq * t)
                    + 0.25 * math.sin(4 * math.pi * freq * t)
                    + 0.10 * math.sin(6 * math.pi * freq * t))
                samples.append(int(value))
            result.append(samples)
        return result

    far, near = voiced(180, 8000), voiced(263, 6000)
    cleaned = []
    try:
        for block in range(blocks):
            pipeline.process_render(pcm(far[block]))
            echo = ([int(0.55 * sample) for sample in far[block - delay]]
                    if block >= delay else [0] * n)
            user = near[block] if block >= 120 else [0] * n
            mixed = [max(-32768, min(32767, a + b))
                     for a, b in zip(echo, user)]
            output = pipeline.process_capture(pcm(mixed))
            if block >= 150:
                cleaned.extend(array("h", output))
    finally:
        pipeline.close()
    # This is intentionally a VAD-oriented bound, not waveform fidelity:
    # near speech remains far above the 180-RMS residual-gate threshold.
    assert _rms(pcm(cleaned)) >= 600
