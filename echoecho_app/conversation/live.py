"""GPT-Live full-duplex audio with Responses delegation to existing tools.

Live has its own protocol: no Realtime audio commits, response.done speech
boundary, or conversation.item.truncate. A startup failure never dispatches
work. Access-denied models can fall back to Realtime before capture uploads.
"""
import asyncio
import base64
import binascii
import json
import time
from collections import OrderedDict

from echoecho_app import config, diagnostics, events
from echoecho_app.conversation.port import ConversationPort, TOOL_NAMES
from echoecho_app.conversation.realtime import (PlaybackTracker, TransportClosed,
                                               WebSocketTransport, pcm16_ms)
from echoecho_app.conversation.session import ACTIVE, ENDING, Session
from echoecho_app.conversation.textmode import build_tools

LIVE_PROMPT = """You are echoecho, a calm, capable assistant sharing a Mac workspace with the user.
Keep replies brief and conversational. Never read paths, task IDs or Markdown aloud.
Backchannel policy: Use occasional short acknowledgments without competing with the user.
Interruption policy: Yield when interrupted and listen to the latest correction.
Delegation policy:
Backend tools: create files and run tasks in the shared workspace and Mac VM,
check task progress, read saved files, and end the conversation.
Delegate when the user asks for work, saved content, task status, a correction,
or to end the conversation. Delegate before answering anything requiring those tools.
Do not delegate greetings or questions you can answer from verified conversation context.
Only say work is finished after the backend confirms it. Queued work is still in progress.
Continue listening while background work runs. Ask one short question when necessary.
"""


class LiveUnavailable(Exception):
    """The account cannot use this model; safe to try the other voice engine."""


class LiveProtocolError(Exception):
    pass


class LiveTransport(WebSocketTransport):
    def __init__(self, model='gpt-live-1', api_key=None):
        super().__init__(model, api_key)
        self.url = 'wss://api.openai.com/v1/live/sessions'


def build_session_start(model='gpt-live-1'):
    tools = [dict(tool, strict=False) for tool in build_tools()]
    return {'type': 'session.start', 'session': {
        'model': model, 'instructions': LIVE_PROMPT,
        'audio': {'format': {'type': 'audio/pcm', 'rate': 24000},
                  'output': {'voice': 'marin'}},
        'delegation': {'type': 'responses', 'responses': {
            'model': config.backend_model(), 'instructions': config.system_prompt(),
            'tools': tools, 'tool_choice': 'auto', 'parallel_tool_calls': False,
        }},
    }}


def access_error(error):
    code = str(error.get('code', '')) if isinstance(error, dict) else ''
    return code in ('model_not_found', 'model_not_available', 'permission_denied',
                    'access_denied', 'unsupported_model')


