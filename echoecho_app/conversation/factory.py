"""Select the proper voice protocol; fall back only before any Live work runs."""
from echoecho_app import config, events
from echoecho_app.conversation.live import LiveClient, LiveTransport, LiveUnavailable
from echoecho_app.conversation.realtime import RealtimeClient, WebSocketTransport


async def connect_voice(model, session, audio, handler_factory, since=None):
    if model.startswith('gpt-live'):
        client = LiveClient(LiveTransport(model),session=session,on_audio=audio.on_audio,
                            flush_playback=audio.flush,pending_audio=audio.pending_ms,
                            since_last_session=since)
        client.on_tool(handler_factory(client))
        try:
            await client.connect()
            return client
        except LiveUnavailable:
            model = config.realtime_model()
            events.emit('session',event='fallback',detail='GPT-Live is unavailable for this account. Using %s.' % model)
    client = RealtimeClient(WebSocketTransport(model),session=session,
                            on_audio=audio.on_audio,flush_playback=audio.flush,
                            transport_factory=lambda:WebSocketTransport(model),
                            since_last_session=since)
    client.on_tool(handler_factory(client))
    try:
        await client.connect()
    except BaseException:
        await client.transport.close()
        raise
    return client
