#!/usr/bin/env python3
"""Live Writer playtests — the real pipeline, driven by synthesized speech.

Each scenario spawns a private livewriter server (own port, own log dir),
synthesizes every turn with TTS (OpenAI gpt-4o-mini-tts by default; macOS
say(1) with --tts say), and streams the audio over the same websocket the
page uses, at real-time pace, 20 ms pcm16 frames, with silent frames between
turns — exactly what a live mic produces. Assertions run against the final
document markdown and the session event log; latency (utterance heard ->
first ink) is reported per scenario.

  python3 scripts/livewriter_playtest.py                     # all scripted scenarios
  python3 scripts/livewriter_playtest.py --only 10_team_update
  python3 scripts/livewriter_playtest.py --browser           # through headless Chrome
                                                             # (mic -> worklet -> ws path)
  python3 scripts/livewriter_playtest.py --generate 3        # invent fresh generative
                                                             # scenarios first, then run them
  python3 scripts/livewriter_playtest.py --judge             # add an LLM judge verdict

Scenario JSON (fixtures/livewriter/*.json):
  name, task, voice?, turns: [ {say, pause_ms?} | {pause_ms} ],
  expects: { contains[], not_contains[], regex[], min_list_items, has_heading,
             min_words },
  judge: { criteria },            # prose for the judge
  latency: { max_p50_first_ink_ms }   # optional hard latency gate

Results land in livewriter-results/<stamp>/: per-scenario dirs with doc.md,
session.jsonl, turns.txt, result.json, and a top-level report.md.
"""

import argparse
import asyncio
import base64
import hashlib
import io
import json
import os
import re
import shutil
import signal
import struct
import subprocess
import sys
import time
import urllib.request
import wave

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(REPO, "fixtures", "livewriter")
RATE = 24000
FRAME = 480  # 20 ms @ 24 kHz
DEFAULT_PORT = 8931


def load_env_local():
    path = os.path.join(REPO, ".env.local")
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())
    except OSError:
        pass


# ---------------------------------------------------------------- tts
class TTS(object):
    def __init__(self, engine, cache_dir, default_voice=None):
        self.engine = engine
        self.cache = cache_dir
        os.makedirs(cache_dir, exist_ok=True)
        self.default_voice = default_voice
        self._client = None

    def synth(self, text, voice=None):
        """-> mono pcm16 @ 24 kHz bytes."""
        voice = voice or self.default_voice or ("alloy" if self.engine == "openai" else "Samantha")
        key = hashlib.sha1((self.engine + "|" + voice + "|" + text).encode()).hexdigest()[:20]
        path = os.path.join(self.cache, key + ".wav")
        if not os.path.exists(path):
            if self.engine == "openai":
                self._synth_openai(text, voice, path)
            else:
                self._synth_say(text, voice, path)
        with wave.open(path) as w:
            assert w.getframerate() == RATE and w.getnchannels() == 1, "bad cache wav"
            pcm = w.readframes(w.getnframes())
        return normalize_pcm(pcm)

    def _synth_openai(self, text, voice, path):
        if self._client is None:
            from openai import OpenAI
            self._client = OpenAI()
        r = self._client.audio.speech.create(model="gpt-4o-mini-tts", voice=voice,
                                             input=text, response_format="wav")
        data = r.read()
        w = wave.open(io.BytesIO(data))
        pcm = w.readframes(w.getnframes())
        pcm = resample(pcm, w.getframerate(), RATE, w.getnchannels())
        write_wav(path, pcm)

    def _synth_say(self, text, voice, path):
        raw = path + ".say.wav"
        cmd = ["say", "-o", raw, "--data-format=LEI16@24000", "--file-format=WAVE"]
        if voice:
            cmd += ["-v", voice]
        cmd.append(text)
        subprocess.run(cmd, check=True)
        with wave.open(raw) as w:
            pcm = w.readframes(w.getnframes())
            pcm = resample(pcm, w.getframerate(), RATE, w.getnchannels())
        os.unlink(raw)
        write_wav(path, pcm)


def resample(pcm, src_rate, dst_rate, channels):
    if channels == 2:  # left channel
        pcm = b"".join(pcm[i:i + 2] for i in range(0, len(pcm), 4))
    if src_rate == dst_rate:
        return pcm
    n = len(pcm) // 2
    m = int(n * dst_rate / src_rate)
    out = bytearray()
    for i in range(m):
        j = min(n - 1, int(i * src_rate / dst_rate))
        out += pcm[j * 2:j * 2 + 2]
    return bytes(out)


