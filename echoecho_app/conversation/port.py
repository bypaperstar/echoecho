"""Contract A: the conversation agent's interface to the rest of echoecho.

Down: exactly 4 tools surfaced to the model/agent —
  dispatch_task(kind, instructions) -> {"task_id", "status": "queued"}
  check_tasks(task_id?)             -> {"tasks": [summary, ...]}
  read_artifact(name)               -> {"name", "content"}
  end_session()                     -> {"status": "ending"}
Up: inject(Injection{text, priority}) at safe turn boundaries.
"""
from abc import ABC, abstractmethod

TOOL_NAMES = ("dispatch_task", "check_tasks", "read_artifact", "end_session")


class ConversationPort(ABC):
    @abstractmethod
    async def run(self):
        """Drive the conversation until the session ends."""

    @abstractmethod
    def inject(self, injection):
        """Queue an Injection to surface at the next safe turn boundary."""

    @abstractmethod
    def on_tool(self, cb):
        """Register cb(name: str, args: dict) -> dict handling the 4 tools."""

    @abstractmethod
    async def end(self):
        """Force the session toward ENDING/IDLE."""
