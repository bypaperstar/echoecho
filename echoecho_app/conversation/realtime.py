"""Raw OpenAI Realtime WebSocket client (GA API) behind a Transport port.

Transport port: async send(dict), async recv() -> dict, async close();
optional async connect(). Real endpoint (Mac-side, untested here):
wss://api.openai.com/v1/realtime?model=<model>, Authorization: Bearer,
NO beta header. FakeTransport replays recorded server-event JSONL from
fixtures/realtime/*.jsonl and records every client send — the tests over it
ARE this layer's spec, since the live API is unreachable from the sandbox.
"""
import asyncio
import base64
import binascii
import hashlib
import json
import os
import time
from collections import Counter
from pathlib import Path

from echoecho_app import config, diagnostics, events
from echoecho_app.conversation.port import ConversationPort, TOOL_NAMES
from echoecho_app.conversation.session import ACTIVE, ENDING, Session
from echoecho_app.conversation.textmode import build_tools

VOICE_SUFFIX = " You are speaking out loud: keep each turn to a sentence or two."


def voice_prompt():
    """Tuned prompt + speaking suffix, built at call time so the generated
    kinds line reflects whatever load_all() registered."""
    return config.system_prompt() + VOICE_SUFFIX
SIGN_OFF_INSTRUCTIONS = ("The session is ending. Say a brief, friendly "
                         "one-sentence goodbye.")
RECONNECT_SECS = 55 * 60  # 60-min API cap; summary+reconnect a bit before it
KNOWN_SERVER_EVENT_TYPES = {
    "session.created", "response.created", "response.output_audio.delta",
    "response.done", "input_audio_buffer.speech_started",
    "input_audio_buffer.speech_stopped",
    "conversation.item.input_audio_transcription.completed",
    "response.output_audio_transcript.done",
    "response.audio_transcript.done", "error",
}


def pcm16_ms(nbytes, rate=24000):
    """Duration of a mono pcm16 byte blob in milliseconds."""
    return nbytes / (rate * 2.0) * 1000.0


class PlaybackTracker:
    """Books appended audio ms per assistant item; a play cursor advances as
    audio actually reaches the speaker. On barge-in, truncate() reports which
    item the cursor is inside and how much of it was heard (audio_end_ms)."""

    def __init__(self):
        self._items = []  # [[item_id, total_ms], ...] in append order
        self._played_ms = 0.0

    def append(self, item_id, ms):
        if self._items and self._items[-1][0] == item_id:
            self._items[-1][1] += ms
        else:
            self._items.append([item_id, float(ms)])

    def advance(self, ms):
        self._played_ms = min(self._played_ms + ms, self.total_ms())

    def total_ms(self):
        return sum(ms for _, ms in self._items)

    def truncate(self):
        """Flush the queue; return (item_id, audio_end_ms) for the item under
        the play cursor, or None if everything appended was already played."""
        cursor, hit = self._played_ms, None
        for item_id, ms in self._items:
            if cursor < ms:
                hit = (item_id, int(cursor))
                break
            cursor -= ms
        self._items, self._played_ms = [], 0.0
        return hit


class TransportClosed(Exception):
    pass


