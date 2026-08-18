"""Session lifecycle FSM (IDLE/ACTIVE/ENDING) + turn-boundary injection gate.

Transport-agnostic: the voice client, text REPL and scripted agent all drive
the same object. Clock is injectable so the 600s silence timeout tests fast.
"""
import time

from echoecho_app import config, diagnostics, events

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
        self._pending_at = []
        self._pending_high_water = 0
        self._user_speaking = False
        self._at_turn_boundary = False

    # -- transitions ------------------------------------------------------

    def _move(self, new, reason=""):
        old, self.state = self.state, new
        events.emit("state", frm=old, to=new, reason=reason)  # UI feed only
        diagnostics.info("session.state.changed", frm=old, to=new,
                         reason=reason,
                         pending_injections=len(self._pending))
        self.on_state_change(old, new, reason)

    def wake(self):
        """IDLE -> ACTIVE (wake phrase heard / typed / spacebar)."""
        if self.state != IDLE:
            diagnostics.debug("session.transition.ignored", action="wake",
                              state=self.state)
            return False
        self.wake_pause()  # echoecho saying "echo" must not self-trigger
        self._last_activity = self.clock()
        self._user_speaking = False
        self._at_turn_boundary = False
        self.end_reason = None
        self._move(ACTIVE, "wake")
        return True

    def begin_ending(self, reason):
        """ACTIVE -> ENDING (end phrase, end_session tool, or silence timeout)."""
        if self.state != ACTIVE:
            diagnostics.debug("session.transition.ignored",
                              action="begin_ending", state=self.state,
                              reason=reason)
            return False
        self.end_reason = reason
        self._move(ENDING, reason)
        return True

    def finish(self):
        """ENDING -> IDLE (after sign-off; wake loop re-armed, workers keep running)."""
        if self.state != ENDING:
            diagnostics.debug("session.transition.ignored", action="finish",
                              state=self.state)
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
            diagnostics.info("session.silence_timeout",
                             silence_s=round(self.seconds_of_silence(), 2),
                             threshold_s=self.silence_timeout)
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
        try:
            queued_at = self.clock()
        except Exception:
            queued_at = None
        self._pending.append(injection)
        self._pending_at.append(queued_at)
        self._pending_high_water = max(self._pending_high_water,
                                       len(self._pending))
        diagnostics.info("session.injection.queued",
                         priority=getattr(injection, "priority", None),
                         queue_depth=len(self._pending),
                         queue_high_water=self._pending_high_water)

    def can_inject(self):
        """Safe turn boundary: last event was a completed response and the user
        isn't mid-utterance."""
        return (self.state == ACTIVE and self._at_turn_boundary
                and not self._user_speaking)

    def drain_injections(self):
        """Return (and clear) pending injections if the gate is open, else []."""
        if not self.can_inject() or not self._pending:
            return []
        valid_times = [value for value in self._pending_at
                       if isinstance(value, (int, float))]
        try:
            now = self.clock() if valid_times else None
        except Exception:
            now = None
        oldest_ms = ((now - min(valid_times)) * 1000
                     if now is not None and valid_times else None)
        pending, self._pending = self._pending, []
        self._pending_at = []
        diagnostics.info("session.injection.drained", count=len(pending),
                         oldest_age_ms=(round(max(0.0, oldest_ms), 1)
                                        if oldest_ms is not None else None))
        return pending

    @property
    def pending_injections(self):
        return list(self._pending)
