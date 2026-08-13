"""Per-session recordings: what echoecho heard, what it said, what it did.

The development feedback loop (PR 9): every real use of the product leaves a
reviewable artifact under recordings/<stamp>_<mode>/ so we can listen to the
audio, compare what echoecho actually did against what was wanted, and turn that
into fixes. Each session directory holds:

    mic.wav        what the mic heard (24 kHz pcm16 mono)
    echoecho.wav       what actually reached the speaker, chimes and zero-fill
                   included, so the timeline matches wall-clock
    session.wav    stereo review mix — left = you, right = echoecho — built at
                   close; the one file to open when reviewing a session
    events.jsonl   every events.emit() during the session (wake, transcripts,
                   tool calls, task lifecycle, injections, state moves),
                   flushed per line so it survives a crash
    transcript.md  human-readable review sheet rendered from the events
    meta.json      end reason, durations, devices, model, per-type counts

Audio bytes arrive on PortAudio callback threads; they are queued and written
by one background thread so the realtime callbacks never touch the disk.
Recording is observability, never load-bearing: start() and every recorder
method swallow their own failures — a full disk must not kill the daemon.
"""
import json
import queue
import threading
import time
import wave
from array import array
from pathlib import Path

from echoecho_app import config, events

RATE = 24000  # matches conversation.audio's pcm16 mono streams
MIX_CHUNK_FRAMES = 24000  # mix session.wav 1s at a time: bounded memory

_active = None


def active():
    """The live SessionRecorder, or None. Audio callbacks poll this."""
    return _active


def start(mode, root=None):
    """Open a new session recording and point the events tee at it.
    Returns the recorder, or None (with a console note) if opening failed."""
    global _active
    try:
        stop()  # never leak a previous recorder
        rec = SessionRecorder(mode, root=root)
        _active = rec
        events.TEE = rec.on_event
        print("[record] session -> %s" % rec.dir)
        return rec
    except Exception as exc:
        print("[record] disabled for this session (%s)" % exc)
        return None


def stop(end_reason=None):
    """Detach and finalize the active recording (no-op when none). Never
    raises: this runs in voice_main's session finally, where an escaping
    exception would kill the always-on daemon."""
    global _active
    rec, _active = _active, None
    events.TEE = None
    if rec is not None:
        try:
            rec.close(end_reason=end_reason)
        except Exception as exc:
            print("[record] finalize failed (%s) — raw files kept in %s"
                  % (exc, rec.dir))
    return rec