class WebSocketTransport:
    """Real GA-endpoint transport (Mac-side; never exercised in sandbox CI)."""

    def __init__(self, model, api_key=None):
        self.model = model
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self.url = "wss://api.openai.com/v1/realtime?model=" + model
        self._ws = None

    async def connect(self):
        import websockets  # lazy: sandbox tests never hit the network
        started = time.monotonic()
        try:
            self._ws = await websockets.connect(
                self.url,
                additional_headers={"Authorization": "Bearer " + self.api_key},
                max_size=None)
        except Exception as exc:
            diagnostics.exception(
                "realtime.transport.connect_failed", exc=exc, model=self.model,
                duration_ms=round((time.monotonic() - started) * 1000, 1))
            raise
        diagnostics.info(
            "realtime.transport.connected", model=self.model,
            duration_ms=round((time.monotonic() - started) * 1000, 1))

    async def send(self, event):
        import websockets.exceptions  # NOT bare `import websockets`: the
        # lazy top-level module doesn't expose `.exceptions` (AttributeError)
        try:
            await self._ws.send(json.dumps(event))
        except websockets.exceptions.ConnectionClosed as exc:
            # Must match recv(): the client's reconnect path only catches
            # TransportClosed, and a WS death is just as often noticed on a
            # send (injection gate, function_call_output ack) as on a recv.
            diagnostics.warning("realtime.transport.closed", operation="send",
                                close_code=getattr(exc, "code", None))
            raise TransportClosed() from exc

    async def recv(self):
        import websockets.exceptions  # see send(): bare import lacks .exceptions
        try:
            return json.loads(await self._ws.recv())
        except websockets.exceptions.ConnectionClosed as exc:
            diagnostics.warning("realtime.transport.closed", operation="recv",
                                close_code=getattr(exc, "code", None))
            raise TransportClosed() from exc

    async def close(self):
        if self._ws is not None:
            started = time.monotonic()
            await self._ws.close()
            diagnostics.info(
                "realtime.transport.closed", operation="close",
                duration_ms=round((time.monotonic() - started) * 1000, 1))


class FakeTransport:
    """Replays server events from a JSONL fixture (path or list of dicts) and
    records every client send. Fixture entries whose type starts with "_" are
    control hooks dispatched to self.hooks[type] (e.g. tests wire "_inject" to
    client.inject, "_clock.advance" to a fake clock) — they let tests place
    adversarial orderings at exact points in the server stream."""

    def __init__(self, source, max_idle_polls=400):
        if isinstance(source, (str, Path)):
            lines = Path(source).read_text(encoding="utf-8").splitlines()
            self.events = [json.loads(ln) for ln in lines
                           if ln.strip() and not ln.strip().startswith("//")]
        else:
            self.events = [dict(e) for e in source]
        self.sent = []
        self.hooks = {}
        self.closed = False
        self.max_idle_polls = max_idle_polls
        self._idle = 0
        self._closed_evt = None

    async def send(self, event):
        if self.closed:
            raise TransportClosed()
        self.sent.append(event)

    async def recv(self):
        while self.events:
            ev = self.events.pop(0)
            if ev.get("type", "").startswith("_"):
                fn = self.hooks.get(ev["type"])
                if fn:
                    fn(ev)
                continue
            return ev
        if self.closed:
            raise TransportClosed()
        self._idle += 1
        if self._idle > self.max_idle_polls:  # safety: never hang a test
            raise TransportClosed()
        if self._closed_evt is None:
            self._closed_evt = asyncio.Event()
        await self._closed_evt.wait()
        raise TransportClosed()

    async def close(self):
        self.closed = True
        if self._closed_evt is not None:
            self._closed_evt.set()

    def sent_types(self):
        return [e.get("type") for e in self.sent]


def build_session_update(instructions=None):
    """GA session.update: instructions, the exact 4 Contract-A tools, pcm16
    24 kHz in/out, far-field cleanup, semantic VAD with barge-in, and input
    transcription. Local AudioPipeline handles acoustic echo separately."""
    return {"type": "session.update", "session": {
        "type": "realtime",
        "instructions": instructions or voice_prompt(),
        "output_modalities": ["audio"],
        "audio": {
            "input": {
                "format": {"type": "audio/pcm", "rate": 24000},
                # Laptop/conference-room microphone cleanup happens before
                # server VAD. Local WebRTC AEC remains responsible for the
                # separate problem of removing Echo's own speaker reference.
                "noise_reduction": {"type": "far_field"},
                "transcription": {"model": "gpt-4o-mini-transcribe"},
                "turn_detection": {"type": "semantic_vad",
                                   "interrupt_response": True},
            },
            "output": {"format": {"type": "audio/pcm", "rate": 24000}},
        },
        "tools": build_tools(),
        "tool_choice": "auto",
    }}


def _system_item(text):
    return {"type": "conversation.item.create",
            "item": {"type": "message", "role": "system",
                     "content": [{"type": "input_text", "text": text}]}}


