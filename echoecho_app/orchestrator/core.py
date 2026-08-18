"""Orchestrator: inbox queue -> worker registry -> ranked Injections back up.

Generic by design: knows kinds only as registry keys; chaining is the
follow_ups[] list on TaskResult, re-enqueued verbatim.
"""
import asyncio
import dataclasses
import hashlib
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from echoecho_app import config, diagnostics, events
from echoecho_app.bus import Injection, Task, TaskRequest, TaskResult
from echoecho_app.orchestrator import log as tasklog
from echoecho_app.orchestrator.ranker import rank
from echoecho_app.services import artifacts


SNAPSHOT_CAP = 5  # ambient [workspace] injections per finished task
TITLE_WORDS = 5  # spoken-handle length
RECENT_SUMMARIES = 10  # finished tasks check_tasks shows without an id


def _diagnostic_label_fields(value, allowed, name):
    """Return a known label or only correlation metadata for unknown input."""
    try:
        if isinstance(value, str) and value in allowed:
            return {name: value}
    except Exception:
        pass
    try:
        raw = value if isinstance(value, str) else str(value)
    except Exception:
        raw = "<unprintable %s>" % type(value).__name__
    return {
        name: "unknown",
        name + "_length": len(raw),
        name + "_fingerprint": hashlib.sha256(
            raw.encode("utf-8", "replace")).hexdigest()[:16],
    }


def _diagnostic_kind_fields(kind, registry, name="kind"):
    """Return privacy-safe diagnostic metadata for a task kind.

    Registry keys are the only task-kind labels diagnostics may record
    verbatim.  Requests and replayed task logs are input boundaries, so an
    unknown kind may itself contain user-authored text; retain only enough
    metadata to correlate repeated bad values without reproducing them.
    """
    return _diagnostic_label_fields(kind, registry, name)


def _compact(text, limit=500):
    """Squash a markdown file to one speakable line for an ambient injection."""
    joined = " / ".join(ln.strip() for ln in text.splitlines() if ln.strip())
    return joined[:limit] + ("…" if len(joined) > limit else "")


def _title(instructions):
    """Short spoken handle: the first few words of the ask."""
    words = (instructions or "").split()
    return " ".join(words[:TITLE_WORDS]) + ("…" if len(words) > TITLE_WORDS
                                            else "")


