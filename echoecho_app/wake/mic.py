"""Mac-only wake-loop mic capture: 16 kHz mono int16, 100 ms blocks -> queue.

sounddevice is imported lazily inside start() so this module imports fine on
the Linux sandbox (no audio hardware, package not installed).
"""
import queue

RATE = 16000
BLOCK_FRAMES = 1600  # 100 ms at 16 kHz -> 3200-byte int16 chunks


class WakeMic:
    def __init__(self, rate=RATE, block_frames=BLOCK_FRAMES, device=None):
        self.rate = rate
        self.block_frames = block_frames
        self.device = device  # SPEC (index / name substring / "" = default),
        self.chunks = queue.Queue()  # resolved fresh at every start()/reopen()
        self._stream = None

    def _callback(self, indata, frames, time_info, status):
        self.chunks.put(bytes(indata))

    def start(self):
        import sounddevice as sd  # lazy: Mac-only dependency
        from echoecho_app.conversation.audio import device_label, resolve_device
        dev = resolve_device(self.device, "input")  # at OPEN time, on purpose
        self._stream = sd.RawInputStream(
            samplerate=self.rate, channels=1, dtype="int16",
            blocksize=self.block_frames, device=dev,
            callback=self._callback)
        self._stream.start()
        print("[wake] mic: %s" % device_label(dev, "input"))
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
            return None

    def drain(self):
        """Throw away buffered chunks (used when un-pausing the wake feed)."""
        while True:
            try:
                self.chunks.get_nowait()
            except queue.Empty:
                return

    def stop(self):
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
