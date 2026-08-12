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

from echo_app import events, recorder

RATE = 24000
BLOCK_FRAMES = 2400  # 100 ms at 24 kHz


# -- device selection (PR 8) --------------------------------------------------
#
# PortAudio freezes its device list at library init, so a daemon that binds
# devices once never sees hardware plugged in later (AirPods!). The fix is
# two-part: resolve_device() runs at STREAM-OPEN time (never at construction),
# and refresh_devices() re-inits PortAudio between sessions — with zero open
# streams — so the list itself is fresh.

def resolve_device(spec, kind):
    """Resolve a device spec into what sounddevice's `device=` argument wants.

    None / "" -> None (PortAudio's default at open time); all digits -> int
    index; anything else -> case-insensitive substring match over the names
    of devices capable of `kind` ('input': max_input_channels>0, 'output':
    max_output_channels>0). Multiple matches -> first; no match -> ValueError
    listing what IS available. sounddevice is imported lazily and only when a
    name actually needs matching, so this module (and the default/index
    paths) work on the Linux sandbox.
    """
    if spec is None:
        return None
    if isinstance(spec, int):
        return spec
    spec = str(spec).strip()
    if not spec:
        return None
    if spec.isdigit():
        return int(spec)
    import sounddevice as sd  # lazy: Mac-only dependency
    key = "max_input_channels" if kind == "input" else "max_output_channels"
    candidates = [(i, dev.get("name", ""))
                  for i, dev in enumerate(sd.query_devices())
                  if dev.get(key, 0) > 0]
    needle = spec.lower()
    for i, name in candidates:
        if needle in name.lower():
            return i
    raise ValueError(
        "no %s device matching %r; available %s devices: %s"
        % (kind, spec, kind,
           ", ".join(repr(n) for _, n in candidates) or "(none)"))


def device_label(index, kind):
    """Human-readable name for a resolved device index. None means 'system
    default' — we name the ACTUAL current default when discoverable. Never
    raises (labels are observability, not load-bearing)."""
    try:
        import sounddevice as sd  # lazy: Mac-only dependency
        if index is None:
            info = sd.query_devices(kind=kind)
            name = info.get("name") if isinstance(info, dict) else None
            return "%s (system default)" % name if name else "system default"
        info = sd.query_devices(index)
        name = info.get("name") if isinstance(info, dict) else None
        return name or "device %s" % index
    except Exception:
        return "system default" if index is None else "device %s" % index


def refresh_devices():
    """Re-initialize PortAudio so its init-time-frozen device list picks up
    hot-plugged hardware. sounddevice._terminate()/_initialize() is private
    API but the standard trick; guarded so a future sounddevice that drops it
    degrades to 'no refresh', never a crash. CRITICAL: only call this with
    ZERO open streams (echo.py's session-boundary helpers own the ordering).
    Returns True iff a refresh actually happened."""
    try:
        import sounddevice as sd  # lazy: Mac-only dependency
    except ImportError:
        return False
    try:
        sd._terminate()
        sd._initialize()
        return True
    except AttributeError:  # private API removed in a future version
        return False
    except Exception as exc:  # never let a refresh kill the daemon
        print("[audio] device refresh failed (%s)" % exc)
        return False


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
                 block_frames=BLOCK_FRAMES, device=None,
                 input_device=None, output_device=None):
        self.tracker = tracker  # PlaybackTracker (advance() as audio plays)
        self.loop = loop        # asyncio loop for thread-safe send scheduling
        self.rate = rate
        self.block_frames = block_frames
        self.device = device    # legacy alias for input_device
        # device SPECS (index / name substring / "" = system default), kept
        # unresolved until start(): resolution must see the current devices
        self.input_device = device if input_device is None else input_device
        self.output_device = output_device
        self.send_event = None  # async cb(dict) -> transport
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._in_stream = None
        self._out_stream = None
        self.muted_capture = False

    # -- capture: mic -> input_audio_buffer.append --------------------------

    def _in_callback(self, indata, frames, time_info, status):
        # Bind locally: stop() nulls these from another thread mid-callback.
        send, loop = self.send_event, self.loop
        rec = recorder.active()
        sending = not (self.muted_capture or send is None or loop is None)
        if rec is None and not sending:
            return
        data = bytes(indata)
        if rec is not None:
            rec.write_mic(data)  # what Echo heard, muted teardown tail included
        if not sending:
            return
        event = {"type": "input_audio_buffer.append",
                 "audio": base64.b64encode(data).decode("ascii")}

        # sounddevice callbacks run on a PortAudio thread; hop to the loop.
        # Retrieve task exceptions: a send racing transport close is expected
        # during teardown and must not spam "exception was never retrieved".
        def _send():
            loop.create_task(send(event)).add_done_callback(
                lambda t: t.exception())
        loop.call_soon_threadsafe(_send)

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
        played = len(chunk)
        if played < need:
            chunk += b"\x00" * (need - played)
        outdata[:need] = chunk
        if self.tracker is not None and played:
            self.tracker.advance(played / (self.rate * 2.0) * 1000.0)
        rec = recorder.active()
        if rec is not None:
            rec.write_echo(chunk)  # zero-fill included: timeline stays real

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
        # resolve specs NOW, against the current device list (voice_main
        # refreshes PortAudio just before this, so hot-plugged devices show)
        in_dev = resolve_device(self.input_device, "input")
        out_dev = resolve_device(self.output_device, "output")
        self._in_stream = sd.RawInputStream(
            samplerate=self.rate, channels=1, dtype="int16",
            blocksize=self.block_frames, device=in_dev,
            callback=self._in_callback)
        self._out_stream = sd.RawOutputStream(
            samplerate=self.rate, channels=1, dtype="int16",
            blocksize=self.block_frames, device=out_dev,
            callback=self._out_callback)
        self._in_stream.start()
        self._out_stream.start()
        in_name = device_label(in_dev, "input")
        out_name = device_label(out_dev, "output")
        events.emit("audio", input=in_name, output=out_name)
        print("[audio] mic: %s -> speaker: %s" % (in_name, out_name))
        return self

    def stop(self):
        for stream in (self._in_stream, self._out_stream):
            if stream is not None:
                stream.stop()
                stream.close()
        self._in_stream = self._out_stream = None
        self.send_event = None