class SessionRecorder:
    def __init__(self, mode, root=None, rate=RATE, clock=time.time):
        self.mode = mode
        self.rate = rate
        self.clock = clock
        self.started = clock()
        self.closed = False
        self.errors = 0
        self.stalled = False  # writer thread failed to exit at close
        self.events = []  # teed event dicts: transcript + meta at close
        self.stream_status = {}  # PortAudio status flags seen, by direction
        self._echoecho_pad_frames = 0  # device-latency shim, see set_echoecho_delay
        root = config.recordings_dir() if root is None else Path(root)
        stamp = time.strftime("%Y-%m-%d_%H%M%S", time.localtime(self.started))
        base = "%s_%s" % (stamp, mode)
        self.dir = root / base
        for n in range(2, 100):  # two wakes in one second: suffix, never clobber
            if not self.dir.exists():
                break
            self.dir = root / ("%s-%d" % (base, n))
        self.dir.mkdir(parents=True)
        self._events_fh = open(str(self.dir / "events.jsonl"), "a",
                               encoding="utf-8")
        self._q = queue.Queue()
        self._wavs = {}  # track name -> wave writer (lazy: text mode has none)
        self._frames = {"mic": 0, "echoecho": 0}
        self._thread = threading.Thread(target=self._drain, daemon=True)
        self._thread.start()

    # -- events tee (events.TEE while active) --------------------------------

    def on_event(self, rec, line):
        """Mirror one already-serialized feed line into the session log."""
        if self.closed:
            return
        try:
            self.events.append(rec)
            self._events_fh.write(line + "\n")
            self._events_fh.flush()  # crash-tolerant: the log always survives
        except Exception:
            self.errors += 1

    # -- audio taps (PortAudio callback threads) ------------------------------

    def write_mic(self, pcm_bytes):
        """One capture block: what echoecho heard."""
        if not self.closed:
            self._q.put(("mic", pcm_bytes))

    def write_echoecho(self, pcm_bytes):
        """One playback block: what actually reached the speaker."""
        if not self.closed:
            self._q.put(("echoecho", pcm_bytes))

    def set_echoecho_delay(self, seconds):
        """Device-latency alignment for session.wav: audio handed to the
        output callback reaches the ear ~in+out stream latency later than the
        same wall-clock moment on the mic track, so the echo track opens with
        that much silence. AudioIO.start() calls this (streams built, not yet
        started — so before the first write_echoecho). Approximate by nature;
        capped at 2 s. Never raises."""
        try:
            self._echoecho_pad_frames = max(0, min(int(float(seconds) * self.rate),
                                               2 * self.rate))
        except Exception:
            self.errors += 1

    def note_status(self, direction, status):
        """Count a PortAudio callback status flag (overflow/underflow). An
        input overflow drops capture audio and silently desyncs the tracks —
        the counts land in meta.json so a reviewer knows alignment drifted.
        In-memory only: callback threads must never touch the disk."""
        try:
            self.stream_status[direction] = self.stream_status.get(direction, 0) + 1
        except Exception:
            self.errors += 1

    def _drain(self):
        while True:
            item = self._q.get()
            if item is None:
                return
            track, data = item
            try:
                w = self._wavs.get(track)
                if w is None:
                    w = wave.open(str(self.dir / (track + ".wav")), "wb")
                    w.setnchannels(1)
                    w.setsampwidth(2)
                    w.setframerate(self.rate)
                    self._wavs[track] = w
                    if track == "echoecho" and self._echoecho_pad_frames:
                        w.writeframes(b"\x00\x00" * self._echoecho_pad_frames)
                        self._frames[track] += self._echoecho_pad_frames
                w.writeframes(data)
                self._frames[track] += len(data) // 2
            except Exception:
                self.errors += 1

    # -- close: finalize wavs, then render the review artifacts ---------------

    def close(self, end_reason=None):
        if self.closed:
            return
        self.closed = True
        try:
            self._q.put(None)
            self._thread.join(timeout=10.0)
            self.stalled = self._thread.is_alive()
        except Exception:
            self.errors += 1
        if self.stalled:
            # writer stuck on a hung disk: touching its wave handles or
            # reading half-written wavs would race it. Keep the raw files,
            # skip finalize; transcript+meta below are main-thread-only.
            self.errors += 1
            print("[record] writer thread stalled — wav files left unfinalized"
                  " in %s" % self.dir)
        else:
            for w in list(self._wavs.values()):
                try:
                    w.close()
                except Exception:
                    self.errors += 1
        steps = (self._write_transcript, lambda: self._write_meta(end_reason)) \
            if self.stalled else (self._write_mix, self._write_transcript,
                                  lambda: self._write_meta(end_reason))
        for step in steps:
            try:
                step()
            except Exception:
                self.errors += 1
        try:
            self._events_fh.close()
        except Exception:
            pass
        secs = max(self._frames["mic"], self._frames["echoecho"]) / float(self.rate)
        audio = ", %.0fs audio" % secs if secs else ""
        print("[record] saved %s (%d events%s)"
              % (self.dir, len(self.events), audio))

    def _write_mix(self):
        """session.wav: stereo review mix, left = mic (you), right = echoecho.
        Both tracks are continuous from stream open to close (the output
        callback zero-fills), so pairing frames keeps them time-aligned."""
        readers = {}
        for track in ("mic", "echoecho"):
            path = self.dir / (track + ".wav")
            readers[track] = wave.open(str(path), "rb") if path.is_file() else None
        try:
            total = max(r.getnframes() if r else 0 for r in readers.values())
            if not total:
                return
            out = wave.open(str(self.dir / "session.wav"), "wb")
            try:
                out.setnchannels(2)
                out.setsampwidth(2)
                out.setframerate(self.rate)
                done = 0
                while done < total:
                    n = min(MIX_CHUNK_FRAMES, total - done)
                    mix = array("h", b"\x00\x00" * (2 * n))
                    for lane, track in enumerate(("mic", "echoecho")):
                        mix[lane::2] = array("h", _read_pad(readers[track], n))
                    out.writeframes(mix.tobytes())
                    done += n
            finally:
                out.close()
        finally:
            for r in readers.values():
                if r is not None:
                    r.close()

    def _write_transcript(self):
        (self.dir / "transcript.md").write_text(
            render_transcript(self.events, self.started), encoding="utf-8")

    def _write_meta(self, end_reason):
        ended = self.clock()
        counts = {}
        for ev in self.events:
            t = ev.get("type", "?")
            counts[t] = counts.get(t, 0) + 1
        meta = {
            "id": self.dir.name,
            "mode": self.mode,
            "started": _iso(self.started),
            "ended": _iso(ended),
            "duration_s": round(ended - self.started, 1),
            "end_reason": end_reason or self._last_idle_reason(),
            "wake_via": self._first("wake", "via"),
            "model": self._connected_model(),
            "devices": self._devices(),
            "mic_s": round(self._frames["mic"] / float(self.rate), 1),
            "echoecho_s": round(self._frames["echoecho"] / float(self.rate), 1),
            "user_turns": counts.get("user_text", 0),
            "assistant_turns": counts.get("assistant_text", 0),
            "tool_calls": [ev.get("name") for ev in self.events
                           if ev.get("type") == "tool_call"],
            "event_counts": counts,
            "write_errors": self.errors,
        }
        if self._echoecho_pad_frames:
            meta["echoecho_delay_s"] = round(self._echoecho_pad_frames / float(self.rate), 3)
        if self.stream_status:  # overflows/underflows: track alignment suspect
            meta["stream_status"] = dict(self.stream_status)
        if self.stalled:
            meta["writer_stalled"] = True
        (self.dir / "meta.json").write_text(
            json.dumps(meta, indent=2, default=str) + "\n", encoding="utf-8")

    # -- meta helpers ----------------------------------------------------------

    def _first(self, etype, field):
        for ev in self.events:
            if ev.get("type") == etype:
                return ev.get(field)
        return None

    def _connected_model(self):
        for ev in self.events:
            if ev.get("type") == "session" and ev.get("event") == "connected":
                return ev.get("model")
        return None

    def _devices(self):
        for ev in self.events:
            if ev.get("type") == "audio":
                return {"input": ev.get("input"), "output": ev.get("output")}
        return None

    def _last_idle_reason(self):
        reason = None
        for ev in self.events:
            if ev.get("type") == "state" and ev.get("to") == "IDLE":
                reason = ev.get("reason")
        return reason


