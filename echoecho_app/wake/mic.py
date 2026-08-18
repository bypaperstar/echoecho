"""Mac-only wake-loop mic capture: 16 kHz mono int16, 100 ms blocks -> queue.

sounddevice is imported lazily inside start() so this module imports fine on
the Linux sandbox (no audio hardware, package not installed).
"""
import queue

from echoecho_app import diagnostics

RATE = 16000
BLOCK_FRAMES = 1600  # 100 ms at 16 kHz -> 3200-byte int16 chunks


class WakeMic:
    def __init__(self, rate=RATE, block_frames=BLOCK_FRAMES, device=None):
        self.rate = rate
        self.block_frames = block_frames
        self.device = device  # SPEC (index / name substring / "" = default),
        self.chunks = queue.Queue()  # resolved fresh at every start()/reopen()
        self._stream = None
        # close() failure means the native stream may still exist after its
        # Python reference is cleared.  This remains sticky for the lifetime
        # of the WakeMic so session recovery can never reinitialize PortAudio.
        self._stream_close_error = None
        self._callbacks = 0
        self._captured_bytes = 0
        self._status_callbacks = 0
        self._queue_high_water = 0
        self._read_timeouts = 0
        self._drained_chunks = 0
        self._starts = 0

    def _callback(self, indata, frames, time_info, status):
        self.chunks.put(bytes(indata))
        # PortAudio callback: counters only. Never write diagnostics here.
        try:
            self._callbacks += 1
            self._captured_bytes += len(indata)
            self._status_callbacks += int(bool(status))
            self._queue_high_water = max(
                self._queue_high_water, self.chunks.qsize())
        except Exception:
            pass

    def start(self):
        import sounddevice as sd  # lazy: Mac-only dependency
        from echoecho_app.conversation.audio import device_label, resolve_device
        if not self.portaudio_close_safe:
            raise RuntimeError(
                "cannot start wake mic after a native stream failed to close")
        dev = resolve_device(self.device, "input")  # at OPEN time, on purpose
        label = device_label(dev, "input")
        stream = None
        try:
            stream = sd.RawInputStream(
                samplerate=self.rate, channels=1, dtype="int16",
                blocksize=self.block_frames, device=dev,
                callback=self._callback)
            self._stream = stream
            stream.start()
        except Exception as exc:
            if stream is not None:
                try:
                    stream.stop()
                except Exception:
                    pass
                try:
                    stream.close()
                except Exception as close_exc:
                    if self._stream_close_error is None:
                        self._stream_close_error = close_exc
            self._stream = None
            diagnostics.exception(
                "wake.mic.start_failed", exc=exc, rate=self.rate,
                block_frames=self.block_frames)
            raise
        self._starts += 1
        diagnostics.info(
            "wake.mic.started", rate=self.rate,
            block_frames=self.block_frames, device_index=dev,
            system_default=dev is None,
            configured_by_name=(isinstance(self.device, str)
                                and bool(self.device.strip())
                                and not self.device.strip().isdigit()),
            start_count=self._starts)
        print("[wake] mic: %s" % label)
        return self

    def reopen(self):
        """Fully close, drop stale chunks, then start() again so the device
        spec re-resolves against the CURRENT device list — this (plus
        refresh_devices() between sessions) is what makes AirPods connected
        while echoecho was busy get picked up with zero user action."""
        self.stop()
        self.drain()
        return self.start()

    def read(self, timeout=0.5):
        """Next 100 ms chunk of int16 bytes, or None on timeout."""
        try:
            return self.chunks.get(timeout=timeout)
        except queue.Empty:
            self._read_timeouts += 1
            return None

    def drain(self):
        """Throw away buffered chunks (used when un-pausing the wake feed)."""
        drained = 0
        while True:
            try:
                self.chunks.get_nowait()
                drained += 1
            except queue.Empty:
                self._drained_chunks += drained
                if drained:
                    diagnostics.info("wake.mic.drained", chunk_count=drained)
                return drained

    def telemetry(self):
        """Memory-only snapshot safe to read from the asyncio thread."""
        return {
            "callback_count": self._callbacks,
            "captured_bytes": self._captured_bytes,
            "status_callbacks": self._status_callbacks,
            "queue_depth": self.chunks.qsize(),
            "queue_high_water": self._queue_high_water,
            "read_timeouts": self._read_timeouts,
            "drained_chunks": self._drained_chunks,
            "start_count": self._starts,
            "stream_active": self._stream is not None,
            "portaudio_close_safe": self.portaudio_close_safe,
        }

    @property
    def portaudio_close_safe(self):
        """Whether every native wake-stream close completed successfully."""
        return self._stream_close_error is None

    def stop(self):
        if self._stream is None:
            if self._stream_close_error is not None:
                raise self._stream_close_error
            return
        stream = self._stream
        first_error = self._stream_close_error
        try:
            stream.stop()
        except Exception as exc:
            first_error = first_error or exc
        try:
            stream.close()
        except Exception as exc:
            if self._stream_close_error is None:
                self._stream_close_error = exc
            first_error = first_error or exc
        finally:
            self._stream = None
            diagnostics.info(
                "wake.mic.stopped", callback_count=self._callbacks,
                captured_bytes=self._captured_bytes,
                status_callbacks=self._status_callbacks,
                queue_high_water=self._queue_high_water,
                read_timeouts=self._read_timeouts,
                drained_chunks=self._drained_chunks,
                start_count=self._starts,
                portaudio_close_safe=self.portaudio_close_safe)
        if first_error is not None:
            diagnostics.exception("wake.mic.stop_failed", exc=first_error)
            raise first_error
