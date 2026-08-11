"""Session lifecycle FSM (IDLE/ACTIVE/ENDING) + turn-boundary injection gate.

Transport-agnostic: the voice client, text REPL and scripted agent all drive
the same object. Clock is injectable so the 600s silence timeout tests fast.
"""
import time

from echo_app import config

IDLE = "IDLE"
ACTIVE = "ACTIVE"
ENDING = "ENDING"


class Session:
    def __init__(self, clock=time.monotonic, silence_timeout=None,
                 on_state_change=None, wake_pause=None, wake_resume=None):
        self.clock = clock
        self.silence_timeout = (config.silence_timeout()
                                if silence_timeout is None else silence_timeout)
        self.on_state_change = on_state_change or (lambda old, new, reason: None)
        self.wake_pause = wake_pause or (lambda: None)      # pause wake feed on ACTIVE
        self.wake_resume = wake_resume or (lambda: None)    # re-arm wake loop on IDLE
        self.state = IDLE
        self.end_reason = None
        self._last_activity = self.clock()
        # injection gate bookkeeping
        self._pending = []
        self._user_speaking = False
        self._at_turn_boundary = False

    # -- transitions ------------------------------------------------------

    def _move(self, new, reason=""):
        old, self.state = self.state, new
        self.on_state_change(old, new, reason)

    def wake(self):
        """IDLE -> ACTIVE (wake phrase heard / typed / spacebar)."""
        if self.state != IDLE:
            return False
        self.wake_pause()  # Echo saying "echo" must not self-trigger
        self._last_activity = self.clock()
        self._user_speaking = False
        self._at_turn_boundary = False
        self.end_reason = None
        self._move(ACTIVE, "wake")
        return True

    def begin_ending(self, reason):
        """ACTIVE -> ENDING (end phrase, end_session tool, or silence timeout)."""
        if self.state != ACTIVE:
            return False
        self.end_reason = reason
        self._move(ENDING, reason)
        return True

    def finish(self):
        """ENDING -> IDLE (after sign-off; wake loop re-armed, workers keep running)."""
        if self.state != ENDING:
            return False
        self._move(IDLE, self.end_reason or "finished")
        self.wake_resume()
        return True

    # -- activity / silence timer -----------------------------------------

    def note_user_speech_started(self):
        self._user_speaking = True
        self._at_turn_boundary = False
        self._last_activity = self.clock()

    def note_user_speech_stopped(self):
        self._user_speaking = False

    def note_assistant_response_started(self):
        """A response is in flight (response.created): not a turn boundary."""
        self._at_turn_boundary = False

    def note_assistant_response_done(self):
        """A completed assistant response is a safe turn boundary; also resets clock."""
        self._at_turn_boundary = True
        self._last_activity = self.clock()

    def check_silence(self):
        """Call periodically; ends the session after silence_timeout of no activity."""
        if self.state == ACTIVE and \
                self.clock() - self._last_activity >= self.silence_timeout:
            return self.begin_ending("silence_timeout")
        return False

    def seconds_of_silence(self):
        return self.clock() - self._last_activity

    # -- end phrase --------------------------------------------------------

    def handle_transcript(self, text):
        """Regex belt-and-suspenders for 'that's it' / 'that is all' etc."""
        if self.state == ACTIVE and config.END_PHRASE_RE.search(text):
            return self.begin_ending("end_phrase")
        return False

    # -- injection gate (the seam the realtime client reuses in PR 4) ------

    def queue_injection(self, injection):
        self._pending.append(injection)

    def can_inject(self):
        """Safe turn boundary: last event was a completed response and the user
        isn't mid-utterance."""
        return (self.state == ACTIVE and self._at_turn_boundary
                and not self._user_speaking)

    def drain_injections(self):
        """Return (and clear) pending injections if the gate is open, else []."""
        if not self.can_inject() or not self._pending:
            return []
        pending, self._pending = self._pending, []
        return pending

    @property
    def pending_injections(self):
        return list(self._pending)
