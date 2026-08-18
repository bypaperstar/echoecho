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
    p.add_argument("--diagnostics-dir", metavar="PATH",
                   help="write structured run diagnostics here (default: "
                        "~/.echoecho/diagnostics)")
    p.add_argument("--no-diagnostics", action="store_true",
                   help="disable structured diagnostics for this run")
    return p.parse_args(argv)


def apply_early_diagnostic_args(argv=None):
    """Honor diagnostic privacy/location flags even when argparse fails.

    An invalid later option still benefits from a startup-error trace, but it
    must never bypass an explicit --no-diagnostics or write that trace to the
    wrong directory. This deliberately recognizes only exact diagnostics
    controls; argparse remains the authority for validation and error text.
    """
    tokens = list(sys.argv[1:] if argv is None else argv)
    for index, token in enumerate(tokens):
        if token == "--no-diagnostics":
            os.environ["ECHOECHO_DIAGNOSTICS"] = "0"
        elif token == "--diagnostics-dir":
            if index + 1 < len(tokens):
                value = tokens[index + 1]
                if isinstance(value, str) and value and not value.startswith("-"):
                    os.environ["ECHOECHO_DIAGNOSTICS_DIR"] = value
        elif isinstance(token, str) and token.startswith("--diagnostics-dir="):
            value = token.split("=", 1)[1]
            if value:
                os.environ["ECHOECHO_DIAGNOSTICS_DIR"] = value


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
    import json
    import time

    from echoecho_app import config, diagnostics, events
    from echoecho_app.bus import TaskRequest
    from echoecho_app.conversation.port import TOOL_NAMES
    from echoecho_app.services import artifacts

    def handle(name, args):
        started = time.monotonic()
        safe_args = args if isinstance(args, dict) else {}
        safe_tool = name if name in TOOL_NAMES else "unknown"
        expected_args = {
            "dispatch_task": {"kind", "instructions", "args"},
            "check_tasks": {"task_id"},
            "read_artifact": {"name"},
            "end_session": set(),
        }.get(safe_tool, set())
        safe_arg_keys = sorted(
            key for key in safe_args if isinstance(key, str)
            and key in expected_args)
        diagnostics.info(
            "tool.call.started", tool=safe_tool,
            arg_keys=safe_arg_keys, arg_count=len(safe_args),
            unknown_arg_count=max(0, len(safe_args) - len(safe_arg_keys)))
        try:
            result = _handle(name, safe_args)
        except Exception as exc:
            diagnostics.exception(
                "tool.call.failed", exc=exc, tool=safe_tool,
                duration_ms=round((time.monotonic() - started) * 1000, 1))
            raise
        # every port emits tool_call before calling us; pairing the result
        # into the feed makes recordings reviewable (was the artifact there?)
        serialized_result = json.dumps(result, default=str)
        events.emit("tool_result", name=name,
                    result=serialized_result[:500])
        diagnostics.info(
            "tool.call.finished", tool=safe_tool,
            duration_ms=round((time.monotonic() - started) * 1000, 1),
            outcome="error" if isinstance(result, dict) and result.get("error")
            else "ok",
            result_keys=sorted(
                key for key in result if isinstance(key, str) and key in {
                    "task_id", "status", "tasks", "name", "content", "error"})
            if isinstance(result, dict) else [],
            result_size=len(serialized_result))
        return result

    def _handle(name, args):
        if name == "dispatch_task":
            task_args = dict(args.get("args") or {})
            instructions = args.get("instructions", "")
            instruction_source = "top_level" if instructions else "missing"
            if not instructions:
                # voice models sometimes nest instructions inside args
                # instead of the top-level field; honor them — otherwise
                # the worker falls back to its default and the result
                # looks like echoecho ignored what was asked
                instructions = task_args.pop("instructions", "")
                if instructions:
                    instruction_source = "nested_args"
            diagnostics.info(
                "tool.dispatch.instructions_resolved",
                source=instruction_source,
                instruction_chars=(len(instructions)
                                   if isinstance(instructions, str) else None),
                task_arg_count=len(task_args))
            task = orch.submit(TaskRequest(kind=args["kind"],
                                           instructions=instructions,
                                           args=task_args))
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
    from echoecho_app import config, diagnostics, recorder
    from echoecho_app.conversation.session import Session
    from echoecho_app.orchestrator.core import Orchestrator
    from echoecho_app.workers.base import load_all

    mode = "script" if args.script else "text"
    diagnostics.install_asyncio(asyncio.get_running_loop())
    session_id = diagnostics.new_id("session")
    with diagnostics.context(session_id=session_id, mode=mode):
        with diagnostics.span("conversation.session", mode=mode):
            session = Session()
            if args.script:
                from echoecho_app.conversation.scripted import ScriptedAgent
                port = ScriptedAgent(args.script, session=session)
            else:
                from echoecho_app.conversation.textmode import TextRepl
                port = TextRepl(session=session)

            registry = load_all()
            diagnostics.info("workers.loaded", count=len(registry),
                             kinds=sorted(registry))
            orch = Orchestrator(registry=registry, on_injection=port.inject,
                                fake_llm=config.echoecho_fake_llm())
            interrupted = orch.rehydrate()  # v2: task table survives restarts
            diagnostics.info("orchestrator.rehydrated", task_count=len(orch.tasks),
                             interrupted_count=len(interrupted))
            port.on_tool(make_tool_handler(orch, port))

            viewer = None
            if not args.script and not args.no_viewer:  # --text live viewer
                from echoecho_app.viewer.server import ViewerServer
                try:
                    viewer = ViewerServer(
                        config.WORKSPACE_DIR,
                        port=int(os.environ.get(
                            "ECHOECHO_VIEWER_PORT", "8765"))).start()
                    diagnostics.info("viewer.started", port=viewer.httpd.server_port)
                    print("[viewer] serving workspace at %s" % viewer.url)
                except OSError as exc:
                    diagnostics.exception("viewer.start.failed", exc=exc)
                    print("[viewer] not started (%s)" % exc)

            # start recording only once setup can no longer raise: a failure
            # above must not leave a forever-incomplete recording directory
            if config.echoecho_record(mode):
                recorder.start(mode)
            orch_loop = asyncio.create_task(orch.run(), name="orchestrator")
            try:
                await port.run()
                drained = await orch.drain(timeout=2.0)
                diagnostics.info("orchestrator.drain", drained=drained,
                                 running_count=len(orch._running))
            finally:
                orch_loop.cancel()
                await asyncio.gather(orch_loop, return_exceptions=True)
                recorder.stop(end_reason=session.end_reason or "interrupted")
                if viewer:
                    viewer.stop()
                    diagnostics.info("viewer.stopped")


