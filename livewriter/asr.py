"""OpenAI realtime transcription bridge.

Connects to wss://api.openai.com/v1/realtime?intent=transcription (GA, Bearer
auth, no beta header — same surface echoecho_app/conversation/realtime.py uses
for realtime sessions) and relays 24 kHz mono pcm16 audio in, transcription
deltas out.

Two model families behave differently (probed live, 2026-08-18):
  * gpt-live-transcribe      — rejects turn_detection; streams word-by-word
                               deltas DURING speech; never emits .completed.
                               The Segmenter owns utterance boundaries.
  * gpt-4o(-mini)-transcribe — needs server_vad; deltas burst only after each
                               VAD segment commits, then .completed fires.

Both are supported so the page/harness can A/B them; default is
gpt-live-transcribe because the ghost tail is the product.

websockets is imported lazily (repo convention: keyless Linux sandbox tests
must import every module with stdlib only).
"""

import asyncio
import base64
import json
import os
import time
from collections import Counter

from echoecho_app import diagnostics

LIVE_MODEL_DEFAULT = "gpt-live-transcribe"


def _needs_vad(model):
    return "live" not in model


def session_update(model):
    audio_in = {
        "format": {"type": "audio/pcm", "rate": 24000},
        "noise_reduction": {"type": "near_field"},
        "transcription": {"model": model},
    }
    if _needs_vad(model):
        audio_in["turn_detection"] = {
            "type": "server_vad",
            "threshold": 0.5,
            "prefix_padding_ms": 300,
            "silence_duration_ms": 300,
        }
    else:
        audio_in["turn_detection"] = None
    return {"type": "session.update", "session": {"type": "transcription", "audio": {"input": audio_in}}}


