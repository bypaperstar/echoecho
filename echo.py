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
    else:
        from echo_app.conversation.textmode import TextRepl
        port = TextRepl(session=session)

    orch = Orchestrator(registry=load_all(), on_injection=port.inject,
                        fake_llm=config.echo_fake_llm())
    port.on_tool(make_tool_handler(orch, port))

    orch_loop = asyncio.ensure_future(orch.run())
    try:
        await port.run()
        await orch.drain(timeout=2.0)  # let in-flight demo workers log
    finally:
        orch_loop.cancel()


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
        sys.exit("Voice mode isn't built yet (Realtime transport lands in PR 4, "
                 "Mac audio in PR 5). Try --text or --script fixtures/smoke.txt.")
    from echo_app import config
    if not (args.script or args.text or config.echo_text()):
        sys.exit("No mode selected: use --text, --script PATH, or set ECHO_TEXT=1. "
                 "(Voice mode lands in later PRs.)")
    asyncio.run(amain(args))


if __name__ == "__main__":
    main()
