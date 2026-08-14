#!/usr/bin/env python3
"""echoecho entrypoint: wires conversation port <-> orchestrator <-> workers.

Sandbox-ready modes: --script fixtures/smoke.txt (keyless scripted run) and
--text (bare REPL). --voice / --mic-check land in later PRs.
"""
import argparse
import asyncio
import os
import sys


def parse_args(argv=None):
    p = argparse.ArgumentParser(prog="echoecho", description="echoecho voice agent")
    p.add_argument("--text", action="store_true", help="text REPL mode (no audio)")
    p.add_argument("--voice", action="store_true", help="voice mode (Mac only, later PR)")
    p.add_argument("--fake-llm", action="store_true", help="use fixture LLM outputs")
    p.add_argument("--script", metavar="PATH", help="run a scripted keyless session")
    p.add_argument("--model", metavar="ID", help="Realtime model id override")
    p.add_argument("--no-viewer", action="store_true",
                   help="don't start the workspace live viewer")
    p.add_argument("--mic-check", action="store_true",
                   help="list audio devices + live RMS meter (Mac only)")
    p.add_argument("--input-device", metavar="SPEC",
                   help="mic device: index or name substring; overrides "
                        "ECHOECHO_INPUT_DEVICE ('' = system default)")
    p.add_argument("--output-device", metavar="SPEC",
                   help="speaker device: index or name substring; overrides "
                        "ECHOECHO_OUTPUT_DEVICE ('' = system default)")
    p.add_argument("--list-devices", action="store_true",
                   help="print an indexed audio device table and exit")
    p.add_argument("--no-record", action="store_true",
                   help="disable session recordings (same as ECHOECHO_RECORD=0)")
    p.add_argument("--recordings", action="store_true",
                   help="list saved session recordings and exit")
    return p.parse_args(argv)


def apply_device_args(args):
    """--input-device/--output-device flags override the env vars (voice_main
    reads devices from config so env and flags share one path)."""
    if args.input_device is not None:
        os.environ["ECHOECHO_INPUT_DEVICE"] = args.input_device
    if args.output_device is not None:
        os.environ["ECHOECHO_OUTPUT_DEVICE"] = args.output_device


def list_devices():
    """--list-devices: indexed device table with in/out capabilities and the
    current defaults, then exit. Polite pointer + nonzero on Linux."""
    try:
        import sounddevice as sd
    except ImportError:
        sys.exit("--list-devices needs sounddevice (Mac): "
                 "pip install -r requirements-mac.txt. No audio hardware "
                 "here? --text and --script fixtures/smoke.txt work keyless.")
    try:
        default_in, default_out = tuple(sd.default.device)
    except Exception:
        default_in = default_out = None
    print("idx   in  out  name")
    for idx, dev in enumerate(sd.query_devices()):
        marks = []
        if idx == default_in:
            marks.append("default in")
        if idx == default_out:
            marks.append("default out")
        print("%3d  %3d  %3d  %s%s"
              % (idx, dev.get("max_input_channels", 0),
                 dev.get("max_output_channels", 0), dev.get("name", "?"),
                 "   <- " + ", ".join(marks) if marks else ""))
    from echoecho_app import config
    print("\nPick with --input-device/--output-device (or ECHOECHO_INPUT_DEVICE/"
          "ECHOECHO_OUTPUT_DEVICE): an index or a case-insensitive name "
          "substring; empty = follow the system default. Devices re-resolve "
          "at every session start, so plug in AirPods and just say '%s'."
          % config.WAKE_PHRASE)


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


