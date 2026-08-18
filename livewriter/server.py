"""The Live Writer server: one port, static page + /ws sessions.

Browser (or harness) connects to ws://127.0.0.1:<port>/ws, streams mono
pcm16 @ 24 kHz binary frames, and receives JSON events:

  server -> client
    ready   {asr, fmt, fake}
    ghost   {text, age_ms}          segmenter pending tail (heard, unwritten)
    utt     {id, text}              an utterance closed, queued for the formatter
    think   {on, queued}            formatter working / idle
    op      {gen, utt, op:{...}}    one accepted edit op (animate it)
    wrote   {through_utt, gen}      ops for utterances <= id all sent (drop ghost)
    halted  {gen}                   stop happened; discard queued animation
    status  {text}                  asr transport notes etc.

  client -> server
    {type:"hello"}                  -> ready
    binary frame                    audio
    {type:"halt"}                   stop button (same as saying "stop")
    {type:"reset"}                  clear document
    {type:"text_input", text}       typed utterance (works keyless/fake too)
    {type:"sim_delta", text}        inject an ASR delta (tests; local only)
    {type:"metric", ...}            page-side latency sample -> session log

Every session writes livewriter-logs/<stamp>/session.jsonl (one event per
line, flushed) and doc.md (atomic snapshot on change) — the playtest harness
asserts on those. GET /healthz, /last/doc, /last/log for quick checks.

Static files are repo-shipped and trusted (same stance as viewer /proto);
everything binds 127.0.0.1.
"""

import asyncio
import http
import json
import os
import time

from . import doc as docmod
from .formatter import Formatter, FakeFormatter, Reviewer
from .segmenter import Segmenter

REVIEW_IDLE_S = 3.0      # pen quiet this long -> the editor may pass
REVIEW_MIN_GAP_S = 12.0  # but never more often than this

ROOT = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(ROOT)
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/lw.css": ("lw.css", "text/css; charset=utf-8"),
    "/lw.js": ("lw.js", "text/javascript; charset=utf-8"),
    "/worklet.js": ("worklet.js", "text/javascript; charset=utf-8"),
}
DEFAULT_PORT = 8799


def _now():
    return time.monotonic()


class SessionLog(object):
    def __init__(self, root_dir):
        stamp = time.strftime("%Y-%m-%d_%H%M%S")
        self.dir = os.path.join(root_dir, stamp + "_%d" % (int(time.time() * 1000) % 1000))
        os.makedirs(self.dir, exist_ok=True)
        self.path = os.path.join(self.dir, "session.jsonl")
        self._fh = open(self.path, "a")
        self.t0 = _now()
        # "last session" pointer for /last/*
        try:
            with open(os.path.join(root_dir, "LAST"), "w") as f:
                f.write(self.dir)
        except OSError:
            pass

    def emit(self, **kw):
        kw.setdefault("t", round(_now() - self.t0, 3))
        try:
            self._fh.write(json.dumps(kw, ensure_ascii=False) + "\n")
            self._fh.flush()
        except (OSError, ValueError):
            pass

    def snapshot_doc(self, markdown):
        tmp = os.path.join(self.dir, ".doc.md.tmp")
        try:
            with open(tmp, "w") as f:
                f.write(markdown)
            os.replace(tmp, os.path.join(self.dir, "doc.md"))
        except OSError:
            pass

    def close(self):
        try:
            self._fh.close()
        except OSError:
            pass


