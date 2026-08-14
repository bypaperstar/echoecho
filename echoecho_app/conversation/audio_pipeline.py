"""Realtime microphone processing for speaker-safe, full-duplex voice.

The native WebRTC Audio Processing Module (APM) removes the speaker signal
from the microphone before it reaches Realtime server VAD.  This adapter owns
the I/O contract around it:

* APM consumes exactly 10 ms pcm16 mono frames.
* The reverse input is audio that actually reached the speaker, never audio
  that was merely queued for playback.
* Render and capture arrive on different PortAudio callback threads, so calls
  into the shared native processor are serialized per 10 ms frame.
* A conservative residual gate silences only low-level post-AEC audio during
  playback and its acoustic tail.  Detected near speech and its short
  hangover pass through, preserving barge-in.
* If native processing fails, capture stays available.  During playback it
  falls back to suppression (no barge-in); outside playback it passes raw.

``livekit`` is imported lazily so text mode and headless imports do not depend
on the Mac audio requirements.
"""
import math
import threading
import time

import audioop


def _rms(pcm_bytes):
    """RMS of little-endian pcm16; zero for empty or malformed input."""
    if not pcm_bytes or len(pcm_bytes) % 2:
        return 0.0
    return float(audioop.rms(pcm_bytes, 2))


class AudioPipeline:
    """WebRTC AEC adapter for one full-duplex audio session.

    ``process_render`` and ``process_capture`` accept any whole-sample callback
    size and preserve the capture byte count. Echo's normal 20 ms callbacks
    contain two 10 ms APM frames; a shorter suffix passes through unchanged as
    a fail-safe.
    """

    MAX_DELAY_MS = 500

    def __init__(self, rate=24000, stream_delay_ms=0,
                 enable_aec=True, enable_noise_suppression=False,
                 enable_high_pass=True, enable_agc=False,
                 render_active_rms=80.0, near_speech_rms=180.0,
                 echo_tail_ms=200, near_speech_hangover_ms=250,
                 clock=time.monotonic):
        self.rate = int(rate)
        if self.rate <= 0 or self.rate % 100:
            raise ValueError("audio rate must contain an integer 10 ms frame")
        self.frame_samples = self.rate // 100
        self.frame_bytes = self.frame_samples * 2
        self.stream_delay_ms = self._clamp_delay(stream_delay_ms)
        self.render_active_rms = max(0.0, float(render_active_rms))
        self.near_speech_rms = max(0.0, float(near_speech_rms))
        self.echo_tail_s = max(0.0, float(echo_tail_ms) / 1000.0)
        self.near_speech_hangover_s = max(
            0.0, float(near_speech_hangover_ms) / 1000.0)
        self._clock = clock
        self._aec_enabled = bool(enable_aec)
        self._options = dict(
            echo_cancellation=self._aec_enabled,
            noise_suppression=bool(enable_noise_suppression),
            high_pass_filter=bool(enable_high_pass),
            auto_gain_control=bool(enable_agc))

        self._apm_lock = threading.Lock()
        self._render_buffer_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._apm = None
        self._AudioFrame = None
        self._render_tail = bytearray()
        self._render_active_frames = 0
        self._render_frame_deadline = 0.0
        self._render_active_until = 0.0
        self._near_speech_until = 0.0
        self._capture_has_near_speech = False
        self._disabled_reason = None
        self._errors = 0
        self._malformed = 0
        self._render_frames = 0
        self._capture_frames = 0
        self._gated_frames = 0
        self._fallback_gated_frames = 0
        self._double_talk_frames = 0
        self._delay_min = None
        self._delay_max = None
        self._init_apm()

    @classmethod
    def _clamp_delay(cls, delay_ms):
        try:
            delay = float(delay_ms)
        except (TypeError, ValueError):
            return 0
        if not math.isfinite(delay):
            return 0
        return max(0, min(cls.MAX_DELAY_MS, int(delay)))

    def _init_apm(self):
        try:
            from livekit import rtc
            self._AudioFrame = rtc.AudioFrame
            self._apm = rtc.AudioProcessingModule(**self._options)
        except Exception as exc:
            self._apm = None
            self._AudioFrame = None
            self._disabled_reason = str(exc) or exc.__class__.__name__

    @property
    def enabled(self):
        with self._apm_lock:
            return self._apm is not None

    @property
    def disabled_reason(self):
        with self._state_lock:
            return self._disabled_reason

    @property
    def capture_has_near_speech(self):
        with self._state_lock:
            return self._capture_has_near_speech

    def stats(self):
        with self._apm_lock:
            enabled = self._apm is not None
        with self._state_lock:
            return {
                "enabled": enabled,
                "disabled_reason": self._disabled_reason,
                "render_frames": self._render_frames,
                "capture_frames": self._capture_frames,
                "gated_frames": self._gated_frames,
                "fallback_gated_frames": self._fallback_gated_frames,
                "double_talk_frames": self._double_talk_frames,
                "errors": self._errors,
                "malformed_frames": self._malformed,
                "near_speech": self._capture_has_near_speech,
                "stream_delay_ms": self.stream_delay_ms,
                "delay_min_ms": self._delay_min,
                "delay_max_ms": self._delay_max,
            }

    def _frame(self, data):
        # bytearray is writable: AudioProcessingModule mutates capture in place.
        return self._AudioFrame(bytearray(data), self.rate, 1,
                                self.frame_samples)

    def _note_render_activity(self, data):
        duration_s = len(data) / float(self.rate * 2)
        now = self._clock()
        # Track the rendered timeline in 10 ms units. Unlike a wall-clock-only
        # gate, this remains correct in fast headless callback simulations.
        callback_frames = max(1, int(math.ceil(
            len(data) / float(self.frame_bytes))))
        if _rms(data) < self.render_active_rms:
            with self._state_lock:
                self._render_active_frames = max(
                    0, self._render_active_frames - callback_frames)
            return
        # The callback hands a future-duration buffer to the device. Keep the
        # gate open for that duration, the estimated device path, and the room
        # tail after its last sample.
        delay_s = self.stream_delay_ms / 1000.0
        active_until = now + duration_s + delay_s + self.echo_tail_s
        active_frames = callback_frames + int(math.ceil(
            (delay_s + self.echo_tail_s) * 100.0))
        with self._state_lock:
            self._render_active_until = max(self._render_active_until,
                                            active_until)
            if now >= self._render_frame_deadline:
                self._render_active_frames = 0
            self._render_active_frames = max(self._render_active_frames,
                                             active_frames)
            self._render_frame_deadline = active_until

    def process_render(self, pcm_bytes):
        """Supply exact speaker PCM to APM's reverse path."""
        data = bytes(pcm_bytes)
        if not data:
            return
        if len(data) % 2:
            with self._state_lock:
                self._malformed += 1
            return
        self._note_render_activity(data)
        if not self._aec_enabled:
            return

        # Copy complete frames out under a tiny buffer lock. Native calls use
        # a separate lock per frame, avoiding a callback-sized critical
        # section that can priority-invert the other PortAudio thread.
        chunks = []
        with self._render_buffer_lock:
            self._render_tail.extend(data)
            while len(self._render_tail) >= self.frame_bytes:
                chunks.append(bytes(self._render_tail[:self.frame_bytes]))
                del self._render_tail[:self.frame_bytes]
        for chunk in chunks:
            with self._apm_lock:
                if self._apm is None:
                    return
                try:
                    self._apm.process_reverse_stream(self._frame(chunk))
                except Exception as exc:
                    self._fail_locked(exc)
                    return
            with self._state_lock:
                self._render_frames += 1

    def _gate_frame(self, cleaned, native_enabled, now):
        """Suppress low residual echo, while preserving near speech gaps."""
        level = _rms(cleaned)
        with self._state_lock:
            if now >= self._render_frame_deadline:
                self._render_active_frames = 0
            render_active = (now < self._render_active_until
                             or self._render_active_frames > 0)
            if self._render_active_frames > 0:
                self._render_active_frames -= 1
            if not native_enabled:
                # Safe degraded mode: no reference cancellation means speaker
                # audio is indistinguishable from the user. Suppress it only
                # while it can be acoustic echo, and pass raw mic otherwise.
                self._capture_has_near_speech = False
                if render_active:
                    self._fallback_gated_frames += 1
                    return b"\x00" * len(cleaned)
                self._capture_has_near_speech = level >= self.near_speech_rms
                return cleaned

            near = level >= self.near_speech_rms
            if near:
                self._capture_has_near_speech = True
                self._near_speech_until = max(
                    self._near_speech_until,
                    now + self.near_speech_hangover_s)
                if render_active:
                    self._double_talk_frames += 1
                return cleaned
            self._capture_has_near_speech = False
            if now < self._near_speech_until:
                return cleaned
            if render_active:
                self._gated_frames += 1
                return b"\x00" * len(cleaned)
            return cleaned

    def process_capture(self, pcm_bytes, stream_delay_ms=None):
        """Return AEC-cleaned PCM, preserving the callback's byte length."""
        data = bytes(pcm_bytes)
        if not data:
            return data
        if len(data) % 2:
            with self._state_lock:
                self._malformed += 1
            return data
        if stream_delay_ms is not None:
            self.set_stream_delay_ms(stream_delay_ms)

        out = bytearray()
        complete = len(data) - (len(data) % self.frame_bytes)
        for offset in range(0, complete, self.frame_bytes):
            raw = data[offset:offset + self.frame_bytes]
            native_enabled = False
            with self._apm_lock:
                if self._apm is not None:
                    try:
                        if self._aec_enabled:
                            self._apm.set_stream_delay_ms(self.stream_delay_ms)
                        frame = self._frame(raw)
                        self._apm.process_stream(frame)
                        cleaned = bytes(frame.data.cast("B"))
                        native_enabled = True
                    except Exception as exc:
                        self._fail_locked(exc)
                        # Never mix processed and raw audio within a callback;
                        # a discontinuity can itself trip VAD.
                        with self._state_lock:
                            self._capture_has_near_speech = False
                        return self._fallback_callback(data)
                else:
                    cleaned = raw
            if native_enabled:
                with self._state_lock:
                    self._capture_frames += 1
            out.extend(self._gate_frame(cleaned,
                                        native_enabled and self._aec_enabled,
                                        self._clock()))

        # Echo guarantees frame-aligned callbacks. Preserve a surprise suffix
        # rather than shifting its timeline into the following callback.
        if complete < len(data):
            with self._state_lock:
                self._malformed += 1
            out.extend(data[complete:])
        return bytes(out)

    def _fallback_callback(self, data):
        now = self._clock()
        with self._state_lock:
            if now >= self._render_frame_deadline:
                self._render_active_frames = 0
            render_active = now < self._render_active_until
            render_active = render_active or self._render_active_frames > 0
            if self._render_active_frames > 0:
                self._render_active_frames = max(
                    0, self._render_active_frames
                    - max(1, len(data) // self.frame_bytes))
            if render_active:
                frames = max(1, len(data) // self.frame_bytes)
                self._fallback_gated_frames += frames
                return b"\x00" * len(data)
            self._capture_has_near_speech = (
                _rms(data) >= self.near_speech_rms)
        return data

    def set_stream_delay_ms(self, delay_ms):
        delay = self._clamp_delay(delay_ms)
        with self._state_lock:
            self.stream_delay_ms = delay
            self._delay_min = delay if self._delay_min is None else min(
                self._delay_min, delay)
            self._delay_max = delay if self._delay_max is None else max(
                self._delay_max, delay)

    @staticmethod
    def _dispose(apm):
        # LiveKit currently exposes no public close() on APM. Dispose its
        # FFI handle eagerly: leaving it to interpreter shutdown can race the
        # native runtime teardown. This is intentionally best-effort.
        handle = getattr(apm, "_ffi_handle", None)
        try:
            if handle is not None:
                handle.dispose()
        except Exception:
            pass

    def _fail_locked(self, exc):
        """Disable APM after a native error. Caller holds ``_apm_lock``."""
        apm, self._apm = self._apm, None
        with self._state_lock:
            self._errors += 1
            self._disabled_reason = str(exc) or exc.__class__.__name__
        self._dispose(apm)

    def close(self):
        """Idempotently release native state after both audio streams stop."""
        with self._apm_lock:
            apm, self._apm = self._apm, None
            self._dispose(apm)
        with self._render_buffer_lock:
            self._render_tail.clear()
        with self._state_lock:
            self._capture_has_near_speech = False
            self._render_active_frames = 0
            self._render_frame_deadline = 0.0
            self._render_active_until = 0.0
            self._near_speech_until = 0.0
