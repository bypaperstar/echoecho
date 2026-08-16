#!/usr/bin/env python3
"""Silent voice E2E playtests: the REAL Mac voice path, no speakers involved.

scripts/playtest.py drives the real voice model with text turns, so it can
never catch what only the audio path breaks: wake-word spotting on real
capture, chimes, playback, barge-in, AEC, the recorder taps, device
selection. This harness closes that gap ON A MAC without ever playing a
sound out loud: everything is piped through a virtual loopback audio device
(BlackHole 2ch — https://github.com/ExistentialAudio/BlackHole).

How the silent pipe works
    say(1) synthesizes each user turn to a WAV; the harness plays it into
    BlackHole's OUTPUT side; echoecho runs with ECHOECHO_INPUT_DEVICE and
    ECHOECHO_OUTPUT_DEVICE both pinned to BlackHole, so its "mic" hears the
    synthesized speech and its own voice goes into the same loop (silently) —
    which also exercises the WebRTC AEC exactly like laptop-speaker use, since
    echoecho's speech re-enters its mic and must be cancelled before server
    VAD sees it. A monitor stream records BlackHole the whole run; voiced
    spans OUTSIDE the harness's own playback windows are echoecho speaking —
    the device-level proof you would have *heard* it respond.

Each scenario in fixtures/voiceplaytests/*.json boots a REAL
`echoecho.py --voice` daemon (Vosk wake word -> OpenAI Realtime session ->
orchestrator + real workers -> viewer + recorder), speaks the turns, and
asserts on every observable surface:
    events        workspace/.events.jsonl (wake/user_text/assistant_text/
                  tool_call/task/injection/session)
    workspace     files the workers wrote (contains/bullet checks)
    recording     recordings/<session>/meta.json + echoecho.wav voiced time
    http          the live viewer (/, /transcript, /doc, /vnc-info w/ token)
    device audio  the monitor's voiced-outside-playback seconds

Run it from a checkout that is NOT the one your live daemon uses (a git
worktree is perfect) — scenarios wipe workspace/ and bind their own viewer
port. The harness never touches other echoecho processes: it only signals the
PID it spawned.

Usage (on the Mac, from the repo root, venv active or via .venv/bin/python):
  python3 scripts/voice_playtest.py --preflight     # environment sanity table
  python3 scripts/voice_playtest.py                 # all scenarios but slow
  python3 scripts/voice_playtest.py --include-slow  # + the VM scenario
  python3 scripts/voice_playtest.py --only wake_and_roundtrip
Results land in voice-playtest-results/<stamp>/<scenario>/ with a top-level
report.md (same shape as scripts/playtest.py).
"""
import argparse
import array
import json
import math
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import wave
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

SCENARIOS_DIR = REPO_ROOT / "fixtures" / "voiceplaytests"
RESULTS_ROOT = REPO_ROOT / "voice-playtest-results"
EVENTS_FEED = REPO_ROOT / "workspace" / ".events.jsonl"

DEFAULT_DEVICE = "BlackHole 2ch"
DEFAULT_VIEWER_PORT = 8791
INJECT_RATE = 24000          # synth + playback rate (matches the session)
VOICED_RMS = 400             # int16 RMS above this in a 50 ms window = voiced
CHIME_ALLOWANCE_S = 1.0      # wake+end chimes account for ~0.4 s voiced
RESPONSE_TIMEOUT = 90        # max wait for one assistant_text after a turn
WAKE_TIMEOUT = 8             # per wake-phrase attempt
WAKE_RETRIES = 3


# ---------------------------------------------------------------- tiny audio utils

def read_wav_mono(path):
    """(rate, int16 array) — stereo collapsed to LEFT channel."""
    with wave.open(str(path), "rb") as w:
        rate, ch = w.getframerate(), w.getnchannels()
        samples = array.array("h", w.readframes(w.getnframes()))
    if ch > 1:
        samples = samples[::ch]
    return rate, samples


def wav_channel(path, channel):
    """(rate, int16 array) for one channel of a WAV (0 = left, 1 = right)."""
    with wave.open(str(path), "rb") as w:
        rate, ch = w.getframerate(), w.getnchannels()
        samples = array.array("h", w.readframes(w.getnframes()))
    return rate, samples[channel::ch] if ch > 1 else samples