class Transcriber(object):
    """Owns one upstream WS. feed_audio() is non-blocking; callbacks fire on
    the event loop. Reconnects with linear backoff if the transport drops
    mid-session (the 60-min realtime cap, network blips)."""

    def __init__(self, model=None, api_key=None, on_delta=None, on_status=None):
        self.model = model or os.environ.get("LIVEWRITER_ASR_MODEL", LIVE_MODEL_DEFAULT)
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self.on_delta = on_delta or (lambda text, now: None)
        self.on_status = on_status or (lambda s: None)
        self._q = asyncio.Queue()
        self._task = None
        self._closed = False
        self._audio_chunks = 0
        self._audio_bytes = 0
        self._queue_high_water = 0
        self._events = 0
        self._deltas = 0
        self._reconnects = 0
        self._protocol_errors = Counter()
        self._started = time.monotonic()

    def _protocol_issue(self, kind, **fields):
        self._protocol_errors[kind] += 1
        count = self._protocol_errors[kind]
        if count <= 3 or count & (count - 1) == 0:
            diagnostics.warning(
                "livewriter.asr.protocol_%s" % kind,
                occurrences=count, **fields)

    def start(self):
        self._task = asyncio.get_event_loop().create_task(self._run())
        diagnostics.info("livewriter.asr.started", model=self.model)
        return self._task

    def feed_audio(self, pcm_bytes):
        if not self._closed:
            self._q.put_nowait(pcm_bytes)
            self._audio_chunks += 1
            self._audio_bytes += len(pcm_bytes)
            self._queue_high_water = max(self._queue_high_water,
                                          self._q.qsize())

    async def close(self):
        self._closed = True
        self._q.put_nowait(None)
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except asyncio.TimeoutError:
                diagnostics.warning("livewriter.asr.close_timed_out")
                self._task.cancel()
                await asyncio.gather(self._task, return_exceptions=True)
            except asyncio.CancelledError:
                diagnostics.warning("livewriter.asr.close_cancelled")
                self._task.cancel()
                await asyncio.gather(self._task, return_exceptions=True)
            except Exception as exc:
                diagnostics.exception("livewriter.asr.close_failed", exc=exc)
        diagnostics.info(
            "livewriter.asr.finished", model=self.model,
            duration_ms=round((time.monotonic() - self._started) * 1000, 1),
            audio_chunks=self._audio_chunks, audio_bytes=self._audio_bytes,
            queue_high_water=self._queue_high_water,
            received_events=self._events, deltas=self._deltas,
            reconnects=self._reconnects,
            protocol_error_counts=dict(self._protocol_errors))

    # -- internals ----------------------------------------------------------
    async def _run(self):
        import websockets
        import websockets.exceptions
        attempt = 0
        while not self._closed and attempt < 4:
            connect_started = time.monotonic()
            try:
                url = "wss://api.openai.com/v1/realtime?intent=transcription"
                ws = await websockets.connect(
                    url,
                    additional_headers={"Authorization": "Bearer " + self.api_key},
                    max_size=None,
                )
            except Exception as e:
                attempt += 1
                self._reconnects += 1
                diagnostics.exception(
                    "livewriter.asr.connect_failed", exc=e, model=self.model,
                    attempt=attempt,
                    duration_ms=round(
                        (time.monotonic() - connect_started) * 1000, 1))
                self.on_status("asr connect failed (%d/3): %s" % (attempt, e))
                await asyncio.sleep(attempt)
                continue
            attempt = 0
            diagnostics.info(
                "livewriter.asr.connected", model=self.model,
                duration_ms=round(
                    (time.monotonic() - connect_started) * 1000, 1))
            self.on_status("asr connected (%s)" % self.model)
            try:
                await ws.send(json.dumps(session_update(self.model)))
                sender = asyncio.get_event_loop().create_task(self._send_loop(ws))
                try:
                    await self._recv_loop(ws)
                finally:
                    sender.cancel()
                    await asyncio.gather(sender, return_exceptions=True)
            except websockets.exceptions.ConnectionClosed:
                diagnostics.warning("livewriter.asr.transport_lost")
            except Exception as e:
                diagnostics.exception("livewriter.asr.failed", exc=e,
                                      model=self.model)
                self.on_status("asr error: %s" % e)
            finally:
                try:
                    await ws.close()
                except Exception:
                    pass
            if not self._closed:
                attempt += 1
                self._reconnects += 1
                self.on_status("asr transport lost — reconnect %d/3" % attempt)
        self.on_status("asr closed")

    async def _send_loop(self, ws):
        while True:
            pcm = await self._q.get()
            if pcm is None:
                return
            await ws.send(json.dumps({
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(pcm).decode("ascii"),
            }))

    async def _recv_loop(self, ws):
        import websockets.exceptions
        while not self._closed:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=90)
            except asyncio.TimeoutError:
                continue  # idle mic is fine; keep listening
            try:
                ev = json.loads(raw)
            except (TypeError, ValueError) as exc:
                try:
                    message_bytes = len(raw)
                except Exception:
                    message_bytes = None
                self._protocol_issue(
                    "invalid_json", error_type=exc.__class__.__name__,
                    message_bytes=message_bytes)
                continue
            if not isinstance(ev, dict):
                self._protocol_issue(
                    "invalid_event", value_type=type(ev).__name__)
                continue
            self._events += 1
            t = ev.get("type", "")
            now = time.monotonic()
            if t == "conversation.item.input_audio_transcription.delta":
                delta = ev.get("delta", "")
                if not isinstance(delta, str):
                    self._protocol_issue(
                        "invalid_delta", value_type=type(delta).__name__)
                    continue
                self._deltas += 1
                self.on_delta(delta, now)
            elif t == "conversation.item.input_audio_transcription.completed":
                # VAD-model path: .completed is authoritative for the segment,
                # but deltas already carried the text; nothing extra to do.
                pass
            elif t == "error":
                err = ev.get("error") if isinstance(ev.get("error"), dict) else {}
                diagnostics.error("livewriter.asr.api_error",
                                  error_type=err.get("type"),
                                  error_code=err.get("code"))
                self.on_status("asr api error: %s" % json.dumps(ev.get("error", {}))[:200])
