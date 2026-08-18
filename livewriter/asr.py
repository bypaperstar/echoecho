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

    def start(self):
        self._task = asyncio.get_event_loop().create_task(self._run())
        return self._task

    def feed_audio(self, pcm_bytes):
        if not self._closed:
            self._q.put_nowait(pcm_bytes)

    async def close(self):
        self._closed = True
        self._q.put_nowait(None)
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()

    # -- internals ----------------------------------------------------------
    async def _run(self):
        import websockets
        import websockets.exceptions
        attempt = 0
        while not self._closed and attempt < 4:
            try:
                url = "wss://api.openai.com/v1/realtime?intent=transcription"
                ws = await websockets.connect(
                    url,
                    additional_headers={"Authorization": "Bearer " + self.api_key},
                    max_size=None,
                )
            except Exception as e:
                attempt += 1
                self.on_status("asr connect failed (%d/3): %s" % (attempt, e))
                await asyncio.sleep(attempt)
                continue
            attempt = 0
            self.on_status("asr connected (%s)" % self.model)
            try:
                await ws.send(json.dumps(session_update(self.model)))
                sender = asyncio.get_event_loop().create_task(self._send_loop(ws))
                try:
                    await self._recv_loop(ws)
                finally:
                    sender.cancel()
            except websockets.exceptions.ConnectionClosed:
                pass
            except Exception as e:
                self.on_status("asr error: %s" % e)
            finally:
                try:
                    await ws.close()
                except Exception:
                    pass
            if not self._closed:
                attempt += 1
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
            ev = json.loads(raw)
            t = ev.get("type", "")
            now = time.monotonic()
            if t == "conversation.item.input_audio_transcription.delta":
                self.on_delta(ev.get("delta", ""), now)
            elif t == "conversation.item.input_audio_transcription.completed":
                # VAD-model path: .completed is authoritative for the segment,
                # but deltas already carried the text; nothing extra to do.
                pass
            elif t == "error":
                self.on_status("asr api error: %s" % json.dumps(ev.get("error", {}))[:200])