def start_session_audio(mic, audio, loop, send_event):
    """Wake -> ACTIVE device swap. The order is load-bearing:
    refresh_devices() re-inits PortAudio and must NEVER run while any stream
    is open, so the wake mic fully closes first; AudioIO.start() then
    resolves its device specs against the fresh list — hardware plugged in
    while echoecho was IDLE (AirPods!) is picked up with zero user action."""
    from echoecho_app import diagnostics
    from echoecho_app.conversation.audio import refresh_devices
    with diagnostics.span("audio.session.start"):
        mic.stop()
        refreshed = refresh_devices()
        diagnostics.info("audio.devices.refreshed", refreshed=refreshed,
                         phase="session_start")
        audio.start(loop, send_event)


def end_session_audio(mic, audio, detector):
    """ACTIVE -> IDLE swap back, same zero-open-streams rule: close the
    session streams, re-init PortAudio, reopen the wake mic (re-resolving its
    device spec), then re-arm the detector. Return whether that recovery was
    safe; any stream-close failure forbids further PortAudio operations."""
    from echoecho_app import diagnostics, recorder
    from echoecho_app.conversation.audio import refresh_devices
    streams_closed = True
    try:
        audio.stop()
    except Exception as exc:
        # A close failure means a native stream may still be open even though
        # Python references were cleared. PortAudio re-init in that state can
        # crash the process, so collect telemetry but do not touch it again.
        streams_closed = False
        diagnostics.exception("audio.session.stop.failed", exc=exc)
    pipeline_stats = audio.pipeline_telemetry()
    if pipeline_stats is not None:
        diagnostics.info("audio.pipeline.summary", **pipeline_stats)
        active_recording = recorder.active()
        if active_recording is not None:
            active_recording.note_audio_processing(
                pipeline_stats, audio.telemetry())
    wake_stream_closed = getattr(mic, "portaudio_close_safe", True)
    if not streams_closed or not wake_stream_closed:
        reason = ("stream_close_failed" if not streams_closed else
                  "wake_stream_close_failed")
        diagnostics.error(
            "audio.devices.refresh.skipped",
            phase="session_end", reason=reason)
        return False
    refreshed = refresh_devices()
    diagnostics.info("audio.devices.refreshed", refreshed=refreshed,
                     phase="session_end")
    try:
        mic.reopen()
    except Exception as exc:  # mic vanished: daemon lives; enter still wakes
        diagnostics.exception("wake.mic.reopen.failed", exc=exc)
        print("[wake] mic reopen failed (%s) — press enter to wake" % exc)
    try:
        detector.resume()
    except Exception as exc:
        diagnostics.exception("wake.detector.resume.failed", exc=exc)
    return True


