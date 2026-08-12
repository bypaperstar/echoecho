"""AgentRuntime port: headless coding-agent CLIs behind one tiny adapter.

Echo rents the agent loop instead of building one (PLAN-GENERIC.md): a
runtime knows how to launch its CLI and how to normalize the CLI's streamed
JSONL into three event shapes the agent.run worker understands —
    {"event": "init",     "session_id": ...}
    {"event": "progress", "text": ...}
    {"event": "result",   "text": ..., "error": bool, "session_id": ...}
One adapter per CLI so flag/stream-format drift stays contained here, and a
FakeAgentCLI that replays recorded fixtures through `cat` — agent.run's real
subprocess + parse path runs keyless and Linux-only, same recipe that made
Realtime testable.
"""
import shutil
from pathlib import Path

from echo_app import config


class ClaudeCLI:
    """`claude -p --output-format stream-json` (Claude Code / Agent SDK)."""

    name = "claude"

    def command(self, prompt):
        # tier-1 policy: file edits inside cwd (= the workspace) auto-accept,
        # everything riskier stays deny-by-default in headless mode
        return ["claude", "-p", prompt, "--output-format", "stream-json",
                "--verbose", "--permission-mode", "acceptEdits"]

    def parse_event(self, ev):
        t = ev.get("type", "")
        if t == "system" and ev.get("subtype") == "init":
            return {"event": "init", "session_id": ev.get("session_id")}
        if t == "assistant":
            blocks = (ev.get("message") or {}).get("content") or []
            text = " ".join(b.get("text", "") for b in blocks
                            if isinstance(b, dict) and b.get("type") == "text")
            return {"event": "progress", "text": text.strip()} if text.strip() else None
        if t == "result":
            return {"event": "result", "text": ev.get("result") or "",
                    "error": bool(ev.get("is_error")),
                    "session_id": ev.get("session_id")}
        return None


class CodexCLI:
    """`codex exec --json`. Codex has no explicit result event: agent
    messages stream as progress and the worker takes the last one as the
    result when the process exits cleanly."""

    name = "codex"

    def command(self, prompt):
        # tier-1 policy: codex's own sandbox, writes fenced to the workspace
        return ["codex", "exec", "--sandbox", "workspace-write", "--json",
                prompt]

    def parse_event(self, ev):
        t = ev.get("type", "")
        if t == "thread.started":
            return {"event": "init", "session_id": ev.get("thread_id")}
        if t == "item.completed":
            item = ev.get("item") or {}
            if item.get("item_type", item.get("type")) == "agent_message":
                text = (item.get("text") or "").strip()
                return {"event": "progress", "text": text} if text else None
            return None
        if t == "error":
            return {"event": "result", "text": ev.get("message") or "agent error",
                    "error": True, "session_id": None}
        return None


class FakeAgentCLI(ClaudeCLI):
    """Replays a recorded claude-style stream-json fixture through `cat`, so
    agent.run exercises its real subprocess + line-parse path keyless.
    `source` is one JSONL file (replayed for every task) or a directory of
    them (consumed in sorted order, one file per task)."""

    name = "fake"

    def __init__(self, source):
        # resolve NOW: the subprocess runs with cwd=workspace/, so a relative
        # fixture path from the launcher's cwd would vanish
        self.source = Path(source).resolve()
        self._consumed = 0

    def command(self, prompt):
        script = self.source
        if script.is_dir():
            scripts = sorted(script.glob("*.jsonl"))
            if self._consumed >= len(scripts):
                raise RuntimeError("fake agent fixtures exhausted (%d used)"
                                   % self._consumed)
            script = scripts[self._consumed]
            self._consumed += 1
        return ["cat", str(script)]


def detect():
    """First installed CLI in preference order, or None (keyless sandboxes
    and Macs without an agent CLI: agent.run reports instead of crashing)."""
    for name, cls in (("claude", ClaudeCLI), ("codex", CodexCLI)):
        if shutil.which(name):
            return cls()
    return None


_fakes = {}  # ECHO_FAKE_AGENT_SCRIPT path -> shared FakeAgentCLI, so a
# directory fixture's consume-in-order counter survives across tasks


def for_ctx(ctx):
    """Pick the runtime for a WorkerContext: injected > fake-by-env > real."""
    injected = ctx.extra.get("agent_cli")
    if injected is not None:
        return injected
    script = config.fake_agent_script()
    if script:
        if script not in _fakes:
            _fakes[script] = FakeAgentCLI(script)
        return _fakes[script]
    return detect()
