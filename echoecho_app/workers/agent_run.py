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
import os
import signal
from pathlib import Path

from echoecho_app import config
from echoecho_app.bus import TaskResult
from echoecho_app.services import agent_cli, artifacts
from echoecho_app.services import vm as vm_mod
from echoecho_app.workers.base import register

KIND = "agent.run"
DESCRIPTION = ("hand any other task to a background agent that can research, "
               "write or edit any workspace file, and run code; pass "
               "args.task_id to steer, extend, or answer a previous agent "
               "task; args.sandbox='vm' runs it inside echoecho's own Mac VM "
               "(pick it for risky or code-heavy work)")
ARG_SCHEMA = {"task_id": {
    "type": "string",
    "description": "resume a previous agent task: same agent session"},
    "sandbox": {
    "type": "string", "enum": ["shell", "vm"],
    "description": "where the agent runs; vm = echoecho's own disposable Mac"}}

STDERR_TAIL = 2000
SAY_LIMIT = 240
MAX_LINE = 8 * 2 ** 20  # claude tool_result lines embed whole files
READ_CHUNK = 65536
QUESTION_PREFIX = "QUESTION:"

# every prompt teaches the needs_input convention, CLI-agnostic: a blocked
# agent surfaces one spoken question instead of silently guessing
PROMPT_SUFFIX = (
    "\n\nYou are working headless for a voice assistant; your reply will be "
    "spoken. If you cannot proceed without an answer from the user, stop and "
    "end your reply with one line starting '%s '." % QUESTION_PREFIX)


def _staged_changes(task, ctx):
    """Did this task stage document edits in its outbox? Returns a list of
    {name, target, summary} from workspace/outbox/<task>/MANIFEST.json, or []
    — the structured signal behind the deterministic 'say apply it' handoff,
    so surfacing staged changes never depends on the agent's free-text reply."""
    if not config.user_docs():
        return []
    raw = artifacts.read(ctx.workspace,
                         "%s/%s/MANIFEST.json" % (config.OUTBOX_DIR, task.id))
    try:
        entries = json.loads(raw) if raw else []
    except ValueError:
        return []
    out = []
    for e in entries if isinstance(entries, list) else []:
        if isinstance(e, dict) and e.get("staged") and e.get("target"):
            out.append({"name": Path(str(e["target"])).name,
                        "target": str(e["target"]),
                        "summary": str(e.get("summary", ""))})
    return out


def _user_docs_convention(task):
    """When the user has shared folders, teach the agent the mediated-write
    rule: their documents are read-only; propose changes via the outbox so
    the user can approve them by voice. Keyed to this task's outbox dir."""
    docs = config.user_docs()
    if not docs:
        return ""
    box = "%s/%s" % (config.OUTBOX_DIR, task.id)
    listing = ", ".join(str(d) for d in docs)
    return (
        "\n\nThe user's shared folders (%s) are mounted READ-ONLY: read them "
        "freely but NEVER modify a file there directly. To propose a change to "
        "one of their documents, write the full updated file under '%s/' and "
        "add an entry to '%s/MANIFEST.json' — a JSON list of "
        "{\"staged\": \"<path under %s>\", \"target\": \"<absolute original "
        "path>\", \"summary\": \"<one line>\"} — plus a short '%s/CHANGES.md'. "
        "The user approves saving by voice; you never overwrite their "
        "originals yourself." % (listing, box, box, box, box))


def _kill_tree(proc):
    """SIGKILL the agent's whole process group (start_new_session put it in
    its own), so children it spawned die with it. Falls back to the direct
    process if the group is already gone."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass


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


async def _pump(stream, runtime, workspace, state, report=None):
    """Parse the CLI's stdout JSONL into the normalized event stream,
    mirroring it into ctx.report so progress reaches the live conversation
    (throttled by the orchestrator) and the session id is checkpointed."""
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
            if report and state["session_id"]:
                report(session_id=state["session_id"])
        elif ev["event"] == "progress":
            state["progress"].append(ev["text"])
            if report:
                report(_speakable(ev["text"], 160))
        elif ev["event"] == "result":
            state["result"] = ev.get("text") or state["result"]
            state["session_id"] = ev.get("session_id") or state["session_id"]
            if ev.get("cost_usd") is not None:
                state["cost_usd"] = ev["cost_usd"]
            if ev.get("error"):
                state["error"] = ev.get("text") or "agent reported an error"
            if report and state["session_id"]:
                report(session_id=state["session_id"])


async def _drain(stream, state):
    """Keep a bounded stderr tail — never the whole stream in memory."""
    tail = b""
    while True:
        chunk = await stream.read(READ_CHUNK)
        if not chunk:
            break
        tail = (tail + chunk)[-4 * STDERR_TAIL:]
    state["stderr"] = tail.decode("utf-8", "replace")


def _resume_session(task, ctx):
    """args.task_id -> the prior task's agent session id, or a TaskResult
    explaining why the resume can't happen."""
    prior_id = task.request.args.get("task_id")
    if not prior_id:
        return None, None
    prior = (ctx.extra.get("tasks") or {}).get(prior_id)
    if prior is None:
        return None, TaskResult(
            say="I don't have a task %s to pick back up." % prior_id,
            data={"error": "unknown task_id %r" % prior_id})
    if prior.status in ("queued", "running"):
        return None, TaskResult(
            say="'%s' is still running — I can steer it once it finishes."
                % (prior.title or prior_id),
            data={"error": "task %s still running" % prior_id})
    session = prior.session_id or (prior.result.data.get("session_id")
                                   if prior.result else None)
    if not session:
        return None, TaskResult(
            say="'%s' left no agent session to resume — I can start fresh "
                "instead." % (prior.title or prior_id),
            data={"error": "no session recorded for %s" % prior_id})
    return session, None