class RealtimeClient(ConversationPort):
    def __init__(self, transport, session=None, out=print, poll_interval=0.25,
                 on_audio=None, flush_playback=None, transport_factory=None,
                 max_session_secs=RECONNECT_SECS, sign_off_max_polls=40,
                 instructions=None, since_last_session=None,
                 max_reconnects=3, reconnect_backoff=1.0):
        self.transport = transport
        self.session = session or Session()
        self.clock = self.session.clock
        self.out = out
        self.poll_interval = poll_interval
        self.instructions = instructions
        self.tracker = PlaybackTracker()
        self._on_audio = on_audio            # cb(item_id, b64_delta) -> speaker (PR 5)
        self._flush_playback = flush_playback or (lambda: None)
        self._transport_factory = transport_factory
        self.max_session_secs = max_session_secs
        self.sign_off_max_polls = sign_off_max_polls
        self.since_last_session = since_last_session
        self.max_reconnects = max_reconnects
        self.reconnect_backoff = reconnect_backoff
        self._reconnects = 0
        self._tool_cb = None
        self._done = False
        self._signing_off = False
        self._sign_off_done = False
        self._sign_off_polls = 0
        self._connected_at = self.clock()
        self._connected = False
        self._run_started = time.monotonic()
        self._sent_counts = Counter()
        self._recv_counts = Counter()
        self._sent_bytes = 0
        self._recv_bytes = 0
        self._response_started_at = None
        self._response_first_audio_at = None
        self._response_seq = 0
        self._summary_emitted = False
        self._protocol_errors = Counter()

    def _protocol_issue(self, kind, level="warning", **fields):
        """Aggregate malformed upstream frames without event-loop log floods."""
        self._protocol_errors[kind] += 1
        count = self._protocol_errors[kind]
        if count <= 3 or count & (count - 1) == 0:
            fields["occurrences"] = count
            emit = diagnostics.error if level == "error" else diagnostics.warning
            emit("realtime.protocol.%s" % kind, **fields)

    # -- ConversationPort -----------------------------------------------------

    def on_tool(self, cb):
        self._tool_cb = cb

    def inject(self, injection):
        self.session.queue_injection(injection)  # delivered by the gate in _tick

    async def end(self):
        self.session.begin_ending("forced")

    async def run(self):
        try:
            await self.connect()
            while not self._done:
                try:
                    try:
                        event = await asyncio.wait_for(
                            self.transport.recv(), self.poll_interval)
                    except asyncio.TimeoutError:
                        event = None
                    if event is not None:
                        await self._handle_event(event)
                    if self.session.check_silence():
                        self._log("silence timeout — closing (sign-off skipped)")
                    await self._tick()
                except TransportClosed:
                    # WS died mid-session: reconnect with backoff so the daemon
                    # never dies; if that fails, fall through to a clean IDLE.
                    diagnostics.warning("realtime.transport.lost",
                                        state=self.session.state)
                    if not await self._try_reconnect():
                        break
            if self.session.state == ACTIVE:
                self.session.begin_ending("transport_closed")
            if self.session.state == ENDING:
                self.session.finish()
        finally:
            self._emit_summary()

    # -- connection ------------------------------------------------------------

    async def connect(self):
        """Connect and configure once; audio capture may be enabled afterward."""
        if not self._connected:
            await self._connect()

    def send_input_audio(self, event):
        """Send one mic append to the currently active transport."""
        return self._send(event)

    async def _send(self, event):
        """Aggregate protocol telemetry without recording event payloads."""
        etype = str(event.get("type", "unknown"))
        self._sent_counts[etype] += 1
        if etype == "input_audio_buffer.append":
            self._sent_bytes += len(event.get("audio", "")) * 3 // 4
        else:
            try:
                self._sent_bytes += len(json.dumps(event, default=str))
            except Exception:
                pass
        return await self.transport.send(event)

    async def _connect(self, reconnect=False):
        started = time.monotonic()
        connect = getattr(self.transport, "connect", None)
        if connect is not None:
            await connect()
        await self._send(build_session_update(self.instructions))
        if reconnect:
            # summary stub (cookbook pattern): a real rolling summary is v1+
            await self._send(_system_item(
                "[reconnected] The previous connection dropped or neared the "
                "60-minute cap and was refreshed; continue where the "
                "conversation left off."))
        elif self.since_last_session:
            # tasks that finished while IDLE, surfaced on wake
            await self._send(_system_item(self.since_last_session))
        events.emit("session", event="connected",
                    model=getattr(self.transport, "model", ""))
        self._connected_at = self.clock()
        self._connected = True
        self.session.wake()  # no-op if the FSM is already ACTIVE
        diagnostics.info(
            "realtime.session.configured", reconnect=reconnect,
            model=getattr(self.transport, "model", ""),
            duration_ms=round((time.monotonic() - started) * 1000, 1),
            since_last_session=bool(self.since_last_session))

    async def _try_reconnect(self):
        """Called when the transport dies mid-ACTIVE. Retries via
        transport_factory with linear backoff; returns True once reconnected."""
        if self._transport_factory is None or self.session.state != ACTIVE:
            diagnostics.warning("realtime.reconnect.unavailable",
                                factory=bool(self._transport_factory),
                                state=self.session.state)
            return False
        while self._reconnects < self.max_reconnects:
            self._reconnects += 1
            backoff = self.reconnect_backoff * self._reconnects
            diagnostics.warning("realtime.reconnect.started",
                                attempt=self._reconnects,
                                max_attempts=self.max_reconnects,
                                backoff_s=backoff)
            await asyncio.sleep(backoff)
            self._log("transport lost — reconnect %d/%d"
                      % (self._reconnects, self.max_reconnects))
            events.emit("session", event="reconnecting",
                        detail="attempt %d/%d" % (self._reconnects,
                                                  self.max_reconnects))
            try:
                self.transport = self._transport_factory()
                self._connected = False
                await self._connect(reconnect=True)
                diagnostics.info("realtime.reconnect.succeeded",
                                 attempt=self._reconnects)
                return True
            except Exception as exc:
                self._log("reconnect failed: %s" % exc)
                diagnostics.exception("realtime.reconnect.failed", exc=exc,
                                      attempt=self._reconnects)
        diagnostics.error("realtime.reconnect.exhausted",
                          attempts=self._reconnects)
        return False

    async def _maybe_reconnect(self):
        if (self._transport_factory is None
                or self.clock() - self._connected_at < self.max_session_secs):
            return
        self._log("55-min session cap — best-effort summary + reconnect")
        diagnostics.info("realtime.refresh.started",
                         connected_s=round(self.clock() - self._connected_at, 1))
        try:
            await self.transport.close()
        except Exception as exc:
            diagnostics.exception("realtime.refresh.close_failed", exc=exc)
        self.transport = self._transport_factory()
        self._connected = False
        await self._connect(reconnect=True)
        diagnostics.info("realtime.refresh.finished")

    # -- server events -----------------------------------------------------------

    async def _handle_event(self, event):
        if not isinstance(event, dict):
            self._protocol_issue("invalid_event", level="error",
                                 value_type=type(event).__name__)
            return
        t = event.get("type", "")
        if not isinstance(t, str):
            self._protocol_issue("invalid_event_type", level="error",
                                 value_type=type(t).__name__)
            return
        self._recv_counts[t if t in KNOWN_SERVER_EVENT_TYPES else "unknown"] += 1
        if t == "response.output_audio.delta":
            delta = event.get("delta", "")
            if not isinstance(delta, str):
                self._protocol_issue("invalid_audio", level="error",
                                     encoded_type=type(delta).__name__)
                return
            self._recv_bytes += len(delta) * 3 // 4
        else:
            try:
                self._recv_bytes += len(json.dumps(event, default=str))
            except Exception:
                pass
        if t == "session.created":
            session_data = event.get("session")
            if not isinstance(session_data, dict):
                self._protocol_issue(
                    "invalid_session", value_type=type(session_data).__name__)
                session_data = {}
            upstream_id = session_data.get("id")
            self._log("session %s created" % upstream_id)
            diagnostics.info("realtime.session.created",
                             realtime_session_id=upstream_id)
        elif t == "response.created":
            self._response_seq += 1
            self._response_started_at = time.monotonic()
            self._response_first_audio_at = None
            diagnostics.info("realtime.response.started",
                             response_seq=self._response_seq)
            self.session.note_assistant_response_started()
        elif t == "response.output_audio.delta":
            try:
                decoded_bytes = len(base64.b64decode(delta, validate=True))
            except (binascii.Error, ValueError) as exc:
                self._protocol_issue(
                    "invalid_audio", level="error",
                    error_type=type(exc).__name__, encoded_bytes=len(delta))
                return
            if self._response_first_audio_at is None:
                self._response_first_audio_at = time.monotonic()
                if self._response_started_at is not None:
                    diagnostics.metric(
                        "realtime.response.ttfa",
                        round((self._response_first_audio_at -
                               self._response_started_at) * 1000, 1),
                        unit="ms", response_seq=self._response_seq)
            self.tracker.append(event.get("item_id", ""),
                                pcm16_ms(decoded_bytes))
            if self._on_audio:
                self._on_audio(event.get("item_id", ""), delta)
        elif t == "response.done":
            await self._on_response_done(event)
        elif t == "input_audio_buffer.speech_started":
            await self._on_speech_started()
        elif t == "input_audio_buffer.speech_stopped":
            self.session.note_user_speech_stopped()
        elif t == "conversation.item.input_audio_transcription.completed":
            transcript = event.get("transcript", "")
            if not isinstance(transcript, str):
                self._protocol_issue(
                    "invalid_transcript", value_type=type(transcript).__name__)
                transcript = ""
            events.emit("user_text", text=transcript)
            self.session.handle_transcript(transcript)
        elif t in ("response.output_audio_transcript.done",
                   "response.audio_transcript.done"):  # GA sibling name
            # purely additive UI feed: what echoecho actually said out loud
            transcript = event.get("transcript", "")
            if not isinstance(transcript, str):
                self._protocol_issue(
                    "invalid_transcript", value_type=type(transcript).__name__)
                transcript = ""
            events.emit("assistant_text", text=transcript)
        elif t == "error":
            self._log("server error: %s" % json.dumps(event.get("error", {})))
            err = event.get("error") if isinstance(event.get("error"), dict) else {}
            diagnostics.error("realtime.server.error",
                              error_type=err.get("type"),
                              error_code=err.get("code"),
                              parameter=err.get("param"))

    async def _on_speech_started(self):
        """Barge-in: reset silence timer, flush local playback, tell the server
        exactly how much of the interrupted item the user actually heard."""
        self.session.note_user_speech_started()
        self._flush_playback()
        trunc = self.tracker.truncate()
        diagnostics.info("realtime.barge_in", truncated=trunc is not None,
                         audio_end_ms=trunc[1] if trunc else None)
        if trunc is not None:
            await self._send({"type": "conversation.item.truncate",
                              "item_id": trunc[0], "content_index": 0,
                              "audio_end_ms": trunc[1]})

    async def _on_response_done(self, event):
        self.session.note_assistant_response_done()
        response = event.get("response", {})
        if not isinstance(response, dict):
            self._protocol_issue(
                "invalid_response", value_type=type(response).__name__)
            response = {}
        response_ms = None
        if self._response_started_at is not None:
            response_ms = round(
                (time.monotonic() - self._response_started_at) * 1000, 1)
        if self._signing_off:
            self._sign_off_done = True
            diagnostics.info("realtime.signoff.response_done",
                             duration_ms=response_ms)
            return
        output = response.get("output", [])
        if not isinstance(output, list):
            self._protocol_issue(
                "invalid_response_output", value_type=type(output).__name__)
            output = []
        calls = [it for it in output if isinstance(it, dict)
                 and it.get("type") == "function_call"]
        diagnostics.info("realtime.response.finished",
                         response_seq=self._response_seq,
                         duration_ms=response_ms,
                         status=response.get("status"),
                         output_count=len(output),
                         tool_count=len(calls),
                         had_audio=self._response_first_audio_at is not None)
        for call in calls:
            name = call.get("name")
            safe_name = (name if isinstance(name, str) and name in TOOL_NAMES
                         else "unknown")
            raw = call.get("arguments") or "{}"
            try:
                if not isinstance(raw, str):
                    raise TypeError("tool arguments are not a string")
                args = json.loads(raw) if raw.strip() else {}
                if not isinstance(args, dict):
                    raise TypeError("tool arguments are not an object")
            except (TypeError, ValueError):
                # model-generated JSON can be malformed; never crash the loop,
                # and still ack below so the conversation isn't stuck waiting
                raw_bytes = (raw.encode("utf-8", "replace")
                             if isinstance(raw, str) else
                             type(raw).__name__.encode("ascii", "replace"))
                fingerprint = hashlib.sha256(
                    raw_bytes).hexdigest()[:12]
                self._log("malformed tool arguments for %s (%d bytes, %s)"
                          % (safe_name, len(raw_bytes), fingerprint))
                self._protocol_issue(
                    "tool_arguments_invalid", tool=safe_name,
                    argument_bytes=len(raw_bytes), fingerprint=fingerprint)
                args = {}
            events.emit("tool_call", name=name, args=args)
            try:
                result = self._tool_cb(name, args) if self._tool_cb else {}
            except Exception as exc:  # a handler bug must not kill the session
                self._log("tool handler %s failed: %s" % (safe_name, exc))
                diagnostics.exception("realtime.tool.handler_failed", exc=exc,
                                      tool=safe_name)
                result = {"error": str(exc)}
            # IMMEDIATE ack: dispatch_task etc. never block the voice turn
            await self._send({"type": "conversation.item.create",
                              "item": {"type": "function_call_output",
                                       "call_id": call.get("call_id"),
                                       "output": json.dumps(result)}})
            if name == "end_session":
                self.session.begin_ending("end_session_tool")
        if calls and self.session.state == ACTIVE:
            await self._send({"type": "response.create"})
            self.session.note_assistant_response_started()

    # -- per-loop bookkeeping: ending flow, reconnect stub, injection gate -----

    async def _tick(self):
        s = self.session
        if s.state == ENDING:
            if s.end_reason in ("silence_timeout", "forced"):
                await self._shutdown()  # no spoken sign-off
            elif not self._signing_off:
                self._signing_off = True
                await self._send(
                    {"type": "response.create",
                     "response": {"instructions": SIGN_OFF_INSTRUCTIONS}})
                diagnostics.info("realtime.signoff.started",
                                 reason=s.end_reason)
            else:
                self._sign_off_polls += 1
                if self._sign_off_done or self._sign_off_polls > self.sign_off_max_polls:
                    if not self._sign_off_done:
                        diagnostics.warning(
                            "realtime.signoff.timed_out",
                            polls=self._sign_off_polls,
                            max_polls=self.sign_off_max_polls)
                    await self._shutdown()
            return
        if s.state != ACTIVE:
            return
        await self._maybe_reconnect()
        pending = s.drain_injections()  # [] unless at a safe turn boundary
        if not pending:
            return
        interrupt = False
        for inj in pending:
            if inj.priority == "silent":
                continue  # task table only; surfaced via check_tasks
            text = inj.text
            if text.startswith("[task"):
                text += " Weave in naturally."
            await self._send(_system_item(text))
            events.emit("injection", text=inj.text, priority=inj.priority)
            interrupt = interrupt or inj.priority == "interrupt"
        if interrupt:
            await self._send({"type": "response.create"})
            s.note_assistant_response_started()
        diagnostics.info("realtime.injections.delivered",
                         count=len(pending), interrupt=interrupt,
                         silent_count=sum(1 for i in pending
                                          if i.priority == "silent"))

    async def _shutdown(self):
        self._done = True
        events.emit("session", event="closed",
                    detail=self.session.end_reason or "")
        try:
            await self.transport.close()
        except Exception as exc:
            diagnostics.exception("realtime.transport.close_failed", exc=exc)
        self._emit_summary()

    def _emit_summary(self):
        if self._summary_emitted:
            return
        self._summary_emitted = True
        diagnostics.info(
            "realtime.session.summary",
            reason=self.session.end_reason or "",
            duration_ms=round((time.monotonic() - self._run_started) * 1000, 1),
            reconnects=self._reconnects,
            sent_event_counts=dict(self._sent_counts),
            received_event_counts=dict(self._recv_counts),
            protocol_error_counts=dict(self._protocol_errors),
            sent_bytes=self._sent_bytes, received_bytes=self._recv_bytes,
            responses=self._response_seq)

    def _log(self, msg):
        self.out("[realtime] %s" % msg)
