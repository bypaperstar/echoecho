"""Orchestrator: inbox queue -> worker registry -> ranked Injections back up.

Generic by design: knows kinds only as registry keys; chaining is the
follow_ups[] list on TaskResult, re-enqueued verbatim.
"""
import asyncio
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from echo_app import config, events
from echo_app.bus import Injection, Task, TaskRequest, TaskResult
from echo_app.orchestrator import log as tasklog
from echo_app.orchestrator.ranker import rank
from echo_app.services import artifacts


SNAPSHOT_CAP = 5  # ambient [workspace] injections per finished task


def _compact(text, limit=500):
    """Squash a markdown file to one speakable line for an ambient injection."""
    joined = " / ".join(ln.strip() for ln in text.splitlines() if ln.strip())
    return joined[:limit] + ("…" if len(joined) > limit else "")


@dataclass
class WorkerContext:
    """What every worker gets alongside its Task (Contract B's ctx)."""
    workspace: Path
    fake_llm: bool = False
    extra: Dict[str, Any] = field(default_factory=dict)


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
        self._seq = 0
        self._running = set()

    @property
    def inbox(self):
        if self._inbox is None:
            self._inbox = asyncio.Queue()
        return self._inbox

    # -- Contract A entry points ------------------------------------------

    def submit(self, request):  # type: (TaskRequest) -> Task
        """Enqueue a task and return it immediately (never blocks the voice turn)."""
        self._seq += 1
        task = Task(id="t%d" % self._seq, request=request, created_at=time.time())
        self.tasks[task.id] = task
        tasklog.append_event(self.log_path, "queued", task_id=task.id,
                             kind=task.kind, instructions=request.instructions,
                             source=request.source)
        events.emit("task", task_id=task.id, kind=task.kind, status="queued")
        self.inbox.put_nowait(task)
        return task

    def summaries(self, task_id=None):
        """Compact status lines for the check_tasks tool."""
        tasks = [self.tasks[task_id]] if task_id in self.tasks else self.tasks.values()
        out = []
        for t in tasks:
            line = "%s %s: %s" % (t.id, t.kind, t.status)
            if t.result and t.result.say:
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

    # -- main loop ----------------------------------------------------------

    async def run(self):
        while True:
            task = await self.inbox.get()
            handle = asyncio.ensure_future(self._run_task(task))
            self._running.add(handle)
            handle.add_done_callback(self._running.discard)

    async def _run_task(self, task):  # type: (Task) -> None
        task.status = "running"
        events.emit("task", task_id=task.id, kind=task.kind, status="running")
        worker = self.registry.get(task.kind)
        try:
            if worker is None:
                raise KeyError("no worker registered for kind %r" % task.kind)
            result = await worker(task, self.ctx)
        except Exception as exc:
            result = TaskResult(say="Task %s (%s) failed: %s" % (task.id, task.kind, exc),
                                data={"error": str(exc),
                                      "traceback": traceback.format_exc()})
            task.status = "error"
        else:
            task.status = "done"
        task.result = result
        task.finished_at = time.time()
        priority = rank(result)
        tasklog.append_event(self.log_path, task.status, task_id=task.id,
                             kind=task.kind, say=result.say, priority=priority,
                             artifacts_touched=result.artifacts_touched,
                             follow_ups=[f.kind for f in result.follow_ups])
        events.emit("task", task_id=task.id, kind=task.kind,
                    status=task.status, say=result.say, priority=priority)
        for follow in result.follow_ups:  # generic chaining
            follow.source = "follow_up"
            self.submit(follow)
        if priority != "silent":  # silent -> task table only (check_tasks)
            self.on_injection(Injection(
                text="[task %s done] %s" % (task.id, result.say),
                priority=priority))
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
