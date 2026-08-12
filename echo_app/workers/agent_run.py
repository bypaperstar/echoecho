"""agent.run: THE generic worker — any natural-language task, executed by a
headless coding agent with cwd=workspace/ (sandbox tier 1).

The agent writes workspace files itself; touched artifacts are detected by
diffing an mtime snapshot around the run, so the orchestrator's ambient
workspace injections keep working with zero coupling to what the agent did.
(Attribution is heuristic: two agent runs overlapping in time can claim each
other's writes. Dispatches are conversation-paced, so this stays cosmetic.)
Fixture lines whose type starts with "_" are control hooks (the FakeTransport
trick): "_write" performs a workspace write, standing in for the file edits a
real agent makes — real CLIs never emit them.
"""
import asyncio
import json

from echo_app.bus import TaskResult
from echo_app.services import agent_cli, artifacts
from echo_app.workers.base import register

KIND = "agent.run"
DESCRIPTION = ("hand any other task to a background agent that can research, "
               "write or edit any workspace file, and run code")
ARG_SCHEMA = {}

STDERR_TAIL = 2000
SAY_LIMIT = 240
MAX_LINE = 8 * 2 ** 20  # claude tool_result lines embed whole files
READ_CHUNK = 65536


def _speakable(text, limit=SAY_LIMIT):
    """Squash an agent's (possibly multi-paragraph markdown) result into one
    speakable line."""
    joined = " ".join(text.split())
    return joined[:limit] + ("…" if len(joined) > limit else "")


def _snapshot(workspace):
    """{name: (mtime, size, inode)} — the same signature triple the viewer
    polls with, so same-tick rewrites still register (rename swaps the inode)."""
    return {name: artifacts.stat_key(workspace, name)
            for name in artifacts.list_files(workspace)}


def _hook(workspace, ev):
    if ev.get("type") == "_write":
        try:
            artifacts.write_atomic(workspace, ev.get("file", ""),
                                   ev.get("content", ""))
        except (ValueError, OSError):
            pass  # a broken fixture write shows up in the test's asserts


async def _lines(stream, max_line=MAX_LINE):
    """Decoded stdout lines. asyncio's readline() RAISES past its 64 KiB
    limit and real agent CLIs routinely exceed it (tool results embed whole
    files), so split lines ourselves and silently drop anything over
    max_line instead of failing the task."""
    buf = b""
    skipping = False
    while True:
        chunk = await stream.read(READ_CHUNK)
        if not chunk:
            if buf and not skipping:
                yield buf.decode("utf-8", "replace")
            return
        buf += chunk
        while True:
            nl = buf.find(b"\n")
            if nl == -1:
                if len(buf) > max_line:
                    buf = b""
                    skipping = True  # drop the rest of this oversized line
                break
            line, buf = buf[:nl], buf[nl + 1:]
            if skipping:
                skipping = False
            elif line.strip():
                yield line.decode("utf-8", "replace")


async def _pump(stream, runtime, workspace, state):
    """Parse the CLI's stdout JSONL into the normalized event stream."""
    async for line in _lines(stream):
        try:
            raw = json.loads(line)
        except ValueError:
            continue  # CLIs print the odd non-JSON banner; never fatal
        if not isinstance(raw, dict) or not isinstance(raw.get("type"), str):
            continue
        if raw.get("type", "").startswith("_"):
            _hook(workspace, raw)
            continue
        ev = runtime.parse_event(raw)
        if ev is None:
            continue
        if ev["event"] == "init":
            state["session_id"] = ev.get("session_id") or state["session_id"]
        elif ev["event"] == "progress":
            state["progress"].append(ev["text"])
        elif ev["event"] == "result":
            state["result"] = ev.get("text") or state["result"]
            state["session_id"] = ev.get("session_id") or state["session_id"]
            if ev.get("error"):
                state["error"] = ev.get("text") or "agent reported an error"


async def _drain(stream, state):
    """Keep a bounded stderr tail — never the whole stream in memory."""
    tail = b""
    while True:
        chunk = await stream.read(READ_CHUNK)
        if not chunk:
            break
        tail = (tail + chunk)[-4 * STDERR_TAIL:]
    state["stderr"] = tail.decode("utf-8", "replace")


@register(KIND, description=DESCRIPTION, arg_schema=ARG_SCHEMA)
async def run_agent(task, ctx):
    runtime = agent_cli.for_ctx(ctx)
    if runtime is None:
        return TaskResult(say="I can't run agent tasks here — no agent CLI "
                              "(claude or codex) is installed.",
                          priority="interrupt", data={"error": "no agent cli"})
    try:
        argv = runtime.command(task.request.instructions)
    except Exception as exc:  # e.g. a directory fixture ran out of scripts
        return TaskResult(say="The agent couldn't start: %s" % exc,
                          priority="interrupt", data={"error": str(exc)})

    before = _snapshot(ctx.workspace)
    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=str(ctx.workspace),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    state = {"progress": [], "result": "", "error": None,
             "session_id": None, "stderr": ""}
    pump = asyncio.ensure_future(_pump(proc.stdout, runtime,
                                       ctx.workspace, state))
    drain = asyncio.ensure_future(_drain(proc.stderr, state))
    try:
        await asyncio.gather(pump, drain)
        rc = await proc.wait()
    finally:
        # a worker exception must NEVER orphan a live agent: nobody would be
        # reading its pipes, so it would block forever, still burning tokens
        pump.cancel()
        drain.cancel()
        await asyncio.gather(pump, drain, return_exceptions=True)
        if proc.returncode is None:
            proc.kill()
            await proc.wait()

    touched = sorted(name for name, mt in _snapshot(ctx.workspace).items()
                     if before.get(name) != mt)
    data = {"result": state["result"], "cli": runtime.name, "exit_code": rc,
            "session_id": state["session_id"],
            "progress": state["progress"][-5:]}
    if state["error"] or rc != 0:
        data["error"] = state["error"] or "%s exited %d" % (runtime.name, rc)
        if state["stderr"]:
            data["stderr"] = state["stderr"][-STDERR_TAIL:]
        return TaskResult(  # data["error"] auto-ranks as interrupt
            say="The agent task failed: %s" % _speakable(data["error"]),
            data=data, artifacts_touched=touched)
    final = state["result"] or (state["progress"][-1] if state["progress"]
                                else "")
    return TaskResult(
        say=_speakable(final) or "The agent finished the task.",
        priority="interrupt",  # primary user-requested result
        data=data, artifacts_touched=touched)
