import asyncio
import base64
import json

import pytest

from echoecho_app import config, events
from echoecho_app.bus import Injection
from echoecho_app.conversation.live import (LiveClient, LiveProtocolError,
    LiveUnavailable, LiveTransport, build_session_start)
from echoecho_app.conversation.realtime import FakeTransport
from echoecho_app.conversation.session import Session
from echoecho_app.workers.base import load_all


def transport(items):
    tr = FakeTransport(items)
    tr.model = 'gpt-live-1'
    return tr


def test_live_uses_its_own_endpoint_and_responses_tools(monkeypatch):
    load_all()
    monkeypatch.setenv('ECHOECHO_BACKEND_MODEL','gpt-6-luna')
    request = build_session_start()
    assert LiveTransport().url == 'wss://api.openai.com/v1/live/sessions'
    assert request['type'] == 'session.start'
    cfg = request['session']
    assert cfg['audio']['format']['rate'] == 24000
    assert cfg['delegation']['responses']['model'] == 'gpt-6-luna'
    assert {t['name'] for t in cfg['delegation']['responses']['tools']} == {
        'dispatch_task','check_tasks','read_artifact','end_session'}
    assert all(t['strict'] is False for t in cfg['delegation']['responses']['tools'])
    assert 'tools' not in cfg


def test_no_audio_before_startup_ack_and_access_denial_is_fallback_safe():
    tr = transport([{'type':'error','error':{'code':'model_not_found'}}])
    client = LiveClient(tr)
    async def go():
        await client.send_input_audio({'audio':'AAAA'})
        with pytest.raises(LiveUnavailable):
            await client.connect()
    asyncio.run(go())
    assert [e['type'] for e in tr.sent] == ['session.start']
    assert tr.closed
    assert client.session.state == 'IDLE'


def test_bad_schema_does_not_masquerade_as_missing_account_access():
    tr = transport([{'type':'error','error':{'code':'invalid_request_error'}}])
    with pytest.raises(LiveProtocolError):
        asyncio.run(LiveClient(tr).connect())
    assert tr.closed


def test_audio_and_caption_lanes_are_independent_and_usage_is_cumulative(tmp_path, monkeypatch):
    monkeypatch.setattr(config,'WORKSPACE_DIR',tmp_path)
    events.reset(mode='test')
    audio = []
    tr = transport([{'type':'session.started'}])
    client = LiveClient(tr,on_audio=lambda item,delta:audio.append(base64.b64decode(delta)))
    async def go():
        await client.connect()
        await client.send_input_audio({'audio':'AAAA'})
        await client._handle({'type':'session.input_transcript.delta','delta':'please '})
        await client._handle({'type':'session.output_transcript.delta','delta':'On it.'})
        await client._handle({'type':'session.input_transcript.delta','delta':'write a note'})
        await client._handle({'type':'session.output_audio.delta','delta':base64.b64encode(b'\0\0'*240).decode()})
        for n in [12,15,14]:
            await client._handle({'type':'session.usage.updated','usage':{'seconds':n}})
        client._flush_caption('user'); client._flush_caption('assistant')
    asyncio.run(go())
    assert client.usage_seconds == 15
    assert len(audio[0])==480
    assert tr.sent[-1]['type']=='session.input_audio.append'
    records=[json.loads(x) for x in (tmp_path/events.FEED_NAME).read_text().splitlines()]
    assert [r['text'] for r in records if r['type']=='user_text']==['please write a note']
    assert [r['text'] for r in records if r['type']=='assistant_text']==['On it.']


def test_function_calls_are_collected_from_items_not_empty_completed_output():
    tr = transport([])
    client = LiveClient(tr)
    seen=[]
    client.session.wake()
    client.on_tool(lambda name,args:seen.append((name,args)) or {'task_id':'t1','status':'queued'})
    call={'type':'function_call','name':'dispatch_task','call_id':'call1',
          'arguments':json.dumps({'kind':'agent.run','instructions':'write a note'})}
    async def go():
        for nested in [{'type':'response.output_item.done','item':call},
                       {'type':'response.completed','response':{'output':[]}}]:
            await client._handle({'type':'response.event','delegation_id':'d1','event':nested})
        # Duplicate delivered item must ack the same call without another write.
        await client._tool(call)
    asyncio.run(go())
    assert len(seen)==1
    assert [e['type'] for e in tr.sent]==['response.item.create','response.create','response.item.create']
    assert tr.sent[0]['item']['call_id']=='call1'