def list_recordings():
    """--recordings: table of saved session recordings + how to review them."""
    import json
    from echoecho_app import config

    root = config.recordings_dir()
    dirs = sorted(p for p in root.iterdir() if p.is_dir()) if root.is_dir() else []
    if not dirs:
        print("no recordings yet — every --voice session records itself into "
              "%s/ (--no-record or ECHOECHO_RECORD=0 to opt out; ECHOECHO_RECORD=1 "
              "records --text/--script runs too)" % root)
        return
    print("%-32s %7s %7s  %-18s %8s" % ("session", "audio", "turns",
                                        "end_reason", "size"))
    total = 0
    for d in dirs:
        size = sum(f.stat().st_size for f in d.iterdir() if f.is_file())
        total += size
        try:
            meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        except Exception:
            meta = None  # crashed mid-session: events.jsonl still there
        audio_s = max(meta.get("mic_s") or 0, meta.get("echoecho_s") or 0) if meta else 0
        print("%-32s %7s %7s  %-18s %7.1fM" % (
            d.name,
            "%d:%02d" % (int(audio_s) // 60, int(audio_s) % 60) if audio_s else "-",
            "%s/%s" % (meta.get("user_turns", 0),
                       meta.get("assistant_turns", 0)) if meta else "?",
            (meta.get("end_reason") or "?") if meta else "(incomplete)",
            size / 1e6))
    print("\n%d recording(s), %.1f MB in %s/. To review one: open its "
          "session.wav (left = you, right = echoecho) and read transcript.md; "
          "events.jsonl is the raw timeline." % (len(dirs), total / 1e6, root))


def make_tool_handler(orch, port):
    """Contract A: the 4 tools, backed by the orchestrator + workspace."""
    from echoecho_app import config
    from echoecho_app.bus import TaskRequest
    from echoecho_app.services import artifacts

    def handle(name, args):
        if name == "dispatch_task":
            task = orch.submit(TaskRequest(kind=args["kind"],
                                           instructions=args.get("instructions", ""),
                                           args=args.get("args", {})))
            return {"task_id": task.id, "status": "queued"}
        if name == "check_tasks":
            return {"tasks": orch.summaries(args.get("task_id"))}
        if name == "read_artifact":
            raw = args.get("name", "")
            try:
                path = artifacts.resolve(config.WORKSPACE_DIR, raw)
            except ValueError as exc:
                return {"name": raw, "error": str(exc)}
            if not path.is_file():
                return {"name": raw, "error": "no such artifact"}
            try:
                return {"name": raw, "content": path.read_text(encoding="utf-8")}
            except (UnicodeDecodeError, OSError):
                return {"name": raw, "error": "not a text file"}
        if name == "end_session":
            port.session.begin_ending("end_session_tool")
            return {"status": "ending"}
        return {"error": "unknown tool %r" % name}

    return handle


async def amain(args):
    from echoecho_app import config, recorder
    from echoecho_app.conversation.session import Session
    from echoecho_app.orchestrator.core import Orchestrator
    from echoecho_app.workers.base import load_all

    session = Session()
    mode = "script" if args.script else "text"
    if args.script:
        from echoecho_app.conversation.scripted import ScriptedAgent
        port = ScriptedAgent(args.script, session=session)
    else:
        from echoecho_app.conversation.textmode import TextRepl
        port = TextRepl(session=session)

    orch = Orchestrator(registry=load_all(), on_injection=port.inject,
                        fake_llm=config.echoecho_fake_llm())
    orch.rehydrate()  # v2: the task table survives restarts
    port.on_tool(make_tool_handler(orch, port))

    viewer = None
    if not args.script and not args.no_viewer:  # --text: live workspace viewer
        from echoecho_app.viewer.server import ViewerServer
        try:
            viewer = ViewerServer(
                config.WORKSPACE_DIR,
                port=int(os.environ.get("ECHOECHO_VIEWER_PORT", "8765"))).start()
            print("[viewer] serving workspace at %s" % viewer.url)
        except OSError as exc:
            print("[viewer] not started (%s)" % exc)

    # start recording only once setup can no longer raise: a failure above
    # must not leave a forever-"(incomplete)" recording dir behind
    if config.echoecho_record(mode):  # opt-in for text/script (ECHOECHO_RECORD=1)
        recorder.start(mode)
    orch_loop = asyncio.ensure_future(orch.run())
    try:
        await port.run()
        await orch.drain(timeout=2.0)  # let in-flight demo workers log
    finally:
        orch_loop.cancel()
        recorder.stop(end_reason=session.end_reason or "interrupted")
        if viewer:
            viewer.stop()


def start_session_audio(mic, audio, loop, send_event):
    """Wake -> ACTIVE device swap. The order is load-bearing:
    refresh_devices() re-inits PortAudio and must NEVER run while any stream
    is open, so the wake mic fully closes first; AudioIO.start() then
    resolves its device specs against the fresh list — hardware plugged in
    while echoecho was IDLE (AirPods!) is picked up with zero user action."""
    from echoecho_app.conversation.audio import refresh_devices
    mic.stop()
    refresh_devices()
    audio.start(loop, send_event)


def end_session_audio(mic, audio, detector):
    """ACTIVE -> IDLE swap back, same zero-open-streams rule: close the
    session streams, re-init PortAudio, reopen the wake mic (re-resolving its
    device spec), then re-arm the detector."""
    from echoecho_app.conversation.audio import refresh_devices
    audio.stop()
    refresh_devices()
    try:
        mic.reopen()
    except Exception as exc:  # mic vanished: daemon lives; enter still wakes
        print("[wake] mic reopen failed (%s) — press enter to wake" % exc)
    detector.resume()


async def voice_main(args):
    """Mac-only always-on daemon: wake loop -> Realtime session -> back to
    IDLE. The Vosk feed is paused while ACTIVE (Session's wake_pause hook) so
    echoecho saying "echo" can't self-trigger; enter/spacebar+enter is the manual
    wake override. Orchestrator + viewer persist across sessions."""
    import threading
    import time

    from echoecho_app import config, events, recorder
    from echoecho_app.conversation.audio import AudioIO
    from echoecho_app.conversation.realtime import (RealtimeClient,
                                                WebSocketTransport)
    from echoecho_app.conversation.session import Session
    from echoecho_app.orchestrator.core import Orchestrator
    from echoecho_app.wake.detector import WakeDetector
    from echoecho_app.wake.mic import WakeMic

    loop = asyncio.get_event_loop()
    detector = WakeDetector()
    mic = WakeMic(device=config.input_device()).start()
    manual_wake = threading.Event()

    def stdin_watcher():  # any line (enter, or spacebar+enter) forces a wake
        for _ in sys.stdin:
            manual_wake.set()
    threading.Thread(target=stdin_watcher, daemon=True).start()

    # wake re-arm (mic reopen with fresh devices + detector.resume) happens in
    # end_session_audio in the finally below — AFTER the session streams are
    # closed — not in the FSM hook, which fires while they're still open.
    session = Session(wake_pause=detector.suspend,
                      wake_resume=lambda: None)
    from echoecho_app.workers.base import load_all
    orch = Orchestrator(registry=load_all(), fake_llm=config.echoecho_fake_llm())
    # v2: rehydrate the task table; tasks the last run left mid-flight are
    # marked interrupted and announced on the first wake (collect_missed)
    # like any other result finished while echoecho was away
    orch.rehydrate()

    viewer = None
    if not args.no_viewer:
        from echoecho_app.viewer.server import ViewerServer
        try:
            viewer = ViewerServer(
                config.WORKSPACE_DIR,
                port=int(os.environ.get("ECHOECHO_VIEWER_PORT", "8765"))).start()
            print("[viewer] serving workspace at %s" % viewer.url)
        except OSError as exc:
            print("[viewer] not started (%s)" % exc)
    orch_loop = asyncio.ensure_future(orch.run())

    model = config.realtime_model()
    # collect_missed() draws from a persisted announcement watermark, so tasks
    # that finished while echoecho was asleep — including across a restart — are
    # each announced on the next wake exactly once
    print("[wake] listening for '%s' (or press enter to wake)"
          % config.WAKE_PHRASE)
    try:
        while True:
            # -- IDLE: pump mic chunks through the detector -----------------
            wake_via = "manual"
            if manual_wake.is_set():
                manual_wake.clear()
            else:
                chunk = await loop.run_in_executor(None, mic.read, 0.2)
                if chunk is not None and detector.detect(chunk):
                    wake_via = "voice"
                else:
                    if not manual_wake.is_set():
                        continue
                    manual_wake.clear()
            if config.echoecho_record("voice"):  # feedback loop: record each use
                recorder.start("voice")      # (before the wake emit: teed too)
            events.emit("wake", via=wake_via)
            # -- WAKE -> ACTIVE session --------------------------------------
            since = None  # "[since last session]" for tasks done while IDLE
            missed = orch.collect_missed()  # marks them announced (persisted)
            if missed:
                since = ("[since last session] Background tasks finished "
                         "while echoecho was asleep: " + " | ".join(missed) +
                         " Mention them naturally if relevant.")
            audio = AudioIO(input_device=config.input_device(),
                            output_device=config.output_device())
            client = RealtimeClient(
                WebSocketTransport(model), session=session,
                on_audio=audio.on_audio, flush_playback=audio.flush,
                transport_factory=lambda: WebSocketTransport(model),
                since_last_session=since)
            audio.tracker = client.tracker
            orch.on_injection = client.inject
            orch.live = True  # results injected now reach the live session
            client.on_tool(make_tool_handler(orch, client))
            # wake() resets this too, but only once the transport is up: a
            # crash before that must not stamp LAST session's reason into
            # this recording's meta.json
            session.end_reason = None
            fallback_reason = "interrupted"  # Ctrl-C / cancellation
            try:
                # close wake mic -> refresh PortAudio -> open session streams
                # with FRESH device resolution (hot-plug pickup, every wake)
                # Open audio with capture upload gated. Realtime must receive
                # session.update before any input_audio_buffer.append; 20 ms
                # callbacks can otherwise race a slow WebSocket handshake.
                start_session_audio(mic, audio, loop, send_event=None)
                await client.connect()
                audio.set_sender(client.send_input_audio)
                audio.play_chime("wake")
                await client.run()  # returns when the session is back to IDLE
            except Exception as exc:  # daemon never dies: back to wake loop
                print("[voice] session crashed (%s) — returning to IDLE" % exc)
                fallback_reason = "crash"  # e.g. died before the FSM woke
                if session.state == "ACTIVE":
                    session.begin_ending("crash")
                if session.state == "ENDING":
                    session.finish()
            finally:
                orch.live = False  # back to IDLE: results wait for next wake
                orch.on_injection = lambda inj: None  # results wait in table
                audio.muted_capture = True  # transport is closing: stop sends
                audio.play_chime("end")
                await asyncio.sleep(max(0.3, audio.pending_ms() / 1000.0))
                # session streams closed -> refresh -> wake mic reopens with
                # fresh device resolution -> detector re-armed
                end_session_audio(mic, audio, detector)
                # streams are closed: everything audible is on disk; finalize
                recorder.stop(end_reason=session.end_reason or fallback_reason)
                manual_wake.clear()  # enter pressed mid-session: no ghost wake
            print("[wake] session over (%s) — listening for '%s' again"
                  % (session.end_reason, config.WAKE_PHRASE))
    finally:
        mic.stop()
        recorder.stop(end_reason="shutdown")  # no-op unless mid-session
        orch_loop.cancel()
        if viewer:
            viewer.stop()


def main(argv=None):
    from echoecho_app import config
    config.load_env_local()  # .env.local secrets; real env vars win
    args = parse_args(argv)
    if args.model:
        os.environ["ECHOECHO_REALTIME_MODEL"] = args.model
    if args.fake_llm:
        os.environ["ECHOECHO_FAKE_LLM"] = "1"
    if args.text:
        os.environ["ECHOECHO_TEXT"] = "1"
    apply_device_args(args)  # flags beat ECHOECHO_INPUT_DEVICE/ECHOECHO_OUTPUT_DEVICE
    if args.no_record:
        os.environ["ECHOECHO_RECORD"] = "0"
    if args.recordings:
        list_recordings()
        return
    if args.list_devices:
        list_devices()
        return
    if args.mic_check:
        mic_check()
        return
    from echoecho_app import events
    events.reset(mode="voice" if args.voice else
                 ("script" if args.script else "text"))
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
    from echoecho_app import config
    if not (args.script or args.text or config.echoecho_text()):
        sys.exit("No mode selected: use --text, --script PATH, or set ECHOECHO_TEXT=1. "
                 "(Voice mode lands in later PRs.)")
    asyncio.run(amain(args))


if __name__ == "__main__":
    main()