@register(KIND, description=DESCRIPTION, arg_schema=ARG_SCHEMA)
async def run_agent(task, ctx):
    runtime = agent_cli.for_ctx(ctx)
    if runtime is None:
        return TaskResult(say="I can't run agent tasks here — no agent CLI "
                              "(claude or codex) is installed.",
                          priority="interrupt", data={"error": "no agent cli"})
    resume, refusal = _resume_session(task, ctx)
    if refusal is not None:
        return refusal  # data["error"] auto-ranks as interrupt
    prompt = (task.request.instructions + PROMPT_SUFFIX
              + _user_docs_convention(task))
    try:
        argv = runtime.command(prompt, resume=resume)
    except Exception as exc:  # e.g. a directory fixture ran out of scripts
        return TaskResult(say="The agent couldn't start: %s" % exc,
                          priority="interrupt", data={"error": str(exc)})

    # sandbox ladder (PR 12): the tier decides what host argv actually runs —
    # tier 1 spawns the CLI directly, tier 2 wraps it in ssh into echoecho's VM
    # with the workspace virtiofs-mounted; the pipeline below is identical
    sandbox = vm_mod.for_task(task, ctx)
    try:
        await sandbox.prepare()
    except Exception as exc:
        return TaskResult(
            say="The %s sandbox couldn't start: %s"
                % (sandbox.name, _speakable(str(exc))),
            data={"error": "sandbox: %s" % exc, "sandbox": sandbox.name})
    argv, cwd = sandbox.command(argv, ctx.workspace)

    # read the budget BEFORE spawning: a misconfigured ECHOECHO_AGENT_TIMEOUT
    # must fail fast, never after a live agent is already running (the
    # finally below can only reap a process it got to assign to `proc`)
    budget = config.agent_timeout()
    before = _snapshot(ctx.workspace)
    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        # own process group: the agent spawns its own children (builds, dev
        # servers, tool subprocesses); a budget kill must take the whole tree,
        # not just the CLI, or orphans burn resources past the budget
        start_new_session=True)
    state = {"progress": [], "result": "", "error": None,
             "session_id": resume, "stderr": "", "cost_usd": None}
    pump = asyncio.ensure_future(_pump(proc.stdout, runtime, ctx.workspace,
                                       state, report=ctx.report))
    drain = asyncio.ensure_future(_drain(proc.stderr, state))
    rc, timed_out = None, False
    try:
        try:
            await asyncio.wait_for(asyncio.gather(pump, drain), budget)
            rc = await proc.wait()
        except asyncio.TimeoutError:
            timed_out = True
    finally:
        # a worker exception must NEVER orphan a live agent: nobody would be
        # reading its pipes, so it would block forever, still burning tokens
        pump.cancel()
        drain.cancel()
        await asyncio.gather(pump, drain, return_exceptions=True)
        if proc.returncode is None:
            _kill_tree(proc)  # tier 1: the agent tree. tier 2: only local ssh
            await proc.wait()
            # tier 2's real agent runs in the guest, unreachable over the now
            # dead ssh — dispose the whole VM so no guest child outlives the
            # budget (no-op for shell/fake without a reset())
            await vm_mod.discard(sandbox)

    touched = sorted(name for name, mt in _snapshot(ctx.workspace).items()
                     if before.get(name) != mt)
    data = {"result": state["result"], "cli": runtime.name, "exit_code": rc,
            "session_id": state["session_id"], "sandbox": sandbox.name,
            "progress": state["progress"][-5:]}
    if state["cost_usd"] is not None:
        data["cost_usd"] = state["cost_usd"]
    if timed_out:
        data["error"] = "hit the %d-minute budget" % (budget / 60)
        return TaskResult(
            say="The agent task hit its %d-minute budget — I stopped it; "
                "partial work is in the workspace and it can be resumed."
                % (budget / 60),
            data=data, artifacts_touched=touched)
    if state["error"] or rc != 0:
        data["error"] = state["error"] or "%s exited %d" % (runtime.name, rc)
        if state["stderr"]:
            data["stderr"] = state["stderr"][-STDERR_TAIL:]
        return TaskResult(  # data["error"] auto-ranks as interrupt
            say="The agent task failed: %s" % _speakable(data["error"]),
            data=data, artifacts_touched=touched)
    final = state["result"] or (state["progress"][-1] if state["progress"]
                                else "")
    lines = [ln.strip() for ln in final.splitlines() if ln.strip()]
    if lines and lines[-1].startswith(QUESTION_PREFIX):
        question = lines[-1][len(QUESTION_PREFIX):].strip()
        data["needs_input"] = True  # auto-ranks as interrupt
        return TaskResult(
            say="The agent needs an answer to continue: %s"
                % _speakable(question),
            data=data, artifacts_touched=touched)
    # if the agent staged document changes, surface the approval handoff
    # DETERMINISTICALLY (not left to the model inferring from its free text)
    staged = _staged_changes(task, ctx)
    if staged:
        data["staged"] = staged
        names = ", ".join(s["name"] for s in staged[:3])
        more = "" if len(staged) <= 3 else " and %d more" % (len(staged) - 3)
        return TaskResult(
            say="Staged %d change%s to %s%s — say \"apply it\" to save over "
                "the original%s." % (len(staged),
                                     "" if len(staged) == 1 else "s", names,
                                     more, "" if len(staged) == 1 else "s"),
            priority="interrupt", data=data, artifacts_touched=touched)
    return TaskResult(
        say=_speakable(final) or "The agent finished the task.",
        priority="interrupt",  # primary user-requested result
        data=data, artifacts_touched=touched)