def recover_session_audio(mic, audio, detector, transition_attempted):
    """Recover the wake path only after a WAKE -> ACTIVE handoff began.

    Session setup constructs several objects while the wake stream is still
    open.  A failure there must leave that healthy stream alone: refreshing
    PortAudio is safe only after ``start_session_audio`` attempted to close it.
    The caller sets ``transition_attempted`` immediately before that call, so
    partial handoff failures still take the conservative cleanup path.
    """
    from echoecho_app import diagnostics
    if not transition_attempted:
        diagnostics.info(
            "audio.session.recovery_skipped",
            reason="transition_not_attempted",
            audio_constructed=audio is not None)
        return True
    return end_session_audio(mic, audio, detector)


def start_tether_watchdog(pid=None, interval=2.0, on_dead=None):
    """Tie this daemon's life to the echoecho.app process.

    The app and the wake word live and die together: echoechoctl/the orb pass
    the app's pid as ECHOECHO_TETHER_PID, and when that process disappears —
    Cmd-Q or a force quit alike — the daemon must not keep the mic open. Poll
    (a non-child pid can't be waited on); on death send ourselves SIGINT so
    voice_main's finally runs, with a hard-exit backstop in case the loop is
    wedged mid-session. Returns the watcher thread, or None if untethered
    (e.g. a bare `python echoecho.py --voice` in a terminal).
    """
    import signal
    import threading
    import time
    from echoecho_app import diagnostics

    if pid is None:
        try:
            pid = int(os.environ.get("ECHOECHO_TETHER_PID", "") or 0)
        except ValueError:
            pid = 0
    if not pid:
        diagnostics.info("tether.disabled")
        return None

    # echoechoctl starts us as `( nohup ... & )` from a non-interactive shell,
    # which hands SIGINT down as SIG_IGN — and CPython leaves ignored signals
    # ignored, so the watchdog's self-SIGINT below would be discarded and
    # every managed shutdown would take the hard-exit backstop instead of the
    # clean finally path. Restore the KeyboardInterrupt handler.
    if signal.getsignal(signal.SIGINT) == signal.SIG_IGN:
        signal.signal(signal.SIGINT, signal.default_int_handler)

    def default_on_dead():
        # Arm the unconditional exit before any console/diagnostic I/O. A
        # hung filesystem or diagnostics lock must not keep the mic daemon
        # alive after its owning app has disappeared.
        def hard_exit():
            time.sleep(10)
            os._exit(0)

        threading.Thread(
            target=hard_exit, daemon=True, name="tether-hard-exit").start()
        os.kill(os.getpid(), signal.SIGINT)
        diagnostics.warning("tether.parent_gone", parent_pid=pid)
        print("[tether] app (pid %d) is gone — shutting down with it" % pid)

    def watch():
        while True:
            time.sleep(interval)
            try:
                os.kill(pid, 0)  # existence probe, no signal delivered
            except ProcessLookupError:  # not OSError: EPERM means it EXISTS
                (on_dead or default_on_dead)()
                return

    t = threading.Thread(target=watch, daemon=True, name="tether")
    t.start()
    diagnostics.info("tether.started", parent_pid=pid,
                     poll_interval_s=interval)
    print("[tether] tied to app pid %d — daemon exits when it does" % pid)
    return t