def _elapsed(secs):
    secs = max(0, int(secs))
    if secs < 60:
        return "%ds" % secs
    if secs < 3600:
        return "%dm%02ds" % (secs // 60, secs % 60)
    return "%dh%02dm" % (secs // 3600, secs % 3600 // 60)


@dataclass
class WorkerContext:
    """What every worker gets alongside its Task (Contract B's ctx).

    report is per-task (bound by the orchestrator): report(text) streams a
    progress line — throttled into ambient injections, surfaced by
    check_tasks; report(session_id=...) checkpoints the agent session behind
    the task so it stays resumable, even across a restart."""
    workspace: Path
    fake_llm: bool = False
    extra: Dict[str, Any] = field(default_factory=dict)
    report: Optional[Callable] = None


class Orchestrator:
    def __init__(self, registry, on_injection=None, log_path=None,
                 workspace=None, fake_llm=False):
        # registry: {kind: async run(task, ctx) -> TaskResult}
        self.registry = registry
        self.on_injection = on_injection or (lambda inj: None)
        self.log_path = Path(log_path) if log_path else config.TASKS_LOG
        self._inbox = None  # created lazily: 3.9 Queues bind the running loop
        self.tasks = {}  # type: Dict[str, Task]
        self.ctx = WorkerContext(workspace=Path(workspace or config.WORKSPACE_DIR),
                                 fake_llm=fake_llm)
        # workers that steer prior tasks (agent.run task_id resume) read the
        # LIVE table through ctx; the dict is shared, not copied
        self.ctx.extra.setdefault("tasks", self.tasks)
        self._seq = 0
        self._running = set()
        self._serial_locks = {}  # kind -> asyncio.Lock for serialize=True kinds
        # PR 11 announcement watermark: which task results have been spoken.
        # live=True means on_injection reaches an ACTIVE session, so a result
        # injected now counts as announced; while IDLE (live=False) results
        # are dropped and wait to be surfaced on the next wake — persisted so
        # a completed-while-idle task still announces across a restart.
        self._announced = set()
        self.live = False

    @property
    def inbox(self):
        if self._inbox is None:
            self._inbox = asyncio.Queue()
        return self._inbox

    # -- Contract A entry points ------------------------------------------

    def submit(self, request):  # type: (TaskRequest) -> Task
        """Enqueue a task and return it immediately (never blocks the voice turn)."""
        self._seq += 1
        task = Task(id="t%d" % self._seq, request=request, created_at=time.time(),
                    title=_title(request.instructions))
        self.tasks[task.id] = task
        tasklog.append_event(self.log_path, "queued", task_id=task.id,
                             kind=task.kind, instructions=request.instructions,
                             source=request.source)
        events.emit("task", task_id=task.id, kind=task.kind, status="queued")
        diagnostics.info(
            "task.queued", task_id=task.id,
            queue_depth=self.inbox.qsize() + 1,
            **_diagnostic_kind_fields(task.kind, self.registry),
            **_diagnostic_label_fields(
                request.source, {"user", "follow_up"}, "source"))
        self.inbox.put_nowait(task)
        return task

    def summaries(self, task_id=None, recent=RECENT_SUMMARIES):
        """Compact status lines for the check_tasks tool: handle, status,
        elapsed time + last progress line while running (PR 11 — "how's it
        going?" works mid-task).

        With no id, the table persists across restarts and grows without
        bound, so return every unfinished task plus only the most recent
        `recent` finished ones — the active work, never a wall of history."""
        if task_id in self.tasks:
            tasks = [self.tasks[task_id]]
        else:
            allt = list(self.tasks.values())
            active = [t for t in allt if t.status in ("queued", "running")]
            done = sorted((t for t in allt if t.status not in
                           ("queued", "running")),
                          key=lambda t: t.finished_at or 0.0)[-recent:]
            tasks = sorted(active + done, key=lambda t: int(t.id[1:])
                           if t.id[1:].isdigit() else 0)
        out = []
        for t in tasks:
            line = "%s %s" % (t.id, t.kind)
            if t.title:
                line += " '%s'" % t.title
            line += ": %s" % t.status
            if t.status in ("queued", "running"):
                line += " — %s elapsed" % _elapsed(time.time() - t.created_at)
                if t.progress:
                    line += ", last: %s" % t.progress
            elif t.result and t.result.say:
                line += " — " + t.result.say
            out.append(line)
        return out

    def results_since(self, ts):
        """Speech-ready lines for non-silent tasks that finished after ts —
        the '[since last session]' wake injection (voice daemon, PR 6)."""
        done = [t for t in self.tasks.values()
                if t.finished_at is not None and t.finished_at > ts
                and t.result is not None and rank(t.result) != "silent"]
        return ["%s (%s): %s" % (t.id, t.kind, t.result.say)
                for t in sorted(done, key=lambda t: t.finished_at)]

    def collect_missed(self):
        """Speech-ready lines for every non-silent finished task not yet
        announced, oldest first; marks them announced (persisted) so they are
        spoken exactly once — even if the finish and the next wake straddle a
        restart. Replaces the wall-clock results_since window for the wake
        announcement, which lost completed-while-idle tasks across restarts."""
        pending = [t for t in self.tasks.values()
                   if t.id not in self._announced
                   and t.finished_at is not None and t.result is not None
                   and rank(t.result) != "silent"]
        pending.sort(key=lambda t: t.finished_at)
        self._mark_announced(pending)
        return ["%s (%s): %s" % (t.id, t.kind, t.result.say) for t in pending]

    def _mark_announced(self, tasks):
        for t in tasks:
            if t.id not in self._announced:
                self._announced.add(t.id)
                tasklog.append_event(self.log_path, "announced", task_id=t.id)

    # -- persistence (PR 11) --------------------------------------------------

    def rehydrate(self):
        """Rebuild the task table from the append-only log, so v2's
        minutes-to-hours tasks survive a restart. Tasks caught mid-flight
        (queued/running at the last shutdown) are marked as interrupted
        errors — still resumable by task_id when a session was checkpointed.
        Returns the newly-interrupted tasks so the caller can announce them."""
        for e in tasklog.replay(self.log_path):
            tid = e.get("task_id")
            if not tid:
                continue
            if e.get("event") == "queued":
                req = TaskRequest(kind=e.get("kind", ""),
                                  instructions=e.get("instructions", ""),
                                  source=e.get("source", "user"))
                self.tasks[tid] = Task(id=tid, request=req,
                                       created_at=e.get("ts", 0.0),
                                       title=_title(req.instructions))
                continue
            task = self.tasks.get(tid)
            if task is None:
                continue  # log tail from before a truncation; skip
            if e.get("event") == "session":
                task.session_id = e.get("session_id") or task.session_id
            elif e.get("event") == "announced":
                self._announced.add(tid)  # already spoken in a prior run
            elif e.get("event") in ("done", "error"):
                task.status = e["event"]
                task.finished_at = e.get("ts")
                task.session_id = e.get("session_id") or task.session_id
                data = {"session_id": task.session_id}
                if e["event"] == "error":
                    data["error"] = e.get("say") or "failed"
                task.result = TaskResult(say=e.get("say", ""),
                                         priority=e.get("priority", "ambient"),
                                         data=data)
        self._seq = max([int(t[1:]) for t in self.tasks
                         if t[1:].isdigit()] or [0])
        interrupted = [t for t in self.tasks.values()
                       if t.status in ("queued", "running")]
        for task in interrupted:
            task.status = "error"
            task.finished_at = time.time()
            resumable = task.session_id is not None
            task.result = TaskResult(
                say="'%s' was interrupted by a restart%s." % (
                    task.title or task.id,
                    " — it can be resumed" if resumable else ""),
                data={"error": "interrupted",
                      "session_id": task.session_id})
            tasklog.append_event(self.log_path, "error", task_id=task.id,
                                 kind=task.kind, say=task.result.say,
                                 priority="interrupt", artifacts_touched=[],
                                 follow_ups=[], session_id=task.session_id)
        diagnostics.info("task_log.rehydrated", task_count=len(self.tasks),
                         interrupted_count=len(interrupted),
                         announced_count=len(self._announced))
        return interrupted

    # -- main loop ----------------------------------------------------------

    async def run(self):
        while True:
            task = await self.inbox.get()
            handle = asyncio.create_task(self._run_task(task),
                                         name="worker-%s" % task.id)
            self._running.add(handle)
            handle.add_done_callback(self._on_task_done)

    def _on_task_done(self, handle):
        """Retrieve every spawned task exception. Worker exceptions are
        converted into TaskResult inside _run_task, but post-processing bugs
        must not become an unobserved ``Task exception was never retrieved``."""
        self._running.discard(handle)
        if handle.cancelled():
            return
        try:
            exc = handle.exception()
        except (asyncio.CancelledError, Exception) as exc:
            diagnostics.exception("task.runner.exception_read_failed", exc=exc)
            return
        if exc is not None:
            diagnostics.exception("task.runner.crashed", exc=exc,
                                  asyncio_task=handle.get_name())

    def _reporter(self, task):
        """Per-task progress channel (ctx.report): text lines throttle into
        ambient injections; session ids checkpoint into the log so the task
        stays resumable across a restart."""
        state = {"last_inject": 0.0}
        diagnostic_kind = _diagnostic_kind_fields(task.kind, self.registry)

        def report(text=None, session_id=None):
            if session_id and session_id != task.session_id:
                task.session_id = session_id
                tasklog.append_event(self.log_path, "session", task_id=task.id,
                                     session_id=session_id)
                diagnostics.info("task.agent_session.checkpointed",
                                 task_id=task.id, **diagnostic_kind)
            if not text:
                return
            task.progress = text
            events.emit("task", task_id=task.id, kind=task.kind,
                        status="progress", say=text)
            now = time.time()
            progress_count = getattr(task, "_diagnostic_progress_count", 0) + 1
            task._diagnostic_progress_count = progress_count
            if (progress_count <= 3 or
                    progress_count & (progress_count - 1) == 0):
                diagnostics.metric(
                    "task.progress.count", progress_count,
                    task_id=task.id, **diagnostic_kind)
            if now - state["last_inject"] >= config.progress_interval():
                state["last_inject"] = now
                self.on_injection(Injection(
                    text="[task %s progress] %s" % (task.id, text),
                    priority="ambient"))

        return report

    async def _run_task(self, task):  # type: (Task) -> None
        total_started = time.monotonic()
        queue_wait_ms = max(0.0, (time.time() - task.created_at) * 1000)
        diagnostic_kind = _diagnostic_kind_fields(task.kind, self.registry)
        diagnostic_context_kind = _diagnostic_kind_fields(
            task.kind, self.registry, name="task_kind")
        with diagnostics.context(task_id=task.id, **diagnostic_context_kind):
            worker = self.registry.get(task.kind)
            lock = None
            lock_wait_ms = 0.0
            serialize = getattr(worker, "serialize", False)
            if serialize:
                # Serialized kinds run in dispatch order; capture contention
                # because it otherwise looks like a hung worker.
                key = serialize if isinstance(serialize, str) else task.kind
                lock = self._serial_locks.setdefault(key, asyncio.Lock())
                lock_started = time.monotonic()
                await lock.acquire()
                lock_wait_ms = (time.monotonic() - lock_started) * 1000
            task.status = "running"
            events.emit("task", task_id=task.id, kind=task.kind,
                        status="running")
            diagnostics.info(
                "task.started",
                queue_wait_ms=round(queue_wait_ms, 1),
                lock_wait_ms=round(lock_wait_ms, 1), serialized=bool(serialize),
                **diagnostic_kind)
            ctx = dataclasses.replace(self.ctx, report=self._reporter(task))
            worker_started = time.monotonic()
            try:
                if worker is None:
                    raise KeyError("no worker registered for kind %r" % task.kind)
                result = await worker(task, ctx)
            except Exception as exc:
                diagnostics.exception(
                    "task.worker.failed", exc=exc,
                    worker_ms=round((time.monotonic() - worker_started) * 1000,
                                    1), **diagnostic_kind)
                result = TaskResult(
                    say="Task %s (%s) failed: %s" % (task.id, task.kind, exc),
                    data={"error": str(exc),
                          "traceback": traceback.format_exc()})
                task.status = "error"
            else:
                task.status = "done"
            finally:
                if lock is not None:
                    lock.release()
            worker_ms = (time.monotonic() - worker_started) * 1000
        task.result = result
        task.finished_at = time.time()
        task.session_id = result.data.get("session_id") or task.session_id
        priority = rank(result)
        tasklog.append_event(self.log_path, task.status, task_id=task.id,
                             kind=task.kind, say=result.say, priority=priority,
                             artifacts_touched=result.artifacts_touched,
                             follow_ups=[f.kind for f in result.follow_ups],
                             session_id=task.session_id)
        events.emit("task", task_id=task.id, kind=task.kind,
                    status=task.status, say=result.say, priority=priority,
                    artifacts_touched=result.artifacts_touched)
        diagnostics.info(
            "task.finished", task_id=task.id,
            status=task.status, priority=priority,
            result_error=bool(result.data.get("error")),
            worker_ms=round(worker_ms, 1),
            total_ms=round((time.monotonic() - total_started) * 1000, 1),
            artifact_count=len(result.artifacts_touched),
            follow_up_count=len(result.follow_ups), live=self.live,
            progress_count=getattr(task, "_diagnostic_progress_count", 0),
            **diagnostic_kind)
        for follow in result.follow_ups:  # generic chaining
            follow.source = "follow_up"
            self.submit(follow)
        if priority != "silent":  # silent -> task table only (check_tasks)
            self.on_injection(Injection(
                text="[task %s done] %s" % (task.id, result.say),
                priority=priority))
            # delivered to a live session -> counts as announced; while IDLE
            # the sink drops it, so it stays pending for the next wake
            if self.live:
                self._mark_announced([task])
        # ambient doc snapshots so the agent can answer "read me the goals" —
        # capped: an agent.run that touches a whole tree must not flood the
        # conversation context with one injection per file
        touched = result.artifacts_touched
        for name in touched[:SNAPSHOT_CAP]:
            snap = _compact(artifacts.read(self.ctx.workspace, name))
            if snap:  # binary/unreadable files read back empty: skipped
                self.on_injection(Injection(
                    text="[workspace] %s now contains: %s" % (name, snap),
                    priority="ambient"))
        if len(touched) > SNAPSHOT_CAP:
            self.on_injection(Injection(
                text="[workspace] …and %d more files changed (use "
                     "read_artifact for any of them)"
                     % (len(touched) - SNAPSHOT_CAP),
                priority="ambient"))

    async def drain(self, timeout=5.0):
        """Test/demo helper: wait until inbox is empty and no worker is running."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.inbox.empty() and not self._running:
                return True
            await asyncio.sleep(0.02)
        return False