def voiced_seconds(samples, rate, window_s=0.05, rms=VOICED_RMS):
    """Seconds of windows whose RMS clears the voiced threshold."""
    n = max(1, int(rate * window_s))
    voiced = 0
    for i in range(0, len(samples) - n, n):
        acc = 0
        for s in samples[i:i + n]:
            acc += s * s
        if math.sqrt(acc / n) >= rms:
            voiced += 1
    return voiced * window_s


def normalize_pcm(samples, peak=0.7):
    """Scale int16 samples so their peak sits at `peak` of full scale —
    say(1) output is quiet and VAD/wake spotting like a confident level."""
    top = max(1, max(abs(s) for s in samples))
    scale = min(4.0, peak * 32767.0 / top)  # cap gain: don't blow up noise
    return array.array("h", (int(s * scale) for s in samples))


def resample_pcm(samples, src_rate, dst_rate):
    """Nearest-sample resample (mono int16). Good enough for speech synth."""
    if src_rate == dst_rate:
        return samples
    n = int(len(samples) * dst_rate / src_rate)
    step = src_rate / dst_rate
    return array.array("h", (samples[min(len(samples) - 1, int(i * step))]
                             for i in range(n)))


def synth(text, cache_dir, voice=None):
    """say(1) -> normalized 24 kHz mono int16 WAV; cached by content."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = re.sub(r"[^a-z0-9]+", "-", text.lower())[:60] or "blank"
    path = cache_dir / ("%s.wav" % key)
    if not path.exists():
        raw = cache_dir / ("%s-raw.wav" % key)
        cmd = ["say", "-o", str(raw),
               "--data-format=LEI16@%d" % INJECT_RATE, "--file-format=WAVE"]
        if voice:
            cmd += ["-v", voice]
        subprocess.run(cmd + [text], check=True, capture_output=True)
        rate, samples = read_wav_mono(raw)
        samples = normalize_pcm(resample_pcm(samples, rate, INJECT_RATE))
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(INJECT_RATE)
            w.writeframes(samples.tobytes())
        raw.unlink()
    return path


# ---------------------------------------------------------------- device I/O

def resolve_output(device):
    from echoecho_app.conversation.audio import resolve_device
    return resolve_device(device, "output")


def resolve_input(device):
    from echoecho_app.conversation.audio import resolve_device
    return resolve_device(device, "input")


class Injector:
    """Plays WAVs into the loopback device — the harness's 'mouth'.
    Every playback window is logged so the monitor can subtract it.

    Playback runs in a SUBPROCESS (this script's hidden --player mode): a
    blocking output stream opened next to the monitor's callback input
    stream in one process makes CoreAudio's AUHAL fail silently
    (kAudioUnitErr_CannotDoInCurrentContext, RMS 0 on the loop) — separate
    processes, like a callback-in/callback-out pair, work every time."""

    def __init__(self, device):
        self.device = device
        self.windows = []  # [(t0, t1)] wall-clock spans we were speaking

    def play(self, wav_path):
        t0 = time.time()
        subprocess.run([sys.executable, str(Path(__file__).resolve()),
                        "--player", str(wav_path), "--device", self.device],
                       check=True, capture_output=True)
        self.windows.append((t0, time.time() + 0.2))  # +device drain slop


def player_main(wav_path, device, lead_s=0.35, tail_s=0.7):
    """--player: block until the WAV (plus VAD-friendly lead/tail silence)
    has been written to the loopback device, then exit."""
    import sounddevice as sd
    rate, samples = read_wav_mono(wav_path)
    pcm = (b"\x00" * int(rate * lead_s) * 2 + samples.tobytes()
           + b"\x00" * int(rate * tail_s) * 2)
    stream = sd.RawOutputStream(samplerate=rate, channels=1, dtype="int16",
                                device=resolve_output(device))
    with stream:
        block = 2400 * 2
        for i in range(0, len(pcm), block):
            stream.write(pcm[i:i + block])


class LoopMonitor:
    """Records the loopback device for a whole scenario — the harness's
    'ear'. Voiced spans outside the injector's playback windows are audio
    echoecho itself produced (chimes + speech): device-level proof it spoke."""

    def __init__(self, device):
        self.device = device
        self.spans = []          # [(t0, t1)] voiced wall-clock spans
        self.last_voiced_at = 0.0
        self._cur = None
        self._stream = None

    def _cb(self, indata, frames, time_info, status):
        buf = array.array("h")
        buf.frombytes(bytes(indata))
        acc = 0
        for s in buf:
            acc += s * s
        now = time.time()
        if math.sqrt(acc / max(1, len(buf))) >= VOICED_RMS:
            self._cur = (self._cur or (now, now))[0], now
            self.last_voiced_at = now
        elif self._cur and now - self._cur[1] > 0.25:  # 250 ms hangover
            self.spans.append(self._cur)
            self._cur = None

    def start(self):
        import sounddevice as sd
        self._stream = sd.RawInputStream(
            samplerate=INJECT_RATE, channels=1, dtype="int16",
            blocksize=int(INJECT_RATE * 0.05), device=resolve_input(self.device),
            callback=self._cb)
        self._stream.start()
        return self

    def stop(self):
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        if self._cur:
            self.spans.append(self._cur)
            self._cur = None

    def voiced_outside(self, windows):
        """Total voiced seconds that do not overlap any injection window."""
        total = 0.0
        for a0, a1 in self.spans:
            span = a1 - a0
            for w0, w1 in windows:
                lo, hi = max(a0, w0 - 0.1), min(a1, w1 + 0.1)
                if hi > lo:
                    span -= hi - lo
            total += max(0.0, span)
        return total

    def wait_quiet(self, min_quiet_s, timeout):
        """Block until the loop has been silent for min_quiet_s — echoecho's
        buffered reply has finished playing, so the next synthesized turn
        won't accidentally barge in over it."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if time.time() - self.last_voiced_at >= min_quiet_s:
                return True
            time.sleep(0.1)
        return False

    def wait_voiced(self, windows, min_run_s, timeout):
        """Block until a voiced span >= min_run_s starts outside the given
        windows (barge-in trigger: echoecho is audibly mid-answer). Live spans
        count too. Returns True on detection."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            spans = list(self.spans) + ([self._cur] if self._cur else [])
            for a0, a1 in spans:
                if a1 - a0 >= min_run_s and not any(
                        w0 - 0.1 <= a0 <= w1 + 0.1 for w0, w1 in windows):
                    return True
            time.sleep(0.1)
        return False


# ---------------------------------------------------------------- event feed

class EventTail:
    """Incremental reader of workspace/.events.jsonl with wait helpers."""

    def __init__(self, path=EVENTS_FEED):
        self.path = Path(path)
        self.events = []
        self._pos = 0

    def poll(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                f.seek(self._pos)
                chunk = f.read()
                self._pos = f.tell()
        except OSError:
            return
        for line in chunk.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if isinstance(ev, dict):
                self.events.append(ev)

    def wait(self, pred, timeout, start=None):
        """First event matching pred at index >= start (default: only events
        newer than 'now'); None on timeout."""
        base = len(self.events) if start is None else start
        deadline = time.time() + timeout
        while True:
            self.poll()
            for ev in self.events[base:]:
                if pred(ev):
                    return ev
            if time.time() >= deadline:
                return None
            time.sleep(0.2)

    def count(self, pred):
        self.poll()
        return sum(1 for ev in self.events if pred(ev))


def ev_match(type_, **where):
    def pred(ev):
        if ev.get("type") != type_:
            return False
        return all(str(ev.get(k, "")) == str(v) for k, v in where.items())
    return pred


# ---------------------------------------------------------------- the daemon

class VoiceDaemon:
    """One real `echoecho.py --voice` under test. Signals ONLY its own PID —
    other echoecho daemons on the machine (the user's!) are never touched."""

    def __init__(self, env, log_path):
        self.env = env
        self.log_path = log_path
        self.proc = None

    def start(self, timeout=45):
        self.log = open(str(self.log_path), "w", encoding="utf-8")
        self.proc = subprocess.Popen(
            [sys.executable, "-u", str(REPO_ROOT / "echoecho.py"), "--voice"],
            cwd=str(REPO_ROOT), env=self.env, stdin=subprocess.DEVNULL,
            stdout=self.log, stderr=subprocess.STDOUT)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError("daemon exited during startup (rc=%s) — see %s"
                                   % (self.proc.returncode, self.log_path))
            try:
                text = self.log_path.read_text(encoding="utf-8")
            except OSError:
                text = ""
            if "[wake] listening" in text:
                return self
            time.sleep(0.3)
        raise RuntimeError("daemon never reached the wake loop — see %s"
                           % self.log_path)

    def stop(self, grace=12):
        if self.proc is None:
            return
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGINT)
            try:
                self.proc.wait(grace)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
                try:
                    self.proc.wait(5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
        self.log.close()
        self.proc = None


# ---------------------------------------------------------------- checks

def check_files(scenario, ws):
    """playtest.py-compatible checks, plus file_glob + min_bullets."""
    out = []
    for check in scenario.get("checks", []):
        if "file_glob" in check:
            globs = check["file_glob"]
            globs = [globs] if isinstance(globs, str) else globs
            paths = sorted({p for g in globs for p in ws.rglob(g)
                            if p.is_file() and not p.name.startswith(".")})
        else:
            p = ws / check["file"]
            paths = [p] if p.is_file() else []
        results = []
        for path in paths:
            try:
                content = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                content = ""
            ok = True
            if "contains_any" in check:
                ok = ok and any(s.lower() in content.lower()
                                for s in check["contains_any"])
            if "not_contains" in check:
                ok = ok and all(s.lower() not in content.lower()
                                for s in check["not_contains"])
            if "min_bullets" in check:
                bullets = len(re.findall(r"^\s*[-*] +\S", content, re.M))
                ok = ok and bullets >= check["min_bullets"]
            results.append(ok)
        rule = {k: v for k, v in check.items() if k not in ("file", "file_glob")}
        label = check.get("file") or check.get("file_glob")
        if check.get("exists_only"):
            ok = bool(paths)
        else:
            ok = any(results) if results else False
        out.append({"file": label, "rule": json.dumps(rule) or "exists",
                    "pass": ok})
    return out


def check_events(scenario, tail):
    out = []
    for check in scenario.get("event_checks", []):
        pred = ev_match(check["type"], **check.get("where", {}))
        n = tail.count(pred)
        lo = check.get("count_min", 1)
        hi = check.get("count_max")
        ok = n >= lo and (hi is None or n <= hi)
        out.append({"file": "events:%s %s" % (check["type"],
                                              check.get("where", {})),
                    "rule": "count in [%s, %s], saw %d" % (lo, hi or "inf", n),
                    "pass": ok})
    return out


def check_http(scenario, port, token):
    out = []
    for check in scenario.get("http_checks", []):
        url = "http://127.0.0.1:%d%s" % (port, check["path"])
        req = urllib.request.Request(url)
        if check.get("auth"):
            req.add_header("Authorization", "Bearer %s" % token)
        status, body = 0, ""
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                status, body = resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            status, body = exc.code, exc.read().decode("utf-8", "replace")
        except Exception as exc:
            body = str(exc)
        ok = status == check.get("expect_status", 200)
        if ok and "expect_contains" in check:
            ok = check["expect_contains"].lower() in body.lower()
        out.append({"file": "http:%s" % check["path"],
                    "rule": "status=%s%s" % (
                        check.get("expect_status", 200),
                        " contains %r" % check["expect_contains"]
                        if "expect_contains" in check else ""),
                    "pass": ok})
    return out


def check_audio(scenario, monitor, injector, recordings_dir):
    out = []
    want = scenario.get("audio_checks", {})
    if "assistant_voiced_s_min" in want:
        got = monitor.voiced_outside(injector.windows)
        need = want["assistant_voiced_s_min"] + CHIME_ALLOWANCE_S
        out.append({"file": "audio:loopback",
                    "rule": "echoecho voiced >= %.1fs (chimes excluded), got %.1fs"
                            % (want["assistant_voiced_s_min"],
                               max(0.0, got - CHIME_ALLOWANCE_S)),
                    "pass": got >= need})
    if want.get("recording_has_assistant_audio"):
        got = 0.0
        for meta_path in sorted(Path(recordings_dir).glob("*/echoecho.wav")):
            rate, samples = read_wav_mono(meta_path)
            got += voiced_seconds(samples, rate)
        out.append({"file": "audio:recording echoecho.wav",
                    "rule": "voiced >= %.1fs incl. chimes, got %.1fs"
                            % (CHIME_ALLOWANCE_S, got),
                    "pass": got >= CHIME_ALLOWANCE_S})
    for key, meta_key in (("meta_user_turns_min", "user_turns"),
                          ("meta_assistant_turns_min", "assistant_turns")):
        if key in want:
            best = -1
            for meta_path in sorted(Path(recordings_dir).glob("*/meta.json")):
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                best = max(best, int(meta.get(meta_key) or 0))
            out.append({"file": "audio:meta.%s" % meta_key,
                        "rule": ">= %d, got %s" % (want[key], best),
                        "pass": best >= want[key]})
    return out


# ---------------------------------------------------------------- one scenario

def wipe_workspace(ws):
    ws.mkdir(exist_ok=True)
    for p in ws.iterdir():
        if p.name == ".gitkeep":
            continue
        shutil.rmtree(p) if p.is_dir() else p.unlink()


def build_env(scenario, args, outdir):
    from echoecho_app import config
    config.load_env_local()
    env = dict(os.environ)
    env.update({
        "ECHOECHO_INPUT_DEVICE": args.device,
        "ECHOECHO_OUTPUT_DEVICE": args.device,
        "ECHOECHO_VIEWER_PORT": str(args.viewer_port),
        "ECHOECHO_VIEWER_TOKEN_FILE": str(outdir / "viewer.token"),
        "ECHOECHO_RECORDINGS_DIR": str(outdir / "recordings"),
        "ECHOECHO_SILENCE_TIMEOUT": "150",
    })
    env.update(scenario.get("env", {}))
    return env


def run_scenario(scenario, args, outdir):
    outdir.mkdir(parents=True, exist_ok=True)
    wipe_workspace(REPO_ROOT / "workspace")
    env = build_env(scenario, args, outdir)
    tail = EventTail()
    injector = Injector(args.device)
    monitor = LoopMonitor(args.device).start()
    cache = RESULTS_ROOT / ".say-cache"
    daemon = VoiceDaemon(env, outdir / "daemon.log")
    started = time.monotonic()
    error, notes = None, []
    timeout_at = time.monotonic() + scenario.get("timeout_s", 600)

    tasks_consumed = [0]

    def say_turn(text, wait_reply=True):
        base = len(tail.events)
        injector.play(synth(text, cache, voice=args.voice))
        if wait_reply:
            got = tail.wait(lambda e: e.get("type") == "assistant_text",
                            RESPONSE_TIMEOUT, start=base)
            if got is None:
                notes.append("no assistant_text within %ss after %r"
                             % (RESPONSE_TIMEOUT, text[:40]))
            # let the buffered reply finish playing before the next turn:
            # speaking over it would be an accidental barge-in
            monitor.wait_quiet(1.3, 45)

    def do_wake():
        for attempt in range(WAKE_RETRIES):
            base = len(tail.events)
            injector.play(synth("echo echo", cache, voice=args.voice))
            if tail.wait(ev_match("session", event="connected"),
                         WAKE_TIMEOUT + (10 if attempt == 0 else 0),
                         start=base):
                time.sleep(1.0)  # let the wake chime clear the loop
                return True
        return False

    try:
        daemon.start()
        for raw in scenario["turns"]:
            if time.monotonic() > timeout_at:
                error = "scenario timeout"
                break
            raw = raw.strip()
            if not raw or raw.startswith("#"):
                continue
            if raw == "~wake":
                if not do_wake():
                    error = "wake word never woke the daemon"
                    break
            elif raw == "~end":
                base = len(tail.events)
                injector.play(synth("that's it, goodbye", cache,
                                    voice=args.voice))
                if not tail.wait(ev_match("session", event="closed"), 45,
                                 start=base):
                    notes.append("session did not close on the end phrase")
                time.sleep(2.5)  # end chime + wake-mic reopen
            elif raw.startswith("~play "):
                injector.play(REPO_ROOT / raw.split(None, 1)[1])
                time.sleep(2.0)
            elif raw.startswith("~wait-task"):
                # each ~wait-task consumes ONE task completion, in order —
                # a completion that already landed satisfies it instantly,
                # but never satisfies two ~wait-task directives
                secs = float(raw.split()[1]) if len(raw.split()) > 1 else 180
                want = tasks_consumed[0] + 1
                deadline = time.time() + secs

                def completions_now():
                    tail.poll()
                    return [x for x in tail.events
                            if x.get("type") == "task"
                            and x.get("status") in ("done", "error")]

                completions = completions_now()
                while len(completions) < want and time.time() < deadline:
                    time.sleep(0.5)
                    completions = completions_now()
                if len(completions) < want:
                    notes.append("no task completion within %ss" % secs)
                else:
                    tasks_consumed[0] = want
                    if completions[want - 1].get("status") == "error":
                        notes.append("task errored: %s"
                                     % json.dumps(completions[want - 1])[:200])
                    time.sleep(2.0)  # let the result injection land
            elif raw.startswith("~wait-voiced"):
                # barge-in helper: block until echoecho is audibly mid-answer
                if not monitor.wait_voiced(injector.windows, 0.8, 30):
                    notes.append("never heard echoecho speaking (barge-in skip)")
            elif raw.startswith("~wait"):
                parts = raw.split()
                time.sleep(float(parts[1]) if len(parts) > 1 else 1.0)
            elif raw.startswith("~say-nowait "):
                say_turn(raw.split(None, 1)[1], wait_reply=False)
            else:
                # a session that already closed (silence timeout, the model
                # hanging up on its own) can't hear this turn — record that
                # and stop instead of timing out on every remaining turn
                tail.poll()
                open_sessions = sum(1 for e in tail.events
                                    if e.get("type") == "session"
                                    and e.get("event") == "connected")
                closed_sessions = sum(1 for e in tail.events
                                      if e.get("type") == "session"
                                      and e.get("event") == "closed")
                if closed_sessions >= open_sessions:
                    notes.append("session closed before %r — remaining "
                                 "turns skipped" % raw[:40])
                    break
                say_turn(raw)
    except Exception as exc:
        error = "%s: %s" % (type(exc).__name__, exc)
    finally:
        # viewer checks need the daemon alive; run before teardown
        token = ""
        try:
            token = (outdir / "viewer.token").read_text(encoding="utf-8").strip()
        except OSError:
            pass
        http_results = check_http(scenario, args.viewer_port, token)
        daemon.stop()
        monitor.stop()

    ws_snap = outdir / "workspace"
    shutil.copytree(REPO_ROOT / "workspace", ws_snap, dirs_exist_ok=True)

    checks = (check_files(scenario, ws_snap) + check_events(scenario, tail)
              + http_results
              + check_audio(scenario, monitor, injector,
                            outdir / "recordings"))
    result = {
        "scenario": scenario["name"],
        "duration_s": round(time.monotonic() - started, 1),
        "error": error,
        "notes": notes,
        "echoecho_voiced_s": round(monitor.voiced_outside(injector.windows), 2),
        "injection_windows": len(injector.windows),
        "checks": checks,
        "checks_passed": sum(1 for c in checks if c["pass"]),
        "checks_total": len(checks),
        "soft": bool(scenario.get("soft")),
    }
    (outdir / "result.json").write_text(json.dumps(result, indent=2),
                                        encoding="utf-8")
    (outdir / "events.jsonl").write_text(
        "\n".join(json.dumps(e) for e in tail.events) + "\n", encoding="utf-8")
    print("[voice-playtest] %s: checks %d/%d%s%s"
          % (scenario["name"], result["checks_passed"], result["checks_total"],
             " ERROR=%s" % error if error else "",
             " notes=%d" % len(notes) if notes else ""))
    return result


# ---------------------------------------------------------------- preflight

def preflight(args):
    rows = []

    def row(name, ok, detail=""):
        rows.append((name, ok, detail))

    row("macOS", sys.platform == "darwin", sys.platform)
    try:
        import sounddevice as sd
        names = [d["name"] for d in sd.query_devices()]
        row("sounddevice", True, "%d devices" % len(names))
        row("loopback device", any(args.device.lower() in n.lower()
                                   for n in names),
            args.device + (" found" if any(args.device.lower() in n.lower()
                                           for n in names) else " MISSING"))
    except Exception as exc:
        row("sounddevice", False, str(exc))
    row("say(1)", shutil.which("say") is not None, shutil.which("say") or "")
    from echoecho_app import config
    config.load_env_local()
    row("OPENAI_API_KEY", bool(os.environ.get("OPENAI_API_KEY")),
        "set" if os.environ.get("OPENAI_API_KEY") else "missing")
    row("vosk model", config.VOSK_MODEL_DIR.is_dir(), str(config.VOSK_MODEL_DIR))
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", args.viewer_port))
        row("viewer port %d" % args.viewer_port, True, "free")
    except OSError:
        row("viewer port %d" % args.viewer_port, False,
            "in use — pick --viewer-port")
    finally:
        sock.close()

    # wake spotting on synthesized speech, fully offline
    if config.VOSK_MODEL_DIR.is_dir() and shutil.which("say"):
        try:
            cache = RESULTS_ROOT / ".say-cache"
            wav = synth("echo echo", cache, voice=args.voice)
            from echoecho_app.wake.detector import WakeDetector
            det = WakeDetector()
            rate, samples = read_wav_mono(wav)
            pcm = resample_pcm(samples, rate, 16000).tobytes()
            hit = any(det.detect(pcm[i:i + 3200])
                      for i in range(0, len(pcm), 3200))
            hit = hit or any(det.detect(b"\x00" * 3200) for _ in range(10))
            row("wake detector vs say(1)", hit, "synth 'echo echo'")
        except Exception as exc:
            row("wake detector vs say(1)", False, str(exc))

    # loopback: play a tone into the device, hear it back — silently. One
    # duplex playrec stream: the arrangement CoreAudio always allows (see
    # the Injector docstring for the arrangement it silently doesn't).
    try:
        import numpy as np  # sounddevice's convenience API needs it
        import sounddevice as sd
        t = np.arange(INJECT_RATE) / float(INJECT_RATE)  # 1 s
        tone = (0.4 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
        dev = (resolve_input(args.device), resolve_output(args.device))
        rec = sd.playrec(tone.reshape(-1, 1), samplerate=INJECT_RATE,
                         channels=1, device=dev)
        sd.wait()
        rms = float(np.sqrt((rec.astype(np.float64) ** 2).mean())) * 32767
        row("silent loopback", rms > 300,
            "RMS %.0f (all-zeros = TCC mic permission denied)" % rms)
    except Exception as exc:
        row("silent loopback", False, str(exc))

    if shutil.which("lume"):
        golden = os.environ.get("ECHOECHO_VM_GOLDEN", config.vm_golden())
        rc = subprocess.run(["lume", "get", golden], capture_output=True)
        row("lume golden image", rc.returncode == 0,
            "%s (%s)" % (golden, "found" if rc.returncode == 0 else
                         "missing — set ECHOECHO_VM_GOLDEN"))
        key = Path(os.path.expanduser(config.vm_ssh_key()))
        row("vm ssh key", key.is_file(),
            "%s (set ECHOECHO_VM_SSH_KEY if elsewhere)" % key)
    else:
        row("lume", False, "not installed — VM scenario will be skipped")

    width = max(len(r[0]) for r in rows)
    ok_all = True
    for name, ok, detail in rows:
        print("  %-*s  %s  %s" % (width, name, "PASS" if ok else "FAIL", detail))
        ok_all = ok_all and (ok or name.startswith(("lume", "vm ")))
    return ok_all


# ---------------------------------------------------------------- main

def load_scenarios(only=None, include_slow=False):
    scenarios = [json.loads(p.read_text(encoding="utf-8"))
                 for p in sorted(SCENARIOS_DIR.glob("*.json"))]
    if only:
        return [s for s in scenarios if s["name"] in only]
    return [s for s in scenarios if include_slow or not s.get("slow")]


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--preflight", action="store_true",
                    help="environment sanity table, no scenarios")
    ap.add_argument("--only", nargs="*", help="scenario names to run")
    ap.add_argument("--include-slow", action="store_true",
                    help="include scenarios marked slow (the VM one)")
    ap.add_argument("--device", default=os.environ.get(
        "ECHOECHO_PLAYTEST_DEVICE", DEFAULT_DEVICE),
        help="loopback audio device name substring (default: %(default)s)")
    ap.add_argument("--voice", default=os.environ.get(
        "ECHOECHO_PLAYTEST_VOICE", ""),
        help="say(1) voice for synthesized turns (default: system voice)")
    ap.add_argument("--viewer-port", type=int, default=DEFAULT_VIEWER_PORT)
    ap.add_argument("--player", metavar="WAV", help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.player:  # internal: Injector's subprocess playback mode
        player_main(args.player, args.device)
        return

    if args.preflight:
        sys.exit(0 if preflight(args) else 1)

    if not preflight(args):
        sys.exit("[voice-playtest] preflight failed — fix the FAIL rows above")

    scenarios = load_scenarios(args.only, args.include_slow)
    if not scenarios:
        sys.exit("no scenarios matched")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    run_dir = RESULTS_ROOT / stamp
    run_dir.mkdir(parents=True)
    print("[voice-playtest] %d scenario(s) -> %s" % (len(scenarios), run_dir))

    rows = []
    for scenario in scenarios:
        print("[voice-playtest] === %s ===" % scenario["name"])
        try:
            rows.append(run_scenario(scenario, args, run_dir / scenario["name"]))
        except Exception as exc:
            rows.append({"scenario": scenario["name"], "error":
                         "%s: %s" % (type(exc).__name__, exc),
                         "checks_passed": 0, "checks_total": 0, "notes": [],
                         "soft": bool(scenario.get("soft"))})

    try:
        sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             cwd=str(REPO_ROOT), capture_output=True,
                             text=True).stdout.strip()
    except OSError:
        sha = "?"
    report = ["# Silent voice playtest — %s" % stamp, "",
              "- revision: `%s`  loopback: `%s`  model: `%s`" % (
                  sha, args.device,
                  os.environ.get("ECHOECHO_REALTIME_MODEL",
                                 "gpt-realtime-2.1-mini")),
              "- every daemon under test was isolated (own viewer port, "
              "recordings dir, token file) and only its own PID signalled; "
              "no audio reached a speaker",
              "- limitations: transcript wording checks depend on the live "
              "model; barge-in is a soft scenario",
              "",
              "| scenario | checks | echoecho voiced (s) | duration | error |",
              "|---|---|---|---|---|"]
    hard_fail = False
    for r in rows:
        ok = r.get("checks_passed", 0) == r.get("checks_total", 0) \
            and not r.get("error")
        if not ok and not r.get("soft"):
            hard_fail = True
        report.append("| %s%s | %d/%d | %s | %ss | %s |" % (
            r["scenario"], " (soft)" if r.get("soft") else "",
            r.get("checks_passed", 0), r.get("checks_total", 0),
            r.get("echoecho_voiced_s", "-"), r.get("duration_s", "-"),
            r.get("error") or ""))
    for r in rows:
        if r.get("notes"):
            report += ["", "## %s — notes" % r["scenario"]] + \
                      ["- " + n for n in r["notes"]]
        failed = [c for c in r.get("checks", []) if not c["pass"]]
        if failed:
            report += ["", "## %s — failed checks" % r["scenario"]] + \
                      ["- `%s` %s" % (c["file"], c["rule"]) for c in failed]
    (run_dir / "report.md").write_text("\n".join(report) + "\n",
                                       encoding="utf-8")
    print("\n".join(report))
    print("[voice-playtest] artifacts in %s" % run_dir)
    sys.exit(1 if hard_fail else 0)


if __name__ == "__main__":
    main()
