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
import json
import os
import time
from pathlib import Path

from echo_app import config
from echo_app.conversation.port import ConversationPort
from echo_app.conversation.session import ACTIVE, ENDING, Session
from echo_app.conversation.textmode import SYSTEM_PROMPT, TOOLS

VOICE_PROMPT = SYSTEM_PROMPT + (
    " You are speaking out loud: keep each turn to a sentence or two.")
SIGN_OFF_INSTRUCTIONS = ("The session is ending. Say a brief, friendly "
                         "one-sentence goodbye.")
RECONNECT_SECS = 55 * 60  # 60-min API cap; summary+reconnect a bit before it


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
        self._ws = await websockets.connect(
            self.url, additional_headers={"Authorization": "Bearer " + self.api_key},
            max_size=None)

    async def send(self, event):
        await self._ws.send(json.dumps(event))

    async def recv(self):
        import websockets
        try:
            return json.loads(await self._ws.recv())
        except websockets.exceptions.ConnectionClosed:
            raise TransportClosed()

    async def close(self):
        if self._ws is not None:
            await self._ws.close()


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


def build_session_update(instructions=VOICE_PROMPT):
    """GA session.update: instructions, the exact 4 Contract-A tools, pcm16
    24 kHz in/out, semantic VAD with barge-in, input transcription on."""
    return {"type": "session.update", "session": {
        "type": "realtime",
        "instructions": instructions,
        "output_modalities": ["audio"],
        "audio": {
            "input": {
                "format": {"type": "audio/pcm", "rate": 24000},
                "transcription": {"model": "gpt-4o-mini-transcribe"},
                "turn_detection": {"type": "semantic_vad",
                                   "interrupt_response": True},
            },
            "output": {"format": {"type": "audio/pcm", "rate": 24000}},
        },
        "tools": TOOLS,
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
                 instructions=VOICE_PROMPT):
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
        self._tool_cb = None
        self._done = False
        self._signing_off = False
        self._sign_off_done = False
        self._sign_off_polls = 0
        self._connected_at = self.clock()

    # -- ConversationPort -----------------------------------------------------

    def on_tool(self, cb):
        self._tool_cb = cb

    def inject(self, injection):
        self.session.queue_injection(injection)  # delivered by the gate in _tick

    async def end(self):
        self.session.begin_ending("forced")

    async def run(self):
        await self._connect()
        while not self._done:
            try:
                event = await asyncio.wait_for(self.transport.recv(),
                                               self.poll_interval)
            except asyncio.TimeoutError:
                event = None
            except TransportClosed:
                break
            if event is not None:
                await self._handle_event(event)
            if self.session.check_silence():
                self._log("silence timeout — closing (sign-off skipped)")
            await self._tick()
        if self.session.state == ACTIVE:
            self.session.begin_ending("transport_closed")
        if self.session.state == ENDING:
            self.session.finish()

    # -- connection ------------------------------------------------------------

    async def _connect(self, reconnect=False):
        connect = getattr(self.transport, "connect", None)
        if connect is not None:
            await connect()
        await self.transport.send(build_session_update(self.instructions))
        if reconnect:
            # summary stub (cookbook pattern): a real rolling summary is PR 6+
            await self.transport.send(_system_item(
                "[reconnected] The previous connection neared the 60-minute "
                "cap and was refreshed; continue where the conversation left off."))
        self._connected_at = self.clock()
        self.session.wake()  # no-op if the FSM is already ACTIVE

    async def _maybe_reconnect(self):
        if (self._transport_factory is None
                or self.clock() - self._connected_at < self.max_session_secs):
            return
        self._log("55-min session cap — best-effort summary + reconnect")
        try:
            await self.transport.close()
        except Exception:
            pass
        self.transport = self._transport_factory()
        await self._connect(reconnect=True)

    # -- server events -----------------------------------------------------------

    async def _handle_event(self, event):
        t = event.get("type", "")
        if t == "session.created":
            self._log("session %s created" % event.get("session", {}).get("id"))
        elif t == "response.created":
            self.session.note_assistant_response_started()
        elif t == "response.output_audio.delta":
            delta = event.get("delta", "")
            self.tracker.append(event.get("item_id", ""),
                                pcm16_ms(len(base64.b64decode(delta))))
            if self._on_audio:
                self._on_audio(event.get("item_id", ""), delta)
        elif t == "response.done":
            await self._on_response_done(event)
        elif t == "input_audio_buffer.speech_started":
            await self._on_speech_started()
        elif t == "input_audio_buffer.speech_stopped":
            self.session.note_user_speech_stopped()
        elif t == "conversation.item.input_audio_transcription.completed":
            self.session.handle_transcript(event.get("transcript", ""))
        elif t == "error":
            self._log("server error: %s" % json.dumps(event.get("error", {})))

    async def _on_speech_started(self):
        """Barge-in: reset silence timer, flush local playback, tell the server
        exactly how much of the interrupted item the user actually heard."""
        self.session.note_user_speech_started()
        self._flush_playback()
        trunc = self.tracker.truncate()
        if trunc is not None:
            await self.transport.send({"type": "conversation.item.truncate",
                                       "item_id": trunc[0], "content_index": 0,
                                       "audio_end_ms": trunc[1]})

    async def _on_response_done(self, event):
        self.session.note_assistant_response_done()
        if self._signing_off:
            self._sign_off_done = True
            return
        calls = [it for it in event.get("response", {}).get("output", [])
                 if it.get("type") == "function_call"]
        for call in calls:
            raw = call.get("arguments") or "{}"
            args = json.loads(raw) if raw.strip() else {}
            result = self._tool_cb(call["name"], args) if self._tool_cb else {}
            # IMMEDIATE ack: dispatch_task etc. never block the voice turn
            await self.transport.send({"type": "conversation.item.create",
                                       "item": {"type": "function_call_output",
                                                "call_id": call.get("call_id"),
                                                "output": json.dumps(result)}})
            if call["name"] == "end_session":
                self.session.begin_ending("end_session_tool")
        if calls and self.session.state == ACTIVE:
            await self.transport.send({"type": "response.create"})
            self.session.note_assistant_response_started()

    # -- per-loop bookkeeping: ending flow, reconnect stub, injection gate -----

    async def _tick(self):
        s = self.session
        if s.state == ENDING:
            if s.end_reason in ("silence_timeout", "forced"):
                await self._shutdown()  # no spoken sign-off
            elif not self._signing_off:
                self._signing_off = True
                await self.transport.send(
                    {"type": "response.create",
                     "response": {"instructions": SIGN_OFF_INSTRUCTIONS}})
            else:
                self._sign_off_polls += 1
                if self._sign_off_done or self._sign_off_polls > self.sign_off_max_polls:
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
            await self.transport.send(_system_item(text))
            interrupt = interrupt or inj.priority == "interrupt"
        if interrupt:
            await self.transport.send({"type": "response.create"})
            s.note_assistant_response_started()

    async def _shutdown(self):
        self._done = True
        try:
            await self.transport.close()
        except Exception:
            pass

    def _log(self, msg):
        self.out("[realtime] %s" % msg)
