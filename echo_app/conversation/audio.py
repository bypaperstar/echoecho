"""Mac-only Realtime audio I/O: 24 kHz pcm16 capture/playback + chimes.

Device-open paths live behind lazy `import sounddevice` so this module
imports cleanly on the Linux sandbox; the pure parts (chime synthesis, the
playback buffer math) are unit-tested there. Capture: 100 ms int16 blocks ->
base64 -> {"type": "input_audio_buffer.append", "audio": ...} scheduled onto
the asyncio loop. Playback: response.output_audio.delta chunks feed a byte
buffer drained by a RawOutputStream callback which advances the
PlaybackTracker's played-ms cursor (the barge-in truncate bookkeeping);
flush() empties the buffer on barge-in.
"""
import base64
import math
import struct
import threading

RATE = 24000
BLOCK_FRAMES = 2400  # 100 ms at 24 kHz


def make_chime(freqs, per_note_ms=90, rate=RATE, volume=0.35):
    """Pure-stdlib sine-wave chime: pcm16 mono bytes, one note per freq,
    with a linear fade per note so there are no clicks. No asset files."""
    frames = []
    n = int(rate * per_note_ms / 1000.0)
    for freq in freqs:
        for i in range(n):
            env = min(1.0, (n - i) / (n * 0.5)) * min(1.0, i / (n * 0.1 + 1))
            val = volume * env * math.sin(2 * math.pi * freq * i / rate)
            frames.append(int(val * 32767))
    return struct.pack("<%dh" % len(frames), *frames)


def wake_chime():
    return make_chime((660, 880))  # rising: session opening


def end_chime():
    return make_chime((880, 660))  # falling: session closed


class AudioIO:
    def __init__(self, tracker=None, loop=None, rate=RATE,
                 block_frames=BLOCK_FRAMES, device=None):
        self.tracker = tracker  # PlaybackTracker (advance() as audio plays)
        self.loop = loop        # asyncio loop for thread-safe send scheduling
        self.rate = rate
        self.block_frames = block_frames
        self.device = device
        self.send_event = None  # async cb(dict) -> transport
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._in_stream = None
        self._out_stream = None
        self.muted_capture = False

    # -- capture: mic -> input_audio_buffer.append --------------------------

    def _in_callback(self, indata, frames, time_info, status):
        if self.muted_capture or self.send_event is None or self.loop is None:
            return
        event = {"type": "input_audio_buffer.append",
                 "audio": base64.b64encode(bytes(indata)).decode("ascii")}
        # sounddevice callbacks run on a PortAudio thread; hop to the loop
        self.loop.call_soon_threadsafe(
            lambda: self.loop.create_task(self.send_event(event)))

    # -- playback: output_audio.delta -> speaker ----------------------------

    def on_audio(self, item_id, b64_delta):
        """RealtimeClient's on_audio hook (it already books tracker.append)."""
        self.feed_pcm(base64.b64decode(b64_delta))

    def feed_pcm(self, pcm_bytes):
        with self._lock:
            self._buf.extend(pcm_bytes)

    def flush(self):
        """Barge-in: drop everything queued for the speaker."""
        with self._lock:
            del self._buf[:]

    def _out_callback(self, outdata, frames, time_info, status):
        need = frames * 2  # mono int16
        with self._lock:
            chunk = bytes(self._buf[:need])
            del self._buf[:need]
        outdata[:len(chunk)] = chunk
        if len(chunk) < need:
            outdata[len(chunk):need] = b"\x00" * (need - len(chunk))
        if self.tracker is not None and chunk:
            self.tracker.advance(len(chunk) / (self.rate * 2.0) * 1000.0)

    def play_chime(self, kind):
        self.feed_pcm(wake_chime() if kind == "wake" else end_chime())

    def pending_ms(self):
        with self._lock:
            return len(self._buf) / (self.rate * 2.0) * 1000.0

    # -- device open/close (Mac-only) ----------------------------------------

    def start(self, loop, send_event):
        import sounddevice as sd  # lazy: Mac-only dependency
        self.loop = loop
        self.send_event = send_event
        self._in_stream = sd.RawInputStream(
            samplerate=self.rate, channels=1, dtype="int16",
            blocksize=self.block_frames, device=self.device,
            callback=self._in_callback)
        self._out_stream = sd.RawOutputStream(
            samplerate=self.rate, channels=1, dtype="int16",
            blocksize=self.block_frames, callback=self._out_callback)
        self._in_stream.start()
        self._out_stream.start()
        return self

    def stop(self):
        for stream in (self._in_stream, self._out_stream):
            if stream is not None:
                stream.stop()
                stream.close()
        self._in_stream = self._out_stream = None
        self.send_event = None
