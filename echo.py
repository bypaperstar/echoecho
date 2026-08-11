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
    p.add_argument("--mic-check", action="store_true", help="mic diagnostics (later PR)")
    return p.parse_args(argv)


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
    elif args.voice:
        # PR 4 wiring: real WS transport + reconnect factory. main() already
        # guards on audio I/O (PR 5), so this only runs once mic/speaker land.
        from echo_app.conversation.realtime import (RealtimeClient,
                                                    WebSocketTransport)
        model = config.realtime_model()
        port = RealtimeClient(WebSocketTransport(model), session=session,
                              transport_factory=lambda: WebSocketTransport(model))
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


def main(argv=None):
    args = parse_args(argv)
    if args.model:
        os.environ["ECHO_REALTIME_MODEL"] = args.model
    if args.fake_llm:
        os.environ["ECHO_FAKE_LLM"] = "1"
    if args.text:
        os.environ["ECHO_TEXT"] = "1"
    if args.mic_check:
        sys.exit("--mic-check isn't built yet (lands with Mac audio in PR 5). "
                 "Try --text or --script fixtures/smoke.txt.")
    if args.voice:
        try:
            import sounddevice  # noqa: F401  (lazy: voice-mode-only dependency)
        except ImportError:
            sys.exit("--voice: the Realtime client is built (PR 4, tested via "
                     "FakeTransport) but mic/speaker I/O needs 'sounddevice' "
                     "and lands in PR 5. Try --text or --script "
                     "fixtures/smoke.txt.")
        sys.exit("--voice: Realtime client is ready (PR 4) but audio "
                 "capture/playback lands in PR 5. Try --text for now.")
    from echo_app import config
    if not (args.script or args.text or config.echo_text()):
        sys.exit("No mode selected: use --text, --script PATH, or set ECHO_TEXT=1. "
                 "(Voice mode lands in later PRs.)")
    asyncio.run(amain(args))


if __name__ == "__main__":
    main()