async def voice_main(args):
    """Mac-only always-on daemon: wake loop -> Realtime session -> back to
    IDLE. The Vosk feed is paused while ACTIVE (Session's wake_pause hook) so
    echoecho saying "echo" can't self-trigger; enter/spacebar+enter is the manual
    wake override. Orchestrator + viewer persist across sessions."""
    import threading
    import time

    from echoecho_app import config, diagnostics, events, recorder
    from echoecho_app.conversation.audio import AudioIO
    from echoecho_app.conversation.realtime import (RealtimeClient,
                                                WebSocketTransport)
    from echoecho_app.conversation.session import Session
    from echoecho_app.orchestrator.core import Orchestrator
    from echoecho_app.wake.detector import WakeDetector
    from echoecho_app.wake.mic import WakeMic

    loop = asyncio.get_running_loop()
    diagnostics.install_asyncio(loop)
    with diagnostics.span("voice.setup"):
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
    registry = load_all()
    diagnostics.info("workers.loaded", count=len(registry),
                     kinds=sorted(registry))
    orch = Orchestrator(registry=registry, fake_llm=config.echoecho_fake_llm())
    # v2: rehydrate the task table; tasks the last run left mid-flight are
    # marked interrupted and announced on the first wake (collect_missed)
    # like any other result finished while echoecho was away
    interrupted = orch.rehydrate()
    diagnostics.info("orchestrator.rehydrated", task_count=len(orch.tasks),
                     interrupted_count=len(interrupted))

    viewer = None
    if not args.no_viewer:
        from echoecho_app.viewer.server import ViewerServer
        try:
            viewer = ViewerServer(
                config.WORKSPACE_DIR,
                port=int(os.environ.get("ECHOECHO_VIEWER_PORT", "8765"))).start()
            diagnostics.info("viewer.started", port=viewer.httpd.server_port)
            print("[viewer] serving workspace at %s" % viewer.url)
        except OSError as exc:
            diagnostics.exception("viewer.start.failed", exc=exc)
            print("[viewer] not started (%s)" % exc)
    orch_loop = asyncio.create_task(orch.run(), name="orchestrator")

    model = config.realtime_model()
    # collect_missed() draws from a persisted announcement watermark, so tasks
    # that finished while echoecho was asleep — including across a restart — are
    # each announced on the next wake exactly once
    print("[wake] listening for '%s' (or press enter to wake)"
          % config.WAKE_PHRASE)
    diagnostics.info("wake.listening", model=model, viewer=viewer is not None)
    try:
        heartbeat_s = max(5.0, float(os.environ.get(
            "ECHOECHO_WAKE_HEARTBEAT_S", "60")))
    except (TypeError, ValueError):
        heartbeat_s = 60.0
    heartbeat_at = time.monotonic() + heartbeat_s
    heartbeat_mic = mic.telemetry()
    heartbeat_detector = detector.telemetry()
    try:
        while True:
            # -- IDLE: pump mic chunks through the detector -----------------
            wake_via = "manual"
            if manual_wake.is_set():
                manual_wake.clear()
            else:
                chunk = await loop.run_in_executor(None, mic.read, 0.2)
                detected = chunk is not None and detector.detect(chunk)
                now = time.monotonic()
                if now >= heartbeat_at:
                    current_mic = mic.telemetry()
                    current_detector = detector.telemetry()
                    fields = {
                        "interval_ms": round((now - heartbeat_at + heartbeat_s) * 1000, 1),
                        "callback_delta": current_mic["callback_count"] - heartbeat_mic["callback_count"],
                        "captured_bytes_delta": current_mic["captured_bytes"] - heartbeat_mic["captured_bytes"],
                        "status_callback_delta": current_mic["status_callbacks"] - heartbeat_mic["status_callbacks"],
                        "read_timeout_delta": current_mic["read_timeouts"] - heartbeat_mic["read_timeouts"],
                        "detector_chunk_delta": current_detector["chunk_count"] - heartbeat_detector["chunk_count"],
                        "queue_depth": current_mic["queue_depth"],
                        "queue_high_water": current_mic["queue_high_water"],
                        "stream_active": current_mic["stream_active"],
                        "detector_suspended": current_detector["suspended"],
                    }
                    unavailable = not fields["stream_active"]
                    suspended = fields["detector_suspended"]
                    stalled = (not unavailable and
                               fields["callback_delta"] == 0)
                    unhealthy = (unavailable or suspended or stalled or
                                 fields["status_callback_delta"] > 0)
                    if unhealthy:
                        diagnostics.warning(
                            "wake.idle.unavailable" if unavailable else
                            "wake.idle.detector_suspended" if suspended else
                            "wake.idle.stalled" if stalled else
                            "wake.idle.degraded", **fields)
                    else:
                        diagnostics.info("wake.idle.heartbeat", **fields)
                    heartbeat_mic = current_mic
                    heartbeat_detector = current_detector
                    heartbeat_at = now + heartbeat_s
                if detected:
                    wake_via = "voice"
                else:
                    if not manual_wake.is_set():
                        continue
                    manual_wake.clear()
            local_session_id = diagnostics.new_id("session")
            session_started = time.monotonic()
            with diagnostics.context(session_id=local_session_id, mode="voice"):
                diagnostics.info("voice.session.started", wake_via=wake_via)
                if config.echoecho_record("voice"):
                    recorder.start("voice")  # wake event is teed into recording
                events.emit("wake", via=wake_via,
                            run_id=diagnostics.get_run_id(),
                            session_id=local_session_id)
                # -- WAKE -> ACTIVE session ----------------------------------
                audio = None
                audio_transition_attempted = False
                audio_recovered = True
                fallback_reason = "interrupted"  # Ctrl-C / cancellation
                try:
                    missed = orch.collect_missed()
                    diagnostics.info("voice.missed_tasks.collected",
                                     count=len(missed))
                    since = None
                    if missed:
                        since = ("[since last session] Background tasks finished "
                                 "while echoecho was asleep: " +
                                 " | ".join(missed) +
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
                    orch.live = True
                    client.on_tool(make_tool_handler(orch, client))
                    # A failure before wake() must not reuse the prior reason.
                    session.end_reason = None
                    # Set this before the call: a partial failure may already
                    # have closed the wake mic or opened a session stream.
                    audio_transition_attempted = True
                    start_session_audio(mic, audio, loop, send_event=None)
                    with diagnostics.span("realtime.connect", model=model):
                        await client.connect()
                    audio.set_sender(client.send_input_audio)
                    audio.play_chime("wake")
                    await client.run()
                except Exception as exc:  # daemon lives: return to wake loop
                    diagnostics.exception("voice.session.crashed", exc=exc,
                                          state=session.state)
                    print("[voice] session crashed (%s) — returning to IDLE"
                          % exc)
                    fallback_reason = "crash"
                    if session.state == "ACTIVE":
                        session.begin_ending("crash")
                    if session.state == "ENDING":
                        session.finish()
                finally:
                    orch.live = False
                    orch.on_injection = lambda inj: None
                    if audio_transition_attempted:
                        audio.muted_capture = True
                        try:
                            audio.play_chime("end")
                            await asyncio.sleep(
                                max(0.3, audio.pending_ms() / 1000.0))
                        except Exception as exc:
                            diagnostics.exception(
                                "audio.end_chime.failed", exc=exc)
                    audio_recovered = recover_session_audio(
                        mic, audio, detector, audio_transition_attempted)
                    reason = session.end_reason or fallback_reason
                    recorder.stop(end_reason=reason)
                    manual_wake.clear()
                    diagnostics.info(
                        "voice.session.finished", reason=reason,
                        duration_ms=round(
                            (time.monotonic() - session_started) * 1000, 1))
                    if not audio_recovered:
                        diagnostics.error(
                            "voice.audio_recovery.fatal",
                            reason="stream_close_failed")
                        raise RuntimeError(
                            "audio stream close failed; stopping before an "
                            "unsafe PortAudio refresh")
            print("[wake] session over (%s) — listening for '%s' again"
                  % (session.end_reason, config.WAKE_PHRASE))
    finally:
        diagnostics.info("voice.shutdown.started")
        try:
            mic.stop()
        except Exception as exc:
            diagnostics.exception("wake.mic.stop.failed", exc=exc)
        recorder.stop(end_reason="shutdown")  # no-op unless mid-session
        orch_loop.cancel()
        await asyncio.gather(orch_loop, return_exceptions=True)
        if viewer:
            viewer.stop()
            diagnostics.info("viewer.stopped")
        diagnostics.info("voice.shutdown.finished")


def main(argv=None):
    import importlib.util
    import platform
    import shutil

    from echoecho_app import __version__, config, diagnostics
    config.load_env_local()  # .env.local secrets; real env vars win
    apply_early_diagnostic_args(argv)
    try:
        args = parse_args(argv)
    except SystemExit as exc:
        if exc.code not in (None, 0):
            diagnostics.configure(
                "daemon", mode="argument_error", version=__version__,
                python=platform.python_version(), platform=platform.platform())
            diagnostics.error("startup.argument_invalid")
            diagnostics.shutdown(outcome="error", exit_code=(
                exc.code if isinstance(exc.code, int) else 1))
        raise
    if args.model:
        os.environ["ECHOECHO_REALTIME_MODEL"] = args.model
    if args.fake_llm:
        os.environ["ECHOECHO_FAKE_LLM"] = "1"
    if args.text:
        os.environ["ECHOECHO_TEXT"] = "1"
    apply_device_args(args)  # flags beat ECHOECHO_INPUT_DEVICE/ECHOECHO_OUTPUT_DEVICE
    if args.no_record:
        os.environ["ECHOECHO_RECORD"] = "0"
    if args.diagnostics_dir:
        os.environ["ECHOECHO_DIAGNOSTICS_DIR"] = args.diagnostics_dir
    if args.no_diagnostics:
        os.environ["ECHOECHO_DIAGNOSTICS"] = "0"
    mode = ("voice" if args.voice else "script" if args.script else
            "text" if (args.text or config.echoecho_text()) else
            "recordings" if args.recordings else
            "list-devices" if args.list_devices else
            "mic-check" if args.mic_check else "none")
    diagnostics.configure(
        "daemon", mode=mode, version=__version__,
        python=platform.python_version(), platform=platform.platform(),
        revision=os.environ.get("ECHOECHO_BUILD_SHA", "unknown"),
        parent_run_id=os.environ.get("ECHOECHO_PARENT_RUN_ID") or None)
    gui_backend = config.gui_input_backend()
    diagnostics.info(
        "startup.capabilities",
        openai_key_present=bool(os.environ.get("OPENAI_API_KEY")),
        fake_llm=config.echoecho_fake_llm(),
        recording_enabled=config.echoecho_record(mode),
        viewer_enabled=not args.no_viewer,
        realtime_model=config.realtime_model() if args.voice else None,
        sandbox=config.sandbox_tier(),
        gui_input_backend=(gui_backend if gui_backend in {"vnc", "ssh"}
                           else "other"),
        vnc_override_present=bool(config.vnc_url_override()),
        sounddevice_available=importlib.util.find_spec("sounddevice") is not None,
        vosk_model_present=config.VOSK_MODEL_DIR.is_dir(),
        claude_available=shutil.which("claude") is not None,
        codex_available=shutil.which("codex") is not None,
        lume_available=shutil.which("lume") is not None)
    if args.recordings:
        list_recordings()
        diagnostics.shutdown(outcome="ok")
        return
    if args.list_devices:
        list_devices()
        diagnostics.shutdown(outcome="ok")
        return
    if args.mic_check:
        mic_check()
        diagnostics.shutdown(outcome="ok")
        return
    from echoecho_app import events
    events.reset(mode=mode, run_id=diagnostics.get_run_id())
    if args.voice:
        try:
            import sounddevice  # noqa: F401  (lazy: voice-mode-only dependency)
        except ImportError as exc:
            diagnostics.exception("startup.dependency_missing", exc=exc,
                                  dependency="sounddevice")
            sys.exit("--voice needs sounddevice (Mac only): "
                     "pip install -r requirements-mac.txt. "
                     "Try --text or --script fixtures/smoke.txt here.")
        if not os.environ.get("OPENAI_API_KEY"):
            diagnostics.error("startup.configuration_invalid",
                              reason="OPENAI_API_KEY missing")
            sys.exit("--voice needs OPENAI_API_KEY set.")
        start_tether_watchdog()
        interrupted = False
        try:
            asyncio.run(voice_main(args))
        except KeyboardInterrupt:
            diagnostics.info("run.interrupted", signal="KeyboardInterrupt")
            interrupted = True
        diagnostics.shutdown(outcome="interrupted" if interrupted else "ok")
        return
    if not (args.script or args.text or config.echoecho_text()):
        diagnostics.error("startup.mode_missing")
        sys.exit("No mode selected: use --text, --script PATH, or set ECHOECHO_TEXT=1. "
                 "(Voice mode lands in later PRs.)")
    asyncio.run(amain(args))
    diagnostics.shutdown(outcome="ok")


def _entrypoint():
    """Preserve clean summaries for service-manager TERM and failed exits."""
    import signal
    import threading

    from echoecho_app import diagnostics

    previous_term = signal.getsignal(signal.SIGTERM)
    term_cancel = threading.Event()
    term_backstop_started = [False]

    def hard_term_exit():
        if not term_cancel.wait(10.0):
            os._exit(143)

    def on_term(signum, _frame):
        if not term_backstop_started[0]:
            term_backstop_started[0] = True
            threading.Thread(
                target=hard_term_exit, daemon=True,
                name="sigterm-hard-exit").start()
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, on_term)
    try:
        try:
            main()
        except KeyboardInterrupt:
            diagnostics.info("run.interrupted", signal="SIGTERM_or_SIGINT")
            diagnostics.shutdown(outcome="interrupted")
            return 130
        except SystemExit as exc:
            code = exc.code
            failed = code not in (None, 0)
            if failed:
                diagnostics.error(
                    "process.exit_requested", exit_code=(code if isinstance(
                        code, int) else 1), exit_code_type=type(code).__name__)
            diagnostics.shutdown(outcome="error" if failed else "ok",
                                 exit_code=(code if isinstance(code, int)
                                            else int(failed)))
            raise
        except BaseException as exc:
            diagnostics.exception("process.crashed", exc=exc)
            diagnostics.shutdown(outcome="crash")
            raise
        else:
            # Most normal branches close explicitly so their domain outcome is
            # exact; this catches future branches that forget to do so.
            diagnostics.shutdown(outcome="ok")
            return 0
    finally:
        term_cancel.set()
        signal.signal(signal.SIGTERM, previous_term)


if __name__ == "__main__":
    sys.exit(_entrypoint())