def _read_pad(reader, nframes):
    """nframes of mono pcm16 from a wave reader, zero-padded past EOF; a
    missing track is all silence."""
    want = nframes * 2
    data = reader.readframes(nframes) if reader is not None else b""
    if len(data) < want:
        data += b"\x00" * (want - len(data))
    return data


def _iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts))


def _offset(ts, started):
    secs = max(0, int(ts - started))
    if secs >= 3600:
        return "%d:%02d:%02d" % (secs // 3600, secs % 3600 // 60, secs % 60)
    return "%02d:%02d" % (secs // 60, secs % 60)


def render_transcript(evs, started):
    """Human-readable review sheet: the conversation interleaved with what
    echoecho actually did (tools, tasks, injections, state moves). Timestamps are
    mm:ss from session start."""
    lines = ["# echoecho session — %s"
             % time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started)),
             ""]
    for ev in evs:
        t = ev.get("type", "?")
        at = _offset(ev.get("ts", started), started)
        if t == "user_text":
            lines.append("**[%s] you:** %s" % (at, ev.get("text", "")))
        elif t == "assistant_text":
            lines.append("**[%s] echoecho:** %s" % (at, ev.get("text", "")))
        elif t == "tool_call":
            lines.append("[%s] ⚙ %s %s"
                         % (at, ev.get("name", "?"),
                            json.dumps(ev.get("args", {}), default=str)))
        elif t == "task":
            say = ev.get("say")
            lines.append("[%s] ⏳ task %s %s: %s%s"
                         % (at, ev.get("task_id", "?"), ev.get("kind", ""),
                            ev.get("status", ""),
                            " — %s" % say if say else ""))
        elif t == "injection":
            lines.append("[%s] ↪ injected (%s): %s"
                         % (at, ev.get("priority", ""), ev.get("text", "")))
        elif t == "state":
            lines.append("[%s] · state %s → %s (%s)"
                         % (at, ev.get("frm", "?"), ev.get("to", "?"),
                            ev.get("reason", "")))
        elif t == "wake":
            lines.append("[%s] ⏰ wake (%s)" % (at, ev.get("via", "")))
        elif t == "audio":
            lines.append("[%s] 🎧 mic: %s → speaker: %s"
                         % (at, ev.get("input", "?"), ev.get("output", "?")))
        elif t == "session":
            lines.append("[%s] · session %s %s"
                         % (at, ev.get("event", ""),
                            ev.get("model") or ev.get("detail") or ""))
        else:
            rest = dict((k, v) for k, v in ev.items() if k not in ("ts", "type"))
            lines.append("[%s] · %s %s" % (at, t, json.dumps(rest, default=str)))
        lines.append("")  # one paragraph per entry: readable as markdown
    return "\n".join(lines).rstrip("\n") + "\n"
