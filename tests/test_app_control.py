import asyncio
import http.client
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from echoecho_app import config, preferences
from echoecho_app.bus import TaskRequest, TaskResult
from echoecho_app.control import DaemonControl
from echoecho_app.orchestrator.core import Orchestrator
from echoecho_app.viewer.server import ViewerServer
from echoecho_app.wake.mic import WakeMic


def test_wake_queue_is_bounded_and_retains_recent_audio():
    mic=WakeMic()
    for i in range(60): mic._callback(i.to_bytes(2,'little')*100,100,None,None)
    assert mic.chunks.qsize()==30
    assert mic.telemetry()['dropped_chunks']==30
    assert mic.read()[0]==30
    assert mic.telemetry()['last_capture_at']>0


def test_secret_free_preferences_override_env_and_reject_untrusted_fields(tmp_path,monkeypatch):
    file=tmp_path/'preferences.json'; monkeypatch.setenv('ECHOECHO_PREFERENCES_FILE',str(file))
    file.write_text(json.dumps({'inputDevice':'MacBook Pro Microphone','voiceModel':'gpt-live-1','recordSessions':False}))
    monkeypatch.setenv('ECHOECHO_INPUT_DEVICE','other'); monkeypatch.setenv('ECHOECHO_VOICE_MODEL','other'); monkeypatch.setenv('ECHOECHO_RECORD','1')
    preferences.apply()
    assert config.input_device()=='MacBook Pro Microphone'
    assert config.voice_model()=='gpt-live-1'
    assert not config.echoecho_record('voice')
    for bad in [{'OPENAI_API_KEY':'secret'},{'recordSessions':'false'},{'voiceModel':'invented'},{'inputDevice':'\ncommand'}]:
        with pytest.raises(ValueError):preferences.validate(bad)


def request(server,path,method='GET',body=None,token=None,extra=None):
    conn=http.client.HTTPConnection(*server.httpd.server_address[:2],timeout=3)
    headers={'Content-Type':'application/json',**(extra or {})}
    if token:headers['Authorization']='Bearer '+token
    try:
        conn.request(method,path,body=json.dumps(body) if body is not None else None,headers=headers)
        response=conn.getresponse();return response.status,json.loads(response.read())
    finally:conn.close()


def test_status_and_commands_are_token_gated_and_cross_origin_writes_denied(tmp_path,monkeypatch):
    monkeypatch.setenv('ECHOECHO_VIEWER_TOKEN_FILE',str(tmp_path/'token'))
    called=[]
    server=ViewerServer(tmp_path,port=0).start()
    server.control=SimpleNamespace(snapshot=lambda:{'phase':'ready'},command=lambda d:called.append(d) or {'ok':True})
    try:
        assert request(server,'/status')[0]==403
        assert request(server,'/status',token=server.token)==(200,{'phase':'ready'})
        assert request(server,'/transcript',extra={'Host':'attacker.example'})[0]==403
        assert request(server,'/control','POST',{'action':'wake'})[0]==403
        assert request(server,'/control','POST',{'action':'wake'},server.token,{'Origin':'https://example.com'})[0]==403
        assert not called
        assert request(server,'/control','POST',{'action':'wake'},server.token)[0]==200
        assert called==[{'action':'wake'}]
    finally:server.stop()


def test_orchestrator_cancels_running_worker_and_preserves_queued_order(tmp_path):
    cleaned=[]
    async def worker(task,ctx):
        try:await asyncio.sleep(30)
        finally:cleaned.append(task.id)
    async def go():
        orch=Orchestrator({'work':worker},workspace=tmp_path,log_path=tmp_path/'tasks.jsonl')
        runner=asyncio.create_task(orch.run())
        for _ in range(6):orch.submit(TaskRequest('work','do it'))
        await asyncio.sleep(.03)
        assert sum(t.status=='running' for t in orch.tasks.values())==3
        assert orch.cancel('t2')
        await asyncio.sleep(.03)
        assert 't2' in cleaned
        assert orch.tasks['t2'].status=='cancelled'
        assert orch.tasks['t4'].status=='running'
        assert orch.cancel('t6')
        runner.cancel();await asyncio.gather(runner,return_exceptions=True)
        restored=Orchestrator({'work':worker},workspace=tmp_path,log_path=tmp_path/'tasks.jsonl')
        restored.rehydrate()
        assert restored.tasks['t2'].status=='cancelled'
        assert restored.tasks['t1'].status=='error'
        assert restored.tasks['t1'].result.data['error']=='Interrupted when Echoecho closed. Partial work remains in the workspace.'
        assert restored.collect_missed()  # interrupted work is reported on next wake
        assert not orch._running
    asyncio.run(go())