class Session(object):
    """One websocket connection = one live document."""

    def __init__(self, ws, cfg):
        self.ws = ws
        self.cfg = cfg
        self.doc = docmod.Doc()
        self.log = SessionLog(cfg["log_dir"])
        self.utt_id = 0
        self.ghost_utts = []  # [(utt_id, text)] emitted, not yet written
        self._send_lock = asyncio.Lock()
        self._doc_dirty = False
        self.seg = Segmenter(self._on_utterance, self._on_stop, self._on_ghost)
        fmt_cls = FakeFormatter if cfg["fake"] else Formatter
        self.fmt = fmt_cls(self.doc, self._send_op, on_think=self._on_think,
                           log=self.log.emit, on_batch_done=self._on_batch_done,
                           model=cfg.get("fmt_model"), api_key=cfg.get("api_key"))
        self.asr = None
        if not cfg["fake"]:
            from .asr import Transcriber
            self.asr = Transcriber(model=cfg.get("asr_model"), api_key=cfg.get("api_key"),
                                   on_delta=self._on_delta, on_status=self._on_asr_status)
        self.reviewer = None
        if not cfg["fake"] and os.environ.get("LIVEWRITER_REVIEW", "1") != "0":
            self.reviewer = Reviewer(self.doc, self._send_op, log=self.log.emit,
                                     api_key=cfg.get("api_key"))
        self.all_utts = []        # [utt_id, text, stopped] — the reviewer's transcript
        self._last_activity = _now()
        self._last_review = 0.0
        self._reviewed_count = 0
        self._review_task = None
        self._tasks = []
        self.audio_bytes = 0
        self.audio_peak = 0

    # -- lifecycle ----------------------------------------------------------
    async def run(self):
        self.fmt.start()
        if self.asr is not None:
            self.asr.start()
        self._tasks.append(asyncio.get_event_loop().create_task(self._ticker()))
        self.log.emit(type="session_start", fake=self.cfg["fake"],
                      asr=getattr(self.asr, "model", "fake"), fmt=self.fmt.model)
        try:
            async for message in self.ws:
                if isinstance(message, bytes):
                    self.audio_bytes += len(message)
                    # cheap liveness meter: a silent mic (bad fake-capture
                    # path, muted hardware) is invisible without this
                    if len(message) >= 2 and self.audio_bytes % 32 == 0:
                        import struct as _s
                        vals = _s.unpack("<%dh" % (len(message) // 2), message)
                        self.audio_peak = max(self.audio_peak, max(abs(v) for v in vals))
                    if self.asr is not None:
                        self.asr.feed_audio(message)
                    continue
                await self._on_text(message)
        finally:
            for t in self._tasks:
                t.cancel()
            if self.asr is not None:
                await self.asr.close()
            await self.fmt.close()
            self.log.emit(type="session_end", audio_bytes=self.audio_bytes,
                          audio_peak=self.audio_peak,
                          calls=self.fmt.calls, dropped_ops=self.fmt.dropped_ops)
            self.log.snapshot_doc(self.doc.to_markdown())
            self.log.close()

    async def _ticker(self):
        while True:
            await asyncio.sleep(0.1)
            self.seg.tick(_now())
            if self._doc_dirty:
                self._doc_dirty = False
                self.log.snapshot_doc(self.doc.to_markdown())
            self._maybe_review()

    def _maybe_review(self):
        if self.reviewer is None or (self._review_task and not self._review_task.done()):
            return
        now = _now()
        written = [u for u in self.all_utts if not u[2]]
        if (len(written) > self._reviewed_count
                and not self.fmt._queue
                and not self.seg.pending.strip()
                and now - self._last_activity >= REVIEW_IDLE_S
                and now - self._last_review >= REVIEW_MIN_GAP_S):
            self._last_review = now
            count = len(written)
            gen0 = self.fmt.gen

            async def run():
                try:
                    await self.reviewer.run_pass(
                        [(u[1], u[2]) for u in self.all_utts],
                        gen_of=lambda: self.fmt.gen,
                        utt_count_of=lambda: len(self.all_utts))
                    if self.fmt.gen == gen0:
                        self._reviewed_count = count
                except Exception as e:
                    self.log.emit(type="review_error", error=str(e)[:200])

            self._review_task = asyncio.get_event_loop().create_task(run())

    # -- client messages ------------------------------------------------------
    async def _on_text(self, message):
        try:
            msg = json.loads(message)
        except ValueError:
            return
        mtype = msg.get("type")
        if mtype == "hello":
            await self._send({"type": "ready", "asr": getattr(self.asr, "model", "fake"),
                              "fmt": self.fmt.model, "fake": self.cfg["fake"],
                              "session_dir": self.log.dir})
        elif mtype == "halt":
            self.log.emit(type="halt", source="client")
            self._halt()
        elif mtype == "reset":
            self._halt()
            del self.doc.lines[:]
            self.fmt.history = []
            self.ghost_utts = []
            self.log.emit(type="reset")
            self.log.snapshot_doc("")
            await self._send({"type": "reset_ok"})
        elif mtype == "text_input":
            text = str(msg.get("text", "")).strip()
            if not text:
                return
            self.log.emit(type="text_input", text=text)
            self.seg.flush(_now())
            if text.lower().rstrip(".!?") == "stop":
                self._halt()
            else:
                self._on_utterance(text, _now(), _now())
        elif mtype == "sim_delta":
            self.seg.feed(str(msg.get("text", "")), _now())
        elif mtype == "metric":
            self.log.emit(type="client_metric",
                          **{k: v for k, v in msg.items() if k != "type"})

    # -- pipeline callbacks ---------------------------------------------------
    def _on_delta(self, text, now):
        self.log.emit(type="asr_delta", text=text)
        self.seg.feed(text, now)

    def _on_ghost(self, pending, t_first):
        age_ms = int((_now() - t_first) * 1000) if t_first else 0
        self._post({"type": "ghost", "text": pending, "age_ms": age_ms})

    def _on_utterance(self, text, t_first, t_last):
        self.utt_id += 1
        uid = self.utt_id
        self.ghost_utts.append((uid, text))
        self.all_utts.append([uid, text, False])
        self._last_activity = _now()
        age_ms = int((_now() - t_first) * 1000) if t_first else 0
        self.log.emit(type="utt", utt=uid, text=text, age_ms=age_ms)
        self._post({"type": "utt", "id": uid, "text": text, "age_ms": age_ms})
        self.fmt.submit(uid, text, t_last)

    def _on_stop(self, discarded):
        self.log.emit(type="halt", source="voice", discarded=discarded)
        self._halt(discarded)

    def _halt(self, discarded=None):
        queued = {u for u, _, _ in self.fmt._queue} if hasattr(self.fmt, "_queue") else set()
        pend = discarded or self.seg.pending.strip()
        gen = self.fmt.halt()
        self.seg.clear()
        # utterances whose ops never landed were interrupted by the stop: the
        # reviewer must not reinstate them — but the writer keeps them as
        # marked context (that is what "scratch that" refers to)
        unwritten = {u for u, _ in self.ghost_utts} | queued
        for u in self.all_utts:
            if u[0] in unwritten:
                u[2] = True
                self.fmt.history.append((u[0], "(stopped) " + u[1]))
        if pend:
            self.fmt.history.append((-1, "(stopped) " + pend))
        self.ghost_utts = []
        self._last_activity = _now()
        self._post({"type": "halted", "gen": gen})

    def _on_think(self, on, queued):
        self._post({"type": "think", "on": bool(on), "queued": queued})

    async def _send_op(self, norm, gen, utt_id):
        if gen != self.fmt.gen:
            return
        self.log.emit(type="op", gen=gen, utt=utt_id, **{"op_": norm})
        self._doc_dirty = True
        self._last_activity = _now()
        await self._send({"type": "op", "gen": gen, "utt": utt_id, "op": norm})

    async def _on_batch_done(self, through_utt, gen):
        self.ghost_utts = [(u, t) for u, t in self.ghost_utts if u > through_utt]
        await self._send({"type": "wrote", "through_utt": through_utt, "gen": gen})

    def _on_asr_status(self, text):
        self.log.emit(type="asr_status", text=text)
        self._post({"type": "status", "text": text})

    # -- ws send helpers ------------------------------------------------------
    def _post(self, obj):
        asyncio.get_event_loop().create_task(self._send(obj))

    async def _send(self, obj):
        try:
            async with self._send_lock:
                await self.ws.send(json.dumps(obj, ensure_ascii=False))
        except Exception:
            pass  # peer gone; run() will unwind


# -- http side ---------------------------------------------------------------

def make_process_request(cfg):
    def process_request(connection, request):
        path = request.path.split("?")[0]
        if path == "/ws":
            return None  # proceed with the websocket handshake
        if path == "/healthz":
            from .formatter import DEFAULT_MODEL
            from .asr import LIVE_MODEL_DEFAULT
            body = json.dumps({"ok": True, "fake": cfg["fake"],
                               "asr": cfg.get("asr_model") or os.environ.get("LIVEWRITER_ASR_MODEL", LIVE_MODEL_DEFAULT),
                               "fmt": cfg.get("fmt_model") or os.environ.get("LIVEWRITER_MODEL", DEFAULT_MODEL),
                               "log_dir": cfg["log_dir"]})
            resp = connection.respond(http.HTTPStatus.OK, body)
            resp.headers["Content-Type"] = "application/json"
            return resp
        if path == "/last/doc" or path == "/last/log":
            try:
                with open(os.path.join(cfg["log_dir"], "LAST")) as f:
                    last = f.read().strip()
                name = "doc.md" if path == "/last/doc" else "session.jsonl"
                with open(os.path.join(last, name)) as f:
                    body = f.read()
                resp = connection.respond(http.HTTPStatus.OK, body or "")
                resp.headers["Content-Type"] = "text/plain; charset=utf-8"
                return resp
            except OSError:
                return connection.respond(http.HTTPStatus.NOT_FOUND, "no session yet\n")
        if path in STATIC:
            fname, ctype = STATIC[path]
            try:
                with open(os.path.join(ROOT, "static", fname)) as f:
                    body = f.read()
            except OSError:
                return connection.respond(http.HTTPStatus.NOT_FOUND, "missing asset\n")
            resp = connection.respond(http.HTTPStatus.OK, body)
            resp.headers["Content-Type"] = ctype
            resp.headers["Cache-Control"] = "no-store"
            return resp
        return connection.respond(http.HTTPStatus.NOT_FOUND, "not found\n")

    return process_request


async def serve(host="127.0.0.1", port=DEFAULT_PORT, fake=False, log_dir=None,
                asr_model=None, fmt_model=None, api_key=None, ready_cb=None):
    from websockets.asyncio.server import serve as ws_serve
    cfg = {
        "fake": fake,
        "log_dir": log_dir or os.environ.get("LIVEWRITER_LOG_DIR",
                                             os.path.join(REPO, "livewriter-logs")),
        "asr_model": asr_model,
        "fmt_model": fmt_model,
        "api_key": api_key or os.environ.get("OPENAI_API_KEY", ""),
    }
    os.makedirs(cfg["log_dir"], exist_ok=True)

    async def handler(ws):
        if ws.request.path.split("?")[0] != "/ws":
            await ws.close()
            return
        await Session(ws, cfg).run()

    async with ws_serve(handler, host, port, process_request=make_process_request(cfg),
                        max_size=None) as server:
        if ready_cb is not None:
            ready_cb(server)
        await asyncio.get_event_loop().create_future()  # run forever
