"""Wake-word detector over the pre-generated fixture WAVs, plus the pure
(audio-hardware-free) parts of conversation/audio.py.

The fixtures are 22.05 kHz piper TTS; we resample to 16 kHz with
audioop.ratecv. NOTE: audioop is removed in Python 3.13 — this resample is
TEST-ONLY glue for the 22.05 kHz fixtures; the live mic (wake/mic.py)
captures 16 kHz natively and never touches audioop.

A 1-second silence tail is appended to every fixture before chunking: the
recognizer only finalizes an utterance after trailing silence, which a live
always-on mic supplies for free but a WAV that ends flush with the speech
does not.

Empirical decoy behavior (vosk-model-small-en-us-0.15, grammar
'["echo","[unk]"]', 3200-byte chunks): decoy_single_echo -> '[unk] echo
[unk]', decoy_speech -> '[unk]', decoy_gecko -> '[unk] echo [unk] echo' —
none contain the contiguous doubled phrase, so none fire. ("gecko" does
decode its 'echo' tail, but never doubled; no README false-fire note needed
for PR 6.)
"""
import asyncio
import audioop  # TEST-ONLY: removed in Python 3.13 (see module docstring)
import base64
import sys
import wave

import pytest

from echo_app import config

pytestmark = pytest.mark.skipif(
    not config.VOSK_MODEL_DIR.is_dir(),
    reason="Vosk model not downloaded (run scripts/fetch_models.sh)")

AUDIO_DIR = config.FIXTURES_DIR / "audio"
CHUNK = 3200  # 100 ms of 16 kHz mono int16


def load_pcm16k(name, tail_silence_secs=1.0):
    with wave.open(str(AUDIO_DIR / name)) as w:
        assert w.getframerate() == 22050 and w.getnchannels() == 1
        pcm = w.readframes(w.getnframes())
    pcm, _ = audioop.ratecv(pcm, 2, 1, 22050, 16000, None)
    return pcm + b"\x00" * int(2 * 16000 * tail_silence_secs)


def feed(detector, pcm):
    """Stream 100 ms chunks exactly like the live wake loop; True if fired."""
    fired = False
    for i in range(0, len(pcm), CHUNK):
        fired = detector.detect(pcm[i:i + CHUNK]) or fired
    return fired


@pytest.fixture(scope="module")
def detector():
    from echo_app.wake.detector import WakeDetector
    return WakeDetector()


@pytest.mark.parametrize("name", ["wake_echo_echo.wav",
                                  "wake_echo_echo_context.wav"])
def test_wake_phrases_fire(detector, name):
    assert feed(detector, load_pcm16k(name))


@pytest.mark.parametrize("name", ["decoy_single_echo.wav",
                                  "decoy_speech.wav",
                                  "decoy_gecko.wav"])
def test_decoys_do_not_fire(detector, name):
    assert not feed(detector, load_pcm16k(name))


def test_reset_after_trigger_allows_refire(detector):
    pcm = load_pcm16k("wake_echo_echo.wav")
    assert feed(detector, pcm)
    assert feed(detector, pcm)  # recognizer was reset, fires again


def test_suspend_blocks_and_resume_rearms(detector):
    pcm = load_pcm16k("wake_echo_echo.wav")
    detector.suspend()
    assert not feed(detector, pcm)  # ACTIVE session: feed paused
    detector.resume()
    assert feed(detector, pcm)


# -- pure parts of conversation/audio.py (no sounddevice needed) -------------

def test_audio_modules_import_without_sounddevice():
    """Importing the Mac-only modules must not import sounddevice (lazy)."""
    before = "sounddevice" in sys.modules
    import echo_app.conversation.audio  # noqa: F401
    import echo_app.wake.mic  # noqa: F401
    assert ("sounddevice" in sys.modules) == before


def test_chimes_are_nonsilent_pcm16():
    from echo_app.conversation.audio import end_chime, wake_chime
    for chime in (wake_chime(), end_chime()):
        assert len(chime) == 2 * int(24000 * 0.09) * 2  # two 90ms notes, int16
        assert max(abs(s) for s in memoryview(chime).cast("h")) > 5000


def test_playback_buffer_advances_tracker_and_flushes():
    from echo_app.conversation.audio import AudioIO
    from echo_app.conversation.realtime import PlaybackTracker
    tracker = PlaybackTracker()
    io = AudioIO(tracker=tracker)
    tracker.append("item_1", 100.0)
    io.feed_pcm(b"\x01\x00" * 2400)  # 100 ms at 24 kHz
    out = bytearray(2400)  # a 1200-frame device callback = 50 ms
    io._out_callback(out, 1200, None, None)
    assert bytes(out) == b"\x01\x00" * 1200
    assert abs(tracker._played_ms - 50.0) < 1e-6
    assert abs(io.pending_ms() - 50.0) < 1e-6
    io.flush()  # barge-in
    assert io.pending_ms() == 0
    out2 = bytearray(2400)
    io._out_callback(out2, 1200, None, None)
    assert bytes(out2) == b"\x00" * 2400  # zero-padded silence, no crash


def test_capture_callback_sends_base64_append():
    """The PortAudio-thread capture callback must schedule a well-shaped
    input_audio_buffer.append onto the loop, and be a safe no-op after stop()
    nulls send_event (teardown race)."""
    from echo_app.conversation.audio import AudioIO
    io = AudioIO()
    sent = []

    async def scenario():
        async def send(ev):
            sent.append(ev)
        io.loop = asyncio.get_event_loop()
        io.send_event = send
        io._in_callback(b"\x01\x00" * 100, 100, None, None)
        await asyncio.sleep(0.05)
        io.send_event = None  # stop() from another thread
        io._in_callback(b"\x01\x00" * 100, 100, None, None)
        await asyncio.sleep(0.05)

    asyncio.run(scenario())
    assert len(sent) == 1
    assert sent[0]["type"] == "input_audio_buffer.append"
    assert base64.b64decode(sent[0]["audio"]) == b"\x01\x00" * 100


def _has_sounddevice():
    try:
        import sounddevice  # noqa: F401
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _has_sounddevice(),
                    reason="sounddevice not installed (Mac-only device paths)")
def test_device_open_paths_mac_only():
    from echo_app.wake.mic import WakeMic
    mic = WakeMic().start()
    assert mic.read(timeout=1.0) is not None
    mic.stop()
