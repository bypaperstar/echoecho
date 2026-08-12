#!/usr/bin/env python3
"""Echo entrypoint: wires conversation port <-> orchestrator <-> workers.

Sandbox-ready modes: --script fixtures/smoke.txt (keyless scripted run) and
--text (bare REPL). --voice / --mic-check land in later PRs.
"""
import argparse
import asyncio
import os
import sys


def parse_args(argv=None):
    p = argparse.ArgumentParser(prog="echo", description="Echo voice agent")
    p.add_argument("--text", action="store_true", help="text REPL mode (no audio)")
    p.add_argument("--voice", action="store_true", help="voice mode (Mac only, later PR)")
    p.add_argument("--fake-llm", action="store_true", help="use fixture LLM outputs")
    p.add_argument("--script", metavar="PATH", help="run a scripted keyless session")
    p.add_argument("--model", metavar="ID", help="Realtime model id override")
    p.add_argument("--no-viewer", action="store_true",
                   help="don't start the workspace live viewer")
    p.add_argument("--mic-check", action="store_true",
                   help="list audio devices + live RMS meter (Mac only)")
    return p.parse_args(argv)


def mic_check(seconds=5.0):
    """Step zero of Mac setup: device list + live RMS meter (catches the TCC
    all-zeros permission failure — see README runbook)."""
    try:
        import sounddevice as sd
    except ImportError:
        sys.exit("--mic-check needs sounddevice (Mac): "
                 "pip install -r requirements-mac.txt")
    import array
    import time
    print(sd.query_devices())
    print("\ndefault input: %s" % (sd.default.device,))
    print("speak now — live RMS for %.0fs (all zeros => TCC mic permission "
          "denied; see README):" % seconds)

    def cb(indata, frames, time_info, status):
        samples = array.array("h")
        samples.frombytes(bytes(indata))
        rms = (sum(s * s for s in samples) / max(1, len(samples))) ** 0.5
        bar = "#" * min(60, int(rms / 300))
        print("RMS %6d |%-60s|" % (int(rms), bar))

    with sd.RawInputStream(samplerate=16000, channels=1, dtype="int16",
                           blocksize=1600, callback=cb):
        time.sleep(seconds)


def make_tool_handler(orch, port):
    """Contract A: the 4 tools, backed by the orchestrator + workspace."""
    from echo_app import config
    from echo_app.bus import TaskRequest

    def handle(name, args):
        if name == "dispatch_task":
            task = orch.submit(TaskRequest(kind=args["kind"],
                                           instructions=args.get("instructions", ""),
                                           args=args.get("args", {})))
            return {"task_id": task.id, "status": "queued"}
        if name == "check_tasks":
            return {"tasks": orch.summaries(args.get("task_id"))}
        if name == "read_artifact":
            path = config.WORKSPACE_DIR / os.path.basename(args.get("name", ""))
            if not path.is_file():
                return {"name": args.get("name"), "error": "no such artifact"}
            return {"name": path.name, "content": path.read_text()}
        if name == "end_session":
            port.session.begin_ending("end_session_tool")
            return {"status": "ending"}
        return {"error": "unknown tool %r" % name}

    return handle


async def amain(args):
    from echo_app import config
    from echo_app.conversation.session import Session
    from echo_app.orchestrator.core import Orchestrator
    from echo_app.workers.base import load_all

    session = Session()
    if args.script:
        from echo_app.conversation.scripted import ScriptedAgent
        port = ScriptedAgent(args.script, session=session)
    else:
        from echo_app.conversation.textmode import TextRepl
        port = TextRepl(session=session)

    orch = Orchestrator(registry=load_all(), on_injection=port.inject,
                        fake_llm=config.echo_fake_llm())
    port.on_tool(make_tool_handler(orch, port))

    viewer = None
    if not args.script and not args.no_viewer:  # --text: live workspace viewer
        from echo_app.viewer.server import ViewerServer
        try:
            viewer = ViewerServer(
                config.WORKSPACE_DIR,
                port=int(os.environ.get("ECHO_VIEWER_PORT", "8765"))).start()
            print("[viewer] serving workspace at %s" % viewer.url)
        except OSError as exc:
            print("[viewer] not started (%s)" % exc)

    orch_loop = asyncio.ensure_future(orch.run())
    try:
        await port.run()
        await orch.drain(timeout=2.0)  # let in-flight demo workers log
    finally:
        orch_loop.cancel()
        if viewer:
            viewer.stop()