def normalize_pcm(pcm, peak=0.7, max_gain=4.0):
    n = len(pcm) // 2
    if not n:
        return pcm
    vals = struct.unpack("<%dh" % n, pcm)
    m = max(1, max(abs(v) for v in vals))
    gain = min(max_gain, peak * 32767.0 / m)
    if abs(gain - 1.0) < 0.05:
        return pcm
    out = struct.pack("<%dh" % n, *[max(-32768, min(32767, int(v * gain))) for v in vals])
    return out


def write_wav(path, pcm, rate=RATE):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)


# ---------------------------------------------------------------- server under test
class ServerProc(object):
    def __init__(self, port, log_dir, fake=False, asr_model=None, fmt_model=None):
        self.port = port
        self.log_dir = log_dir
        env = dict(os.environ)
        env["LIVEWRITER_LOG_DIR"] = log_dir
        cmd = [sys.executable, "-u", "-m", "livewriter", "--port", str(port), "--log-dir", log_dir]
        if fake:
            cmd.append("--fake")
        if asr_model:
            cmd += ["--asr-model", asr_model]
        if fmt_model:
            cmd += ["--model", fmt_model]
        self.proc = subprocess.Popen(cmd, cwd=REPO, env=env,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    def wait_ready(self, timeout=15):
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                urllib.request.urlopen("http://127.0.0.1:%d/healthz" % self.port, timeout=1).read()
                return True
            except Exception:
                if self.proc.poll() is not None:
                    out = self.proc.stdout.read().decode(errors="replace")
                    raise RuntimeError("server died: %s" % out[-2000:])
                time.sleep(0.25)
        return False

    def http(self, path):
        return urllib.request.urlopen(
            "http://127.0.0.1:%d%s" % (self.port, path), timeout=5).read().decode()

    def stop(self):
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGINT)
            try:
                self.proc.wait(8)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
                try:
                    self.proc.wait(4)
                except subprocess.TimeoutExpired:
                    self.proc.kill()


# ---------------------------------------------------------------- ws driver
class Driver(object):
    """Streams scenario audio like a live mic and records every server event."""

    def __init__(self, port):
        self.port = port
        self.events = []
        self.ws = None
        self._reader = None

    async def connect(self):
        import websockets
        self.ws = await websockets.connect("ws://127.0.0.1:%d/ws" % self.port, max_size=None)
        await self.ws.send(json.dumps({"type": "hello"}))
        self._reader = asyncio.get_event_loop().create_task(self._read())
        i = await self._wait(lambda e: e["type"] == "ready", 10)
        # read results from THIS session's dir, never /last/* — another client
        # (a stray page, a parallel test) must not swap the session under us
        self.session_dir = self.events[i].get("session_dir") if i >= 0 else None

    async def _read(self):
        try:
            while True:
                raw = await self.ws.recv()
                if isinstance(raw, bytes):
                    continue
                ev = json.loads(raw)
                ev["_t"] = time.monotonic()
                self.events.append(ev)
        except Exception:
            pass

    async def _wait(self, pred, timeout, start=0):
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            for i in range(start, len(self.events)):
                if pred(self.events[i]):
                    return i
            await asyncio.sleep(0.1)
        return -1

    async def stream_pcm(self, pcm, realtime=1.0):
        """20 ms frames at (1/realtime)x pace."""
        t_start = time.monotonic()
        sent = 0
        for i in range(0, len(pcm), FRAME * 2):
            await self.ws.send(pcm[i:i + FRAME * 2])
            sent += 1
            target = t_start + sent * 0.02 / realtime
            delay = target - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)

    async def silence(self, ms, realtime=1.0):
        await self.stream_pcm(b"\x00" * (2 * int(RATE * ms / 1000.0)), realtime)

    async def settle(self, idle_s=4.0, timeout=90.0):
        """Wait until the pipeline goes quiet: no new op/utt/ghost-text events
        for idle_s (thinking pauses included)."""
        t0 = time.monotonic()

        def busy_t():
            t = 0.0
            for e in self.events:
                if e["type"] in ("op", "utt", "wrote") or (e["type"] == "ghost" and e.get("text")):
                    t = max(t, e["_t"])
                if e["type"] == "think" and e.get("on"):
                    t = max(t, e["_t"])
            return t

        while time.monotonic() - t0 < timeout:
            last = busy_t()
            if last and time.monotonic() - last >= idle_s:
                return True
            if not last and time.monotonic() - t0 > 20:
                return False
            await asyncio.sleep(0.25)
        return False

    async def close(self):
        try:
            await self.ws.close()
        except Exception:
            pass
        if self._reader:
            self._reader.cancel()


