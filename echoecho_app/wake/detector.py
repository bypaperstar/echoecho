"""Vosk grammar-mode wake-word spotting: pure detect(chunk)->bool, no audio I/O.

Grammar '["echo", "[unk]"]' restricts decoding to the wake word plus filler;
we fire only when a partial OR final contains the doubled phrase "echo echo"
(a single "echo" in normal speech decodes as '[unk] echo [unk]' and stays
silent). After a trigger the recognizer is reset so one utterance can't fire
twice. Runs identically in the Linux sandbox and on the Mac; ~2% of one core
streaming live (measured 51x realtime).
"""
import json
import time

from echoecho_app import config, diagnostics

WAKE_DECODED_PHRASE = config.WAKE_DECODED_PHRASE


class WakeDetector:
    """Feed 16 kHz mono int16 chunks (100 ms = 3200 bytes) to detect()."""

    def __init__(self, model_dir=None, rate=16000):
        started = time.monotonic()
        try:
            import vosk  # heavy: only loaded where a detector is actually built
            vosk.SetLogLevel(-1)
            self.rate = rate
            self._model = vosk.Model(str(model_dir or config.VOSK_MODEL_DIR))
            self._rec = vosk.KaldiRecognizer(
                self._model, rate, json.dumps(["echo", "[unk]"]))
            self._rec.SetPartialWords(True)
        except Exception as exc:
            diagnostics.exception(
                "wake.detector.load_failed", exc=exc, rate=rate,
                custom_model=model_dir is not None)
            raise
        self._suspended = False
        self._chunks = 0
        self._audio_bytes = 0
        self._detections = 0
        diagnostics.info(
            "wake.detector.loaded", rate=rate,
            custom_model=model_dir is not None,
            duration_ms=round((time.monotonic() - started) * 1000, 1))

    def detect(self, chunk_bytes):
        """True exactly once per heard "echoecho"; False while suspended."""
        if self._suspended:
            return False
        self._chunks += 1
        self._audio_bytes += len(chunk_bytes)
        try:
            if self._rec.AcceptWaveform(chunk_bytes):
                text = json.loads(self._rec.Result()).get("text", "")
            else:
                text = json.loads(self._rec.PartialResult()).get("partial", "")
        except Exception as exc:
            diagnostics.exception(
                "wake.detector.decode_failed", exc=exc,
                chunk_count=self._chunks, audio_bytes=self._audio_bytes)
            raise
        if WAKE_DECODED_PHRASE in text:
            self._rec.Reset()
            self._detections += 1
            diagnostics.info(
                "wake.detector.triggered", detection_count=self._detections,
                chunk_count=self._chunks, audio_bytes=self._audio_bytes)
            return True
        return False

    # -- suspend during ACTIVE sessions (echoecho saying "echo" must not
    # self-trigger; the session FSM wires wake_pause/wake_resume here) -----

    def suspend(self):
        self._suspended = True
        self._rec.Reset()  # drop any half-decoded utterance
        diagnostics.info(
            "wake.detector.suspended", detection_count=self._detections,
            chunk_count=self._chunks)

    def resume(self):
        self._rec.Reset()  # fresh recognizer state for the next wake
        self._suspended = False
        diagnostics.info(
            "wake.detector.resumed", detection_count=self._detections,
            chunk_count=self._chunks)

    @property
    def suspended(self):
        return self._suspended

    def telemetry(self):
        """Metadata-only liveness snapshot; decoded speech never leaves Vosk."""
        return {
            "chunk_count": self._chunks,
            "audio_bytes": self._audio_bytes,
            "detection_count": self._detections,
            "suspended": self._suspended,
        }