def test_worker_reported_error_is_not_a_completed_task(tmp_path):
    async def worker(task,ctx):return TaskResult(say='failed',data={'error':'timeout'})
    async def go():
        orch=Orchestrator({'work':worker},workspace=tmp_path,log_path=tmp_path/'tasks.jsonl')
        runner=asyncio.create_task(orch.run());task=orch.submit(TaskRequest('work'))
        assert await orch.drain()
        runner.cancel();await asyncio.gather(runner,return_exceptions=True)
        assert task.status=='error'
    asyncio.run(go())


def test_real_shared_vm_cleanup_stops_it_without_deleting_disk(monkeypatch,tmp_path):
    from echoecho_app.services.vm import LumeVM,discard
    guest=LumeVM(workspace=tmp_path);calls=[]
    async def lume(*args):calls.append(args);return 0,''
    monkeypatch.setattr(guest,'_lume',lume)
    asyncio.run(discard(guest))
    assert calls==[('stop','echoecho-vm')]


def test_status_keeps_old_running_tasks_visible_and_resume_guards_failed_native_close(tmp_path):
    from echoecho_app.bus import Task
    from echoecho_app.conversation.session import Session
    mic=WakeMic()
    tasks={'old':Task('old',TaskRequest('work'),status='running')}
    for i in range(40):
        tasks[str(i)]=Task(str(i),TaskRequest('work'),status='done')
    control=DaemonControl(None,Session(),mic,SimpleNamespace(tasks=tasks),threading.Event())
    assert len(control.snapshot()['tasks'])==31
    assert control.snapshot()['tasks'][0]['id']=='old'
    mic._stream_close_error=RuntimeError('native close failed')
    with pytest.raises(ValueError,match='Restart voice'):
        control._resume_mic()


def test_http_commands_use_owner_loop_and_paused_listening_accepts_tasks(tmp_path,monkeypatch):
    from echoecho_app.conversation import audio
    from echoecho_app.conversation.session import Session
    import time
    loop=asyncio.new_event_loop();ready=threading.Event();operations=[]
    def own(operation):
        assert asyncio.get_running_loop() is loop
        operations.append(operation)
    class Mic:
        active=True
        portaudio_close_safe=True
        def stop(self):own('stop');self.active=False
        def start(self):own('start');self.active=True
        def drain(self):own('drain')
        def telemetry(self):return {'stream_active':self.active,'last_capture_at':time.monotonic()}
    mic=Mic()
    orch=Orchestrator({},workspace=tmp_path,log_path=tmp_path/'tasks.jsonl')
    control=DaemonControl(loop,Session(),mic,orch,threading.Event())
    monkeypatch.setattr(audio,'refresh_devices',lambda:own('refresh'))
    monkeypatch.setenv('ECHOECHO_VIEWER_TOKEN_FILE',str(tmp_path/'token'))
    def run_loop():
        asyncio.set_event_loop(loop);loop.call_soon(ready.set);loop.run_forever()
    thread=threading.Thread(target=run_loop);thread.start();assert ready.wait(2)
    server=ViewerServer(tmp_path,port=0).start();server.control=control
    try:
        assert request(server,'/control','POST',{'action':'pause'},server.token)==(200,{'ok':True})
        assert control.snapshot()['phase']=='paused'
        assert not mic.active
        status,result=request(server,'/control','POST',{'action':'submit','text':'write a note'},server.token)
        assert status==200 and result['task_id']=='t1'
        assert orch.tasks['t1'].status=='queued'
        assert not mic.active
        assert request(server,'/control','POST',{'action':'resume'},server.token)==(200,{'ok':True})
        assert mic.active
        assert operations==['stop','drain','refresh','drain','start']
    finally:
        server.stop();loop.call_soon_threadsafe(loop.stop);thread.join(2);loop.close()