class LiveClient(ConversationPort):
    def __init__(self, transport, session=None, on_audio=None,
                 flush_playback=None, pending_audio=None, since_last_session=None,
                 poll_interval=0.25, startup_timeout=20, close_timeout=15):
        self.transport = transport
        self.session = session or Session()
        self.tracker = PlaybackTracker()
        self._on_audio = on_audio
        self._flush_playback = flush_playback or (lambda: None)
        self._pending_audio = pending_audio or (lambda: 0)
        self.since_last_session = since_last_session
        self.poll_interval = poll_interval
        self.startup_timeout = startup_timeout
        self.close_timeout = close_timeout
        self._tool_cb = None
        self._connected = False
        self._closed = False
        self._closing = False
        self._close_at = None
        self._captions = {'user': '', 'assistant': ''}
        self._caption_at = {'user': 0.0, 'assistant': 0.0}
        self._calls = OrderedDict()
        self._pending_calls = {}
        self.usage_seconds = 0.0
        self.finalized = False

    def on_tool(self, cb):
        self._tool_cb = cb

    def inject(self, injection):
        self.session.queue_injection(injection)

    async def end(self):
        self.session.begin_ending('forced')

    async def connect(self):
        if self._connected:
            return
        try:
            connect = getattr(self.transport, 'connect', None)
            if connect:
                await connect()
            await self.transport.send(build_session_start(self.transport.model))
            deadline = time.monotonic() + self.startup_timeout
            while True:
                ev = await asyncio.wait_for(self.transport.recv(), max(0.01, deadline-time.monotonic()))
                if not isinstance(ev, dict):
                    raise LiveProtocolError('Invalid Live startup response')
                if ev.get('type') == 'session.started':
                    break
                if ev.get('type') == 'error':
                    error = ev.get('error') if isinstance(ev.get('error'), dict) else {}
                    if access_error(error):
                        raise LiveUnavailable('GPT-Live is unavailable for this API account')
                    raise LiveProtocolError('GPT-Live startup rejected: '+str(error.get('code','unknown')))
                if ev.get('type') == 'session.closed':
                    raise LiveProtocolError('GPT-Live closed before startup')
            self._connected = True
            self.session.wake()
            events.emit('session', event='connected', model=self.transport.model)
            diagnostics.info('live.session.started', model=self.transport.model,
                             backend_model=config.backend_model())
            if self.since_last_session:
                await self._append('thinking', self.since_last_session)
        except BaseException as exc:
            await self.transport.close()
            status = getattr(getattr(exc, 'response', None), 'status_code', None)
            if status in (403, 404):
                raise LiveUnavailable('GPT-Live is unavailable for this API account') from exc
            raise

    async def send_input_audio(self, event):
        if not self._connected or self._closing:
            return
        await self.transport.send({'type':'session.input_audio.append',
                                   'audio': event['audio']})

    async def _append(self, kind, text):
        # Conservative UTF-8 byte bound stays below the 500-token append limit,
        # including non-English text; full task data remains in the backend.
        content = ''
        for char in str(text):
            if len((content + char).encode('utf-8')) > 400:
                await self.transport.send({'type':'session.'+kind+'.append',
                                           'delegation_id':None, 'content':content})
                content = ''
            content += char
        if content:
            await self.transport.send({'type':'session.'+kind+'.append',
                                       'delegation_id':None, 'content':content})

    def _flush_caption(self, who):
        text = self._captions[who].strip()
        self._captions[who] = ''
        if text:
            events.emit(who+'_text', text=text)
            if who == 'user':
                self.session.handle_transcript(text)

    async def _handle(self, ev):
        if not isinstance(ev, dict):
            raise LiveProtocolError('Invalid Live event')
        kind = ev.get('type')
        if kind in ('session.input_transcript.delta', 'session.output_transcript.delta'):
            who = 'user' if kind.startswith('session.input') else 'assistant'
            delta = ev.get('delta', '')
            if not isinstance(delta, str):
                return
            # Captions are not interruption events. Live permits overlap and
            # decides when to yield; flushing on every caller fragment would
            # cut off acknowledgments during ordinary full-duplex speech.
            self._captions[who] += delta
            self._caption_at[who] = time.monotonic()
            if len(self._captions[who]) > 8000:
                self._flush_caption(who)
            self.session.note_activity()
        elif kind == 'session.output_audio.delta':
            delta = ev.get('delta', '')
            try:
                raw = base64.b64decode(delta, validate=True)
            except (ValueError, TypeError, binascii.Error):
                diagnostics.warning('live.audio.invalid')
                return
            if len(raw) % 2:
                diagnostics.warning('live.audio.invalid')
                return
            self.tracker.append('live', pcm16_ms(len(raw)))
            self.session.note_activity()
            if self._on_audio:
                self._on_audio('live', delta)
        elif kind == 'response.event':
            nested = ev.get('event') or {}
            if not isinstance(nested, dict):
                return
            if nested.get('type') == 'response.output_item.done':
                call = nested.get('item') or {}
                if isinstance(call, dict) and call.get('type') == 'function_call':
                    self._pending_calls.setdefault(ev.get('delegation_id'),[]).append(call)
            elif nested.get('type') == 'response.completed':
                calls = self._pending_calls.pop(ev.get('delegation_id'),[])
                if self._closing or self.session.state == ENDING:
                    return  # closing rejects new work and function outputs
                for call in calls:
                    if self.session.state == ENDING:
                        break
                    await self._tool(call)
                if calls and self.session.state == ACTIVE:
                    await self.transport.send({'type':'response.create'})
            elif nested.get('type') in ('response.failed', 'response.incomplete'):
                self._pending_calls.pop(ev.get('delegation_id'),None)
                events.emit('session', event='error', detail='The voice backend could not finish. Please try again.')
                diagnostics.warning('live.backend.failed', outcome=nested.get('type'))
        elif kind == 'session.usage.updated':
            self._update_usage(ev)
        elif kind == 'session.closed':
            self._update_usage(ev)
            self.finalized = True
            self._closed = True
        elif kind == 'error':
            error = ev.get('error') if isinstance(ev.get('error'), dict) else {}
            diagnostics.error('live.server.error', error_code=error.get('code'),
                              error_type=error.get('type'))
            events.emit('session', event='error', detail='Voice connection error. End and retry the conversation.')
            self.session.begin_ending('protocol_error')

    def _update_usage(self, ev):
        usage = ev.get('usage')
        seconds = usage.get('seconds') if isinstance(usage, dict) else None
        if isinstance(seconds, (int,float)) and seconds >= 0:
            self.usage_seconds = max(self.usage_seconds, seconds)

    async def _tool(self, call):
        call_id = call.get('call_id')
        if not isinstance(call_id, str) or not call_id:
            raise LiveProtocolError('Live function call has no call ID')
        if call_id in self._calls:
            result = self._calls[call_id]  # replay the ack without repeating writes
        else:
            name = call.get('name')
            try:
                args = json.loads(call.get('arguments') or '{}')
                if name not in TOOL_NAMES or not isinstance(args,dict):
                    raise ValueError('Invalid function call')
            except (TypeError, ValueError):
                result = {'error':'Invalid function arguments'}
            else:
                events.emit('tool_call', name=name, args=args)
                try:
                    result = self._tool_cb(name,args) if self._tool_cb else {'error':'Tools unavailable'}
                except Exception as exc:
                    diagnostics.exception('live.tool.failed',exc=exc)
                    result = {'error':'Tool failed; the action was not confirmed'}
            self._calls[call_id] = result
            if len(self._calls)>256:
                self._calls.popitem(last=False)
        await self.transport.send({'type':'response.item.create','item':{
            'type':'function_call_output', 'call_id':call_id, 'output':json.dumps(result)}})

    async def run(self):
        try:
            await self.connect()
            while not self._closed:
                try:
                    ev = await asyncio.wait_for(self.transport.recv(),self.poll_interval)
                except asyncio.TimeoutError:
                    ev = None
                if ev is not None:
                    await self._handle(ev)
                now = time.monotonic()
                for who in self._captions:
                    if self._captions[who] and now-self._caption_at[who] > 0.8:
                        self._flush_caption(who)
                if self._pending_audio() > 0:
                    self.session.note_activity()
                self.session.check_silence()
                if self.session.state == ENDING and not self._closing and not self._closed:
                    self._closing = True
                    self._close_at = now
                    await self.transport.send({'type':'session.close'})
                if self._closing and now-self._close_at > self.close_timeout:
                    diagnostics.warning('live.close.unconfirmed')
                    break
                if not self._closing:
                    # Live can listen while speaking. No response.done gate exists.
                    self.session.allow_live_injections()
                    for inj in self.session.drain_injections():
                        if inj.priority == 'silent':
                            continue
                        await self._append('commentary' if inj.priority=='interrupt' else 'thinking', inj.text)
                        events.emit('injection',text=inj.text,priority=inj.priority)
        except TransportClosed:
            events.emit('session',event='error',detail='Connection lost. Say echo echo to reconnect.')
            self.session.begin_ending('transport_closed')
        finally:
            for who in self._captions:
                self._flush_caption(who)
            await self.transport.close()
            self._closed = True
            if self.session.state == ACTIVE:
                self.session.begin_ending('closed')
            self.session.finish()
            events.emit('session',event='closed',detail=self.session.end_reason or '')
            diagnostics.info('live.session.closed',finalized=self.finalized,
                             usage_seconds=self.usage_seconds)
