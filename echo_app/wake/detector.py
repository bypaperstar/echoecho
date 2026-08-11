"""Vosk grammar-mode wake-word spotting: pure detect(chunk)->bool, no audio I/O.

Grammar '["echo", "[unk]"]' restricts decoding to the wake word plus filler;
we fire only when a partial OR final contains the doubled phrase "echo echo"
(a single "echo" in normal speech decodes as '[unk] echo [unk]' and stays
silent). After a trigger the recognizer is reset so one utterance can't fire
twice. Runs identically in the Linux sandbox and on the Mac; ~2% of one core
streaming live (measured 51x realtime).
"""
import json

from echo_app import config

WAKE_PHRASE = config.WAKE_PHRASE  # "echo echo"


class WakeDetector:
    """Feed 16 kHz mono int16 chunks (100 ms = 3200 bytes) to detect()."""

    def __init__(self, model_dir=None, rate=16000):
        import vosk  # heavy: only loaded where a detector is actually built
        vosk.SetLogLevel(-1)
        self.rate = rate
        self._model = vosk.Model(str(model_dir or config.VOSK_MODEL_DIR))
        self._rec = vosk.KaldiRecognizer(
            self._model, rate, json.dumps(["echo", "[unk]"]))
        self._rec.SetPartialWords(True)
        self._suspended = False

    def detect(self, chunk_bytes):
        """True exactly once per heard "echo echo"; False while suspended."""
        if self._suspended:
            return False
        if self._rec.AcceptWaveform(chunk_bytes):
            text = json.loads(self._rec.Result()).get("text", "")
        else:
            text = json.loads(self._rec.PartialResult()).get("partial", "")
        if WAKE_PHRASE in text:
            self._rec.Reset()
            return True
        return False

    # -- suspend during ACTIVE sessions (Echo saying "echo" must not
    # self-trigger; the session FSM wires wake_pause/wake_resume here) -----

    def suspend(self):
        self._suspended = True
        self._rec.Reset()  # drop any half-decoded utterance

    def resume(self):
        self._rec.Reset()  # fresh recognizer state for the next wake
        self._suspended = False

    @property
    def suspended(self):
        return self._suspended
