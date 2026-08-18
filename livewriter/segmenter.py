"""Delta stream -> utterances.

gpt-live-transcribe emits an append-only stream of word/punctuation deltas with
no turn detection, so the server decides where "utterances" end — the units the
formatter consumes. Boundaries, in priority order:

  * terminal punctuation (. ! ?) once a few words exist   -> emit immediately
  * a pause: no new delta for PAUSE_S                     -> emit
  * run-on speech: MAX_WORDS with no boundary             -> emit anyway

"stop" is the panic word (same contract as the mockup): the instant the
pending tail ends with "stop"/"stop." we fire on_stop and DISCARD the pending
buffer — words spoken right before a stop were interrupted, not content. A
bare "stop" without punctuation waits STOP_CONFIRM_S for more words before
firing, so "we should stop shipping bugs" does not halt the pen mid-sentence.

The class is sans-IO: the owner calls feed() per delta and tick() on a timer,
passing a monotonic now; tests drive it with a fake clock.
"""

import re

# gpt-live-transcribe delivers deltas in bursts a beat behind the audio, so a
# gap in the delta stream is a much noisier signal than a gap in speech —
# PAUSE_S must out-wait burst jitter or utterances split mid-clause.
PAUSE_S = 0.85
PAUSE_FRAG_S = 1.8  # tiny fragments ("Two,") tempt the formatter to complete
                    # them — hold them much longer in case the rest is coming
STOP_CONFIRM_S = 0.45
MAX_WORDS = 28
CLAUSE_WORDS = 14  # a , ; : — boundary this deep is a good enough seam
MIN_WORDS_FOR_PUNCT = 3

_STOP_TAIL = re.compile(r"(^|\s)stop\s*[.!?]?\s*$", re.IGNORECASE)
_STOP_PUNCT = re.compile(r"(^|\s)stop\s*[.!?]\s*$", re.IGNORECASE)


class Segmenter(object):
    def __init__(self, on_utterance, on_stop, on_ghost=None):
        self.on_utterance = on_utterance  # fn(text, t_first, t_last)
        self.on_stop = on_stop            # fn()
        self.on_ghost = on_ghost          # fn(pending_text, t_first) on change
        self.pending = ""
        self.t_first = None
        self.t_last = None
        self._stop_armed_at = None
        self.utt_count = 0

    def feed(self, delta, now):
        """One transcription delta (may be a word, punctuation, or fragment)."""
        if not delta:
            return
        if not self.pending:
            self.t_first = now
        self.pending += delta
        self.t_last = now
        self._stop_armed_at = None

        tail = self.pending.strip()
        if _STOP_PUNCT.search(tail):
            self._fire_stop()
            return
        if _STOP_TAIL.search(tail):
            # bare "stop" — arm; fires from tick() if no more words come
            self._stop_armed_at = now
            self._ghost()
            return

        words = len(tail.split())
        if tail and tail[-1] in ".!?" and words >= MIN_WORDS_FOR_PUNCT:
            self._emit(now)
            return
        if tail and tail[-1] in ",;:—" and words >= CLAUSE_WORDS:
            self._emit(now)
            return
        if words >= MAX_WORDS:
            self._emit(now)
            return
        self._ghost()

    def tick(self, now):
        """Call every ~100ms."""
        if self._stop_armed_at is not None and now - self._stop_armed_at >= STOP_CONFIRM_S:
            self._fire_stop()
            return
        tail = self.pending.strip()
        if tail and self.t_last is not None:
            gap = now - self.t_last
            need = PAUSE_S if len(tail.split()) >= 3 else PAUSE_FRAG_S
            if gap >= need:
                self._emit(now)

    def flush(self, now):
        """Force out whatever is pending (session end, typed input following)."""
        if self.pending.strip():
            self._emit(now)

    def clear(self):
        self.pending = ""
        self.t_first = None
        self.t_last = None
        self._stop_armed_at = None
        self._ghost()

    # -- internals ----------------------------------------------------------
    def _emit(self, now):
        text = self.pending.strip()
        self.pending = ""
        self._stop_armed_at = None
        t_first, t_last = self.t_first, self.t_last
        self.t_first = None
        self.t_last = None
        if not text:
            return
        self.utt_count += 1
        self._ghost()
        self.on_utterance(text, t_first, t_last)

    def _fire_stop(self):
        # words before "stop" were interrupted mid-thought: discard them.
        self.pending = ""
        self.t_first = None
        self.t_last = None
        self._stop_armed_at = None
        self._ghost()
        self.on_stop()

    def _ghost(self):
        if self.on_ghost is not None:
            self.on_ghost(self.pending.strip(), self.t_first)
