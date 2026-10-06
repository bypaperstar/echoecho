"""Thread-safe daemon status and a bounded command bridge to the asyncio loop.

HTTP threads read only snapshots. Commands execute on the owning loop, where
PortAudio, sessions and the orchestrator are used; no audio reinitialization
can race a request. The viewer authenticates callers before reaching here.
"""
import asyncio
from concurrent.futures import TimeoutError as CommandTimeout
import threading
import time

from echoecho_app import config
from echoecho_app.bus import TaskRequest


class DaemonControl:
    def __init__(self, loop, session, mic, orchestrator, manual_wake):
        self.loop = loop
        self.session = session
        self.mic = mic
        self.orchestrator = orchestrator
        self.manual_wake = manual_wake
        self.client = None
        self.audio = None
        self.phase = 'ready'
        self.paused = False
        self.error = ''
        self.model = config.voice_model()
        self.devices = []
        self._lock = threading.Lock()
        self._snapshot = {}
        self.publish()

    def publish(self):
        source = self.audio if self.audio is not None else self.mic
        telemetry = source.telemetry()
        last_capture = telemetry.get('last_capture_at', 0)
        age = max(0,time.monotonic()-last_capture) if last_capture else None
        all_tasks = list(self.orchestrator.tasks.values())
        visible_tasks = [t for t in all_tasks if t.status in ('queued', 'running')]
        visible_tasks += [t for t in all_tasks if t.status not in ('queued', 'running')][-30:]
        tasks = [{
            'id':t.id, 'title':t.title or t.kind, 'kind':t.kind,
            'status':t.status, 'progress':t.progress or '',
            'say':t.result.say if t.result else '',
            'createdAt':t.created_at, 'finishedAt':t.finished_at,
        } for t in visible_tasks]
        snapshot = {
            'phase':'paused' if self.paused and self.session.state=='IDLE' else self.phase, 'session':self.session.state,
            'model':self.model, 'error':self.error,
            'inputDevice':getattr(source,'input_label', '') or config.input_device() or 'System default',
            'outputDevice':getattr(source,'output_label', '') or config.output_device() or 'System default',
            'captureActive':bool(telemetry.get('stream_active')),
            'captureAge':age, 'inputLevel':telemetry.get('input_level',0),
            'devices':self.devices, 'tasks':tasks,
            'recordSessions':config.echoecho_record('voice'),
            'configuredInputDevice':config.input_device(),
            'configuredOutputDevice':config.output_device(),
            'configuredVoiceModel':config.voice_model(),
        }
        with self._lock:
            self._snapshot = snapshot

    def snapshot(self):
        with self._lock:
            return dict(self._snapshot)

    def command(self, data):
        if not self.loop.is_running():
            raise ValueError('Echoecho is shutting down')
        future = asyncio.run_coroutine_threadsafe(self._command(data), self.loop)
        try:
            return future.result(timeout=2)
        except CommandTimeout:
            future.cancel()
            raise ValueError('Echoecho is busy; try again')

    async def _command(self, data):
        action = data.get('action')
        if action == 'configure':
            from echoecho_app import preferences
            preferences.apply()
            self.mic.device = config.input_device()
            if self.session.state == 'IDLE' and not self.paused:
                self.mic.reopen()
            self.refresh_devices()
            result = {'ok':True}
        elif action == 'pause':
            self.paused = True
            if self.client:
                await self.client.end()
            elif self.session.state == 'IDLE':
                self.mic.stop()
                self.mic.drain()
            result = {'ok':True}
        elif action == 'resume':
            if self.session.state != 'IDLE':
                raise ValueError('Wait for the conversation to finish')
            if self.paused:
                self._resume_mic()
            result = {'ok':True}
        elif action == 'wake':
            if self.paused:
                self._resume_mic()
            if self.session.state == 'IDLE' and self.phase != 'connecting':
                self.manual_wake.set()
            result = {'ok':True}
        elif action == 'end':
            if self.client:
                await self.client.end()
            result = {'ok':True}
        elif action == 'submit':
            text = data.get('text')
            if not isinstance(text,str) or not text.strip() or len(text)>12000:
                raise ValueError('Enter a task of 1–12,000 characters')
            task = self.orchestrator.submit(TaskRequest(kind='agent.run', instructions=text.strip()))
            result = {'ok':True,'task_id':task.id}
        elif action == 'cancel':
            task_id = data.get('task_id')
            if not isinstance(task_id,str):
                raise ValueError('Invalid task')
            result = {'ok':self.orchestrator.cancel(task_id)}
        else:
            raise ValueError('Unknown command')
        self.publish()
        return result

    def _resume_mic(self):
        from echoecho_app.conversation.audio import refresh_devices
        if self.session.state != 'IDLE' or self.phase == 'connecting':
            raise ValueError('Wait for the conversation to finish')
        if not self.mic.portaudio_close_safe:
            raise ValueError('Restart voice before reopening the microphone')
        refresh_devices()  # paused: there are no native streams to race
        self.mic.drain()
        self.mic.start()
        self.paused = False

    def refresh_devices(self):
        # Read only: never terminate/reinitialize PortAudio from a status poll.
        try:
            import sounddevice as sd
            self.devices = [{'name':d['name'],'input':d['max_input_channels']>0,
                             'output':d['max_output_channels']>0}
                            for d in sd.query_devices()]
        except Exception:
            self.devices = []