# ---------------------------------------------------------------- browser driver
def build_scenario_wav(scenario, tts, path, lead_ms=1200):
    pcm = b"\x00" * (2 * int(RATE * lead_ms / 1000.0))
    for turn in scenario["turns"]:
        if turn.get("say"):
            pcm += tts.synth(turn["say"], scenario.get("voice"))
        pcm += b"\x00" * (2 * int(RATE * turn.get("pause_ms", 900) / 1000.0))
    pcm += b"\x00" * (2 * RATE * 2)
    write_wav(path, pcm)
    return len(pcm) / (2.0 * RATE)


def find_chrome():
    for c in ("google-chrome", "google-chrome-stable", "chromium-browser", "chromium",
              "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"):
        p = shutil.which(c) if not c.startswith("/") else (c if os.path.exists(c) else None)
        if p:
            return p
    return None


def run_browser(port, wav_path, duration_s, headless=True, extra_wait_s=12):
    chrome = find_chrome()
    if not chrome:
        raise RuntimeError("no chrome found")
    args = [chrome,
            "--use-fake-device-for-media-stream",
            "--use-fake-ui-for-media-stream",
            "--use-file-for-fake-audio-capture=%s%%noloop" % wav_path,
            "--autoplay-policy=no-user-gesture-required",
            "--no-first-run", "--no-default-browser-check",
            "--user-data-dir=/tmp/lw-chrome-%d" % port,
            ]
    if headless:
        args += ["--headless=new", "--mute-audio", "--disable-gpu"]
    args.append("http://127.0.0.1:%d/?autostart=1&test=1" % port)
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(duration_s + extra_wait_s)
    finally:
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()


# ---------------------------------------------------------------- checks
def check_expects(md, expects, log_events):
    results = []

    def add(rule, ok, note=""):
        results.append({"rule": rule, "pass": bool(ok), "note": note})

    low = md.lower()
    for s in expects.get("contains", []):
        add("contains %r" % s, s.lower() in low)
    for s in expects.get("not_contains", []):
        add("not_contains %r" % s, s.lower() not in low)
    for rx in expects.get("regex", []):
        add("regex %r" % rx, re.search(rx, md) is not None)
    n = expects.get("min_list_items")
    if n:
        items = len(re.findall(r"^\s*[-*] ", md, re.M))
        add("min_list_items %d" % n, items >= n, "found %d" % items)
    if expects.get("has_heading"):
        add("has_heading", re.search(r"^#{1,3} ", md, re.M) is not None)
    w = expects.get("min_words")
    if w:
        words = len(md.split())
        add("min_words %d" % w, words >= w, "found %d" % words)
    if expects.get("stopped"):
        add("halt event", any(e.get("type") == "halt" for e in log_events))
    return results