async def voice_main(args):
    """Mac-only always-on daemon: wake loop -> Realtime session -> back to
    IDLE. The Vosk feed is paused while ACTIVE (Session's wake_pause hook) so
    Echo saying "echo" can't self-trigger; enter/spacebar+enter is the manual
    wake override. Orchestrator + viewer persist across sessions."""
    import threading
    import time

    from echo_app import config
    from echo_app.conversation.audio import AudioIO
    from echo_app.conversation.realtime import (RealtimeClient,
                                                WebSocketTransport)
    from echo_app.conversation.session import Session
    from echo_app.orchestrator.core import Orchestrator
    from echo_app.wake.detector import WakeDetector
    from echo_app.wake.mic import WakeMic

    loop = asyncio.get_event_loop()
    detector = WakeDetector()
    mic = WakeMic().start()
    manual_wake = threading.Event()

    def stdin_watcher():  # any line (enter, or spacebar+enter) forces a wake
        for _ in sys.stdin:
            manual_wake.set()
    threading.Thread(target=stdin_watcher, daemon=True).start()

    session = Session(wake_pause=detector.suspend,
                      wake_resume=lambda: (mic.drain(), detector.resume()))
    from echo_app.workers.base import load_all
    orch = Orchestrator(registry=load_all(), fake_llm=config.echo_fake_llm())

    viewer = None
    if not args.no_viewer:
        from echo_app.viewer.server import ViewerServer
        try:
            viewer = ViewerServer(
                config.WORKSPACE_DIR,
                port=int(os.environ.get("ECHO_VIEWER_PORT", "8765"))).start()
            print("[viewer] serving workspace at %s" % viewer.url)
        except OSError as exc:
            print("[viewer] not started (%s)" % exc)
    orch_loop = asyncio.ensure_future(orch.run())

    model = config.realtime_model()
    idle_since = None  # set on session end: tasks finishing after it are "missed"
    print("[wake] listening for '%s' (or press enter to wake)"
          % config.WAKE_PHRASE)
    try:
        while True:
            # -- IDLE: pump mic chunks through the detector -----------------
            if manual_wake.is_set():
                manual_wake.clear()
            else:
                chunk = await loop.run_in_executor(None, mic.read, 0.2)
                if chunk is None or not detector.detect(chunk):
                    if not manual_wake.is_set():
                        continue
                    manual_wake.clear()
            # -- WAKE -> ACTIVE session --------------------------------------
            since = None  # "[since last session]" for tasks done while IDLE
            missed = orch.results_since(idle_since) if idle_since else []
            if missed:
                since = ("[since last session] Background tasks finished "
                         "while Echo was asleep: " + " | ".join(missed) +
                         " Mention them naturally if relevant.")
            audio = AudioIO()
            client = RealtimeClient(
                WebSocketTransport(model), session=session,
                on_audio=audio.on_audio, flush_playback=audio.flush,
                transport_factory=lambda: WebSocketTransport(model),
                since_last_session=since)
            audio.tracker = client.tracker
            orch.on_injection = client.inject
            client.on_tool(make_tool_handler(orch, client))
            audio.start(loop, lambda ev: client.transport.send(ev))
            audio.play_chime("wake")
            try:
                await client.run()  # returns when the session is back to IDLE
            except Exception as exc:  # daemon never dies: back to wake loop
                print("[voice] session crashed (%s) — returning to IDLE" % exc)
                if session.state == "ACTIVE":
                    session.begin_ending("crash")
                if session.state == "ENDING":
                    session.finish()
            finally:
                idle_since = time.time()
                orch.on_injection = lambda inj: None  # results wait in table
                audio.muted_capture = True  # transport is closing: stop sends
                audio.play_chime("end")
                await asyncio.sleep(max(0.3, audio.pending_ms() / 1000.0))
                audio.stop()
                manual_wake.clear()  # enter pressed mid-session: no ghost wake
            print("[wake] session over (%s) — listening for '%s' again"
                  % (session.end_reason, config.WAKE_PHRASE))
    finally:
        mic.stop()
        orch_loop.cancel()
        if viewer:
            viewer.stop()


def main(argv=None):
    from echo_app import config
    config.load_env_local()  # .env.local secrets; real env vars win
    args = parse_args(argv)
    if args.model:
        os.environ["ECHO_REALTIME_MODEL"] = args.model
    if args.fake_llm:
        os.environ["ECHO_FAKE_LLM"] = "1"
    if args.text:
        os.environ["ECHO_TEXT"] = "1"
    if args.mic_check:
        mic_check()
        return
    if args.voice:
        try:
            import sounddevice  # noqa: F401  (lazy: voice-mode-only dependency)
        except ImportError:
            sys.exit("--voice needs sounddevice (Mac only): "
                     "pip install -r requirements-mac.txt. "
                     "Try --text or --script fixtures/smoke.txt here.")
        if not os.environ.get("OPENAI_API_KEY"):
            sys.exit("--voice needs OPENAI_API_KEY set.")
        try:
            asyncio.run(voice_main(args))
        except KeyboardInterrupt:
            pass
        return
    from echo_app import config
    if not (args.script or args.text or config.echo_text()):
        sys.exit("No mode selected: use --text, --script PATH, or set ECHO_TEXT=1. "
                 "(Voice mode lands in later PRs.)")
    asyncio.run(amain(args))


if __name__ == "__main__":
    main()
