"""Typed contracts between the three layers (Contract A up, Contract B down)."""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class TaskRequest:
    kind: str
    instructions: str = ""
    args: Dict[str, Any] = field(default_factory=dict)
    source: str = "user"  # "user" | "follow_up"


@dataclass
class TaskResult:
    say: str = ""
    priority: str = "ambient"  # "interrupt" | "ambient" | "silent"
    data: Dict[str, Any] = field(default_factory=dict)
    follow_ups: List[TaskRequest] = field(default_factory=list)
    artifacts_touched: List[str] = field(default_factory=list)


@dataclass
class Task:
    id: str
    request: TaskRequest
    status: str = "queued"  # "queued" | "running" | "done" | "error"
    result: Optional[TaskResult] = None
    created_at: float = 0.0
    finished_at: Optional[float] = None
    # long-task plumbing (PR 11): a short spoken handle, the last progress
    # line a worker reported, and the agent session behind the task (what
    # makes it resumable — even across an echoecho restart)
    title: str = ""
    progress: Optional[str] = None
    session_id: Optional[str] = None

    @property
    def kind(self):
        return self.request.kind


@dataclass
class Injection:
    """Orchestrator -> conversation: a speech-ready line + how urgently to surface it."""
    text: str
    priority: str = "ambient"