def test_overlapping_delegations_do_not_mix_function_results():
    tr=transport([]); client=LiveClient(tr); client.session.wake()
    seen=[]; client.on_tool(lambda name,args:seen.append(args['name']) or {})
    async def go():
        for did in ['a','b']:
            await client._handle({'type':'response.event','delegation_id':did,'event':{
                'type':'response.output_item.done','item':{'type':'function_call',
                'name':'read_artifact','call_id':did,'arguments':json.dumps({'name':did})}}})
        await client._handle({'type':'response.event','delegation_id':'b','event':{'type':'response.completed'}})
        assert seen==['b']
        await client._handle({'type':'response.event','delegation_id':'a','event':{'type':'response.completed'}})
    asyncio.run(go()); assert seen==['b','a']


def test_pending_backend_calls_are_not_dispatched_after_user_ends_session():
    tr=transport([]);client=LiveClient(tr);client.session.wake();seen=[]
    client.on_tool(lambda *args:seen.append(args) or {})
    async def go():
        await client._handle({'type':'response.event','delegation_id':'d','event':{
            'type':'response.output_item.done','item':{'type':'function_call','name':'dispatch_task',
            'call_id':'late','arguments':'{}'}}})
        await client.end()
        await client._handle({'type':'response.event','delegation_id':'d','event':{'type':'response.completed'}})
    asyncio.run(go())
    assert not seen and not tr.sent


def test_invalid_arguments_are_rejected_without_dispatch():
    tr=transport([]); client=LiveClient(tr); called=[]
    client.on_tool(lambda name,args:called.append(name))
    asyncio.run(client._tool({'name':'dispatch_task','arguments':'[1]','call_id':'broken'}))
    assert not called
    assert 'error' in json.loads(tr.sent[0]['item']['output'])


def test_forced_close_waits_for_final_usage_and_never_sends_realtime_commands():
    tr=transport([{'type':'session.started'}])
    send = tr.send
    async def finish_on_close(event):
        await send(event)
        if event['type'] == 'session.close':
            tr.events.append({'type':'session.closed','usage':{'seconds':3}})
    tr.send = finish_on_close
    client=LiveClient(tr,poll_interval=.001)
    async def go():
        await client.connect()
        await client.end()
        await client.run()
    asyncio.run(go())
    assert client.finalized
    assert client.usage_seconds==3
    assert client.session.state=='IDLE'
    assert tr.sent_types() == ['session.start', 'session.close']
    assert not any(e['type'].startswith('conversation.') for e in tr.sent)


def test_live_injections_do_not_reset_inactivity_clock():
    now=[0.0]; session=Session(clock=lambda:now[0],silence_timeout=5)
    session.wake(); session.queue_injection(Injection('ready','ambient'))
    now[0]=4
    session.allow_live_injections()
    assert session.drain_injections()
    now[0]=6
    assert session.check_silence()


def test_malformed_audio_is_skipped():
    played=[]; client=LiveClient(transport([]),on_audio=lambda *x:played.append(x))
    async def go():
        await client._handle({'type':'session.output_audio.delta','delta':'!!!'})
        await client._handle({'type':'session.output_audio.delta','delta':'AA=='})
    asyncio.run(go()); assert not played


def test_full_duplex_captions_do_not_cancel_simultaneous_assistant_speech():
    flushed=[]
    client=LiveClient(transport([]),pending_audio=lambda:100,
                      flush_playback=lambda:flushed.append(True))
    asyncio.run(client._handle({'type':'session.input_transcript.delta','delta':'yes'}))
    assert not flushed


def test_long_context_is_sent_in_complete_utf8_chunks_without_lost_results():
    tr=transport([]); client=LiveClient(tr); text='Finished: café 🙂 ' * 100
    asyncio.run(client._append('thinking',text))
    assert ''.join(e['content'] for e in tr.sent)==text
    assert all(len(e['content'].encode('utf-8'))<=400 for e in tr.sent)


@pytest.mark.parametrize('code,falls_back',[('model_not_found',True),('invalid_request_error',False)])
def test_factory_falls_back_only_for_account_access_failures(monkeypatch,code,falls_back):
    from types import SimpleNamespace
    from echoecho_app.conversation import factory
    live=transport([{'type':'error','error':{'code':code}}])
    realtime=transport([]); realtime.model='gpt-realtime-2.1'
    monkeypatch.setattr(factory,'LiveTransport',lambda model:live)
    made=[]
    monkeypatch.setattr(factory,'WebSocketTransport',lambda model:made.append(model) or realtime)
    monkeypatch.setenv('ECHOECHO_REALTIME_MODEL','gpt-realtime-2.1')
    audio=SimpleNamespace(on_audio=lambda *a:None,flush=lambda:None,pending_ms=lambda:0)
    session=Session()
    async def go():
        connected=factory.connect_voice('gpt-live-1',session,audio,lambda client:lambda *a:{})
        if falls_back:
            client=await connected
            assert client.transport is realtime
            assert session.state=='ACTIVE'
        else:
            with pytest.raises(LiveProtocolError):await connected
            assert session.state=='IDLE'
    asyncio.run(go())
    assert live.closed
    assert bool(made)==falls_back
    assert live.sent_types()==['session.start']