def latency_stats(log_events):
    """utterance heard (first word) -> first op of the batch containing it."""
    utts = {}
    for e in log_events:
        if e.get("type") == "utt":
            utts[e["utt"]] = {"heard": e["t"] - e.get("age_ms", 0) / 1000.0, "utt_t": e["t"]}
    firsts = {}
    for e in log_events:
        if e.get("type") == "op" and e.get("utt") in utts and e["utt"] not in firsts:
            firsts[e["utt"]] = e["t"]
    inks = []
    finals = []
    for u, t in firsts.items():
        inks.append((t - utts[u]["heard"]) * 1000.0)
        finals.append((t - utts[u]["utt_t"]) * 1000.0)
    ttfops = [e.get("ttfop_ms") for e in log_events if e.get("type") == "fmt_done" and e.get("ops")]
    out = {}
    if inks:
        inks.sort()
        finals.sort()
        out["n_utts"] = len(inks)
        out["heard_to_ink_p50_ms"] = int(inks[len(inks) // 2])
        out["heard_to_ink_p90_ms"] = int(inks[int(len(inks) * 0.9)])
        out["final_to_ink_p50_ms"] = int(finals[len(finals) // 2])
    if ttfops:
        ttfops.sort()
        out["fmt_ttfop_p50_ms"] = int(ttfops[len(ttfops) // 2])
    return out


# ---------------------------------------------------------------- judge
JUDGE_PROMPT = """You are grading a "Live Writer" dictation session: a person spoke out loud and an AI wrote a document live.

WHAT WAS SPOKEN (turn by turn):
%s

SCENARIO INTENT: %s
JUDGE CRITERIA: %s

FINAL DOCUMENT (markdown):
---
%s
---

Grade the document. Score each 0-10:
- fidelity: is every substantive fact/idea spoken captured (unless retracted/scratched)? nothing invented?
- formatting: clean structure (headings/lists/numbers/paragraphs where appropriate), no filler words, reads like a good writer typed it
- commands: were spoken commands (corrections, scratch that, make that a list, stop, etc.) executed rather than transcribed?

Reply with ONLY JSON: {"fidelity": n, "formatting": n, "commands": n, "pass": true/false, "issues": ["..."]}
pass=false if any substantive spoken content is missing/invented or a command was written as text."""


def judge(scenario, md, model):
    from openai import OpenAI
    turns = "\n".join("- %r" % t["say"] for t in scenario["turns"] if t.get("say"))
    crit = (scenario.get("judge") or {}).get("criteria", "see intent")
    prompt = JUDGE_PROMPT % (turns, scenario.get("task", ""), crit, md)
    client = OpenAI()
    kwargs = dict(model=model, input=prompt, max_output_tokens=4000)
    if model.startswith("gpt-5"):
        kwargs["reasoning"] = {"effort": "medium"}
    resp = client.responses.create(**kwargs)
    text = resp.output_text.strip()
    m = re.search(r"\{.*\}", text, re.S)
    try:
        return json.loads(m.group(0)) if m else {"error": text[:300]}
    except ValueError:
        return {"error": text[:300]}


# ---------------------------------------------------------------- generator
GEN_PROMPT = """Invent ONE test scenario for a "Live Writer" dictation app (a person talks out loud, an AI writes a clean formatted document live).

Genre for this one: %s.

The speaker must sound like a real human thinking out loud: include natural disfluencies (um, okay so), at least one SELF-CORRECTION mid-flow ("no wait, make that..."), and at least one spoken COMMAND from: "scratch that", "new paragraph", "make that a list", "change X to Y", "heading ...". Include at least one number/date/price said in words that should become figures. 6-10 turns, each 5-25 words, natural spoken punctuation.

Reply ONLY with JSON:
{"name": "gen_<short_slug>", "task": "<one line: who is dictating what>",
 "voice": "<one of: alloy, echo, shimmer, coral, verse, ballad>",
 "turns": [{"say": "...", "pause_ms": 700}, ...],
 "expects": {"contains": [<3-6 short strings that MUST appear in the final doc — only unambiguous ones, e.g. the corrected name, a converted figure like "$40">],
             "not_contains": [<2-4 strings that must NOT appear: retracted words, filler like "um", command words like "scratch that">],
             "min_list_items": <n or omit>, "has_heading": <true or omit>},
 "judge": {"criteria": "<one line on what good looks like>"}}

Make expects extremely reliable: not_contains strings must be things a correct writer would never include; contains strings must be forced by the dictation. Lowercase matching is used."""

GENRES = ["meeting notes", "cooking recipe", "personal email", "product spec",
          "short story opening", "lecture notes about a science topic", "weekly todo planning",
          "sports match recap", "short poem with a title", "customer support case summary",
          "travel itinerary", "startup pitch outline", "apartment renovation plan",
          "podcast episode outline", "biology field observations"]


def generate_scenarios(n, model, out_dir, seed_idx=0):
    from openai import OpenAI
    client = OpenAI()
    os.makedirs(out_dir, exist_ok=True)
    paths = []
    for i in range(n):
        genre = GENRES[(seed_idx + i) % len(GENRES)]
        kwargs = dict(model=model, input=GEN_PROMPT % genre, max_output_tokens=6000)
        if model.startswith("gpt-5"):
            kwargs["reasoning"] = {"effort": "medium"}
        resp = client.responses.create(**kwargs)
        m = re.search(r"\{.*\}", resp.output_text, re.S)
        if not m:
            print("  generator gave no JSON for %s" % genre)
            continue
        try:
            sc = json.loads(m.group(0))
        except ValueError as e:
            print("  generator JSON error for %s: %s" % (genre, e))
            continue
        sc["generated"] = True
        sc["genre"] = genre
        name = re.sub(r"[^a-z0-9_]+", "", sc.get("name", "gen_x").lower()) or "gen_x"
        stamp = time.strftime("%H%M%S")
        path = os.path.join(out_dir, "%s_%s.json" % (name, stamp))
        with open(path, "w") as f:
            json.dump(sc, f, indent=1)
        paths.append(path)
        print("  generated: %s (%s)" % (os.path.basename(path), genre))
    return paths


# ---------------------------------------------------------------- run one
async def run_scenario(scenario, args, out_dir, port):
    os.makedirs(out_dir, exist_ok=True)
    log_dir = os.path.join(out_dir, "logs")
    tts = TTS(args.tts, os.path.join(args.results_root, ".tts-cache"))
    srv = ServerProc(port, log_dir, fake=args.fake,
                     asr_model=args.asr_model, fmt_model=args.fmt_model)
    result = {"scenario": scenario["name"], "error": None, "checks": [], "latency": {}}
    t0 = time.time()
    try:
        if not srv.wait_ready():
            raise RuntimeError("server not ready")
        session_dir = None
        if args.browser:
            wav = os.path.join(out_dir, "input.wav")
            dur = build_scenario_wav(scenario, tts, wav)
            run_browser(port, wav, dur, headless=not args.headful)
        else:
            drv = Driver(port)
            await drv.connect()
            session_dir = drv.session_dir
            for turn in scenario["turns"]:
                if turn.get("say"):
                    pcm = tts.synth(turn["say"], scenario.get("voice"))
                    await drv.stream_pcm(pcm, realtime=args.pace)
                await drv.silence(turn.get("pause_ms", 900), realtime=args.pace)
            await drv.settle()
            await drv.close()
        time.sleep(0.6)
        if session_dir:
            with open(os.path.join(session_dir, "doc.md")) as f:
                md = f.read()
            with open(os.path.join(session_dir, "session.jsonl")) as f:
                raw_log = f.read()
        else:
            md = srv.http("/last/doc")
            raw_log = srv.http("/last/log")
    finally:
        srv.stop()
    events = [json.loads(l) for l in raw_log.splitlines() if l.strip()]
    with open(os.path.join(out_dir, "doc.md"), "w") as f:
        f.write(md)
    with open(os.path.join(out_dir, "session.jsonl"), "w") as f:
        f.write(raw_log)
    with open(os.path.join(out_dir, "turns.txt"), "w") as f:
        for t in scenario["turns"]:
            f.write((t.get("say") or "(pause %sms)" % t.get("pause_ms")) + "\n")
    result["checks"] = check_expects(md, scenario.get("expects", {}), events)
    result["latency"] = latency_stats(events)
    lat_gate = (scenario.get("latency") or {}).get("max_p50_first_ink_ms")
    if lat_gate and result["latency"].get("heard_to_ink_p50_ms", 0) > lat_gate:
        result["checks"].append({"rule": "latency p50 <= %d" % lat_gate, "pass": False,
                                 "note": "%s" % result["latency"].get("heard_to_ink_p50_ms")})
    if args.judge:
        try:
            result["judge"] = judge(scenario, md, args.judge_model)
        except Exception as e:
            result["judge"] = {"error": str(e)[:200]}
    result["duration_s"] = round(time.time() - t0, 1)
    result["doc_words"] = len(md.split())
    with open(os.path.join(out_dir, "result.json"), "w") as f:
        json.dump(result, f, indent=1)
    return result


def load_scenarios(args):
    paths = []
    for d in [FIXTURES, os.path.join(FIXTURES, "generated")]:
        if os.path.isdir(d):
            paths += sorted(os.path.join(d, p) for p in os.listdir(d) if p.endswith(".json"))
    out = []
    for p in paths:
        try:
            with open(p) as f:
                sc = json.load(f)
        except ValueError as e:
            print("bad scenario %s: %s" % (p, e))
            continue
        if args.only and args.only not in sc.get("name", ""):
            continue
        if sc.get("generated") and not args.include_generated and not args.only:
            continue
        out.append(sc)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="substring filter on scenario name")
    ap.add_argument("--browser", action="store_true", help="drive via headless Chrome fake mic")
    ap.add_argument("--headful", action="store_true")
    ap.add_argument("--fake", action="store_true", help="fake formatter (plumbing test)")
    ap.add_argument("--tts", choices=["openai", "say"], default="openai")
    ap.add_argument("--pace", type=float, default=1.0, help="realtime multiplier (1.0 = live)")
    ap.add_argument("--asr-model", default=None)
    ap.add_argument("--fmt-model", default=None)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--judge", action="store_true")
    ap.add_argument("--judge-model", default=os.environ.get("LIVEWRITER_JUDGE_MODEL", "gpt-5.2"))
    ap.add_argument("--generate", type=int, default=0, help="invent N new scenarios, then run them")
    ap.add_argument("--gen-model", default=os.environ.get("LIVEWRITER_GEN_MODEL", "gpt-5.2"))
    ap.add_argument("--gen-seed", type=int, default=int(time.time()) % 1000)
    ap.add_argument("--include-generated", action="store_true",
                    help="also run previously generated scenarios")
    ap.add_argument("--results", default=None)
    args = ap.parse_args()

    load_env_local()
    if not args.fake and not os.environ.get("OPENAI_API_KEY"):
        print("OPENAI_API_KEY required (or --fake)")
        return 2

    stamp = time.strftime("%Y-%m-%d_%H%M%S")
    args.results_root = os.path.join(REPO, "livewriter-results")
    results_dir = args.results or os.path.join(args.results_root, stamp)
    os.makedirs(results_dir, exist_ok=True)

    if args.generate:
        print("generating %d scenarios (%s)..." % (args.generate, args.gen_model))
        gen_paths = generate_scenarios(args.generate, args.gen_model,
                                       os.path.join(FIXTURES, "generated"), args.gen_seed)
        if not args.only and gen_paths:
            args.include_generated = False
            scenarios = []
            for p in gen_paths:
                with open(p) as f:
                    scenarios.append(json.load(f))
        else:
            scenarios = load_scenarios(args)
    else:
        scenarios = load_scenarios(args)

    if not scenarios:
        print("no scenarios matched")
        return 1

    all_results = []
    port = args.port
    for sc in scenarios:
        print("== %s ==" % sc["name"])
        out_dir = os.path.join(results_dir, sc["name"])
        try:
            res = asyncio.run(run_scenario(sc, args, out_dir, port))
        except Exception as e:
            res = {"scenario": sc["name"], "error": str(e)[:500], "checks": [], "latency": {}}
            with open(os.path.join(out_dir, "result.json"), "w") as f:
                json.dump(res, f, indent=1)
        port += 1
        all_results.append(res)
        ok = sum(1 for c in res["checks"] if c["pass"])
        print("   checks %d/%d  latency %s  %s" % (
            ok, len(res["checks"]), res.get("latency"), ("ERROR: " + res["error"]) if res.get("error") else ""))
        for c in res["checks"]:
            if not c["pass"]:
                print("   FAIL: %s %s" % (c["rule"], c.get("note", "")))
        if res.get("judge"):
            print("   judge: %s" % json.dumps(res["judge"])[:200])

    # report
    lines = ["# Live Writer playtest — %s" % stamp, "",
             "| scenario | checks | judge | p50 heard→ink | p50 final→ink | fmt ttfop | dur |",
             "|---|---|---|---|---|---|---|"]
    for r in all_results:
        ok = sum(1 for c in r["checks"] if c["pass"])
        lat = r.get("latency", {})
        j = r.get("judge") or {}
        js = ("%s/%s/%s%s" % (j.get("fidelity", "-"), j.get("formatting", "-"), j.get("commands", "-"),
                              "" if j.get("pass", True) else " ✗")) if j and "error" not in j else ("err" if j else "—")
        lines.append("| %s | %d/%d%s | %s | %s | %s | %s | %ss |" % (
            r["scenario"], ok, len(r["checks"]), " ⚠️" if r.get("error") else "",
            js,
            lat.get("heard_to_ink_p50_ms", "—"), lat.get("final_to_ink_p50_ms", "—"),
            lat.get("fmt_ttfop_p50_ms", "—"), r.get("duration_s", "—")))
        if r.get("error"):
            lines.append("")
            lines.append("**%s error:** `%s`" % (r["scenario"], r["error"]))
    for r in all_results:
        fails = [c for c in r["checks"] if not c["pass"]]
        if fails:
            lines.append("")
            lines.append("## %s — failed checks" % r["scenario"])
            for c in fails:
                lines.append("- %s %s" % (c["rule"], c.get("note", "")))
        if r.get("judge") and r["judge"].get("issues"):
            lines.append("")
            lines.append("## %s — judge issues" % r["scenario"])
            for i in r["judge"]["issues"]:
                lines.append("- %s" % i)
    with open(os.path.join(results_dir, "report.md"), "w") as f:
        f.write("\n".join(lines) + "\n")
    print("report: %s" % os.path.join(results_dir, "report.md"))

    hard_fail = any(r.get("error") or any(not c["pass"] for c in r["checks"]) for r in all_results)
    return 1 if hard_fail else 0


if __name__ == "__main__":
    sys.exit(main())
