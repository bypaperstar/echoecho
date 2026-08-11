"""Bare stdin REPL stub (PR 3 upgrades this to a real Responses-API tool loop).

Type "echo echo" to wake, "/dispatch <kind> <instructions...>" to fire a task,
"/tasks" to check them, an end phrase ("that's it") to end, "/quit" to exit.
"""
import asyncio

from echo_app import config
from echo_app.conversation.port import ConversationPort
from echo_app.conversation.session import Session


class TextRepl(ConversationPort):
    def __init__(self, session=None, out=print):
        self.out = out
        self.session = session or Session()
        self.session.on_state_change = (
            lambda old, new, reason: out("[state] %s -> %s (%s)" % (old, new, reason)))
        self._tool_cb = None
        self._stop = False

    def on_tool(self, cb):
        self._tool_cb = cb

    def inject(self, injection):
        self.session.queue_injection(injection)
        # Bare stub: print immediately if we're at a turn boundary already.
        self._drain()

    async def end(self):
        self._stop = True

    async def run(self):
        self.out('Echo text REPL — type "echo echo" to wake, /quit to exit.')
        loop = asyncio.get_event_loop()
        while not self._stop:
            try:
                line = await loop.run_in_executor(None, input, "> ")
            except (EOFError, KeyboardInterrupt):
                break
            await asyncio.sleep(0.05)  # let just-finished workers deliver
            self._handle(line.strip())
        if self.session.state == "ACTIVE":
            self.session.begin_ending("quit")
        if self.session.state == "ENDING":
            self.session.finish()

    def _handle(self, line):
        session = self.session
        if not line:
            self._drain()
            return
        if line == "/quit":
            self._stop = True
            return
        if session.state == "IDLE":
            if config.WAKE_PHRASE in line.lower():
                session.wake()
            else:
                self.out('(asleep — say "echo echo")')
            return
        session.note_user_speech_started()
        session.note_user_speech_stopped()
        if line.startswith("/dispatch "):
            parts = line.split(" ", 2)
            kind = parts[1]
            instructions = parts[2] if len(parts) > 2 else ""
            result = self._tool_cb("dispatch_task",
                                   {"kind": kind, "instructions": instructions})
            self.out("[echo] queued %s" % result.get("task_id"))
        elif line == "/tasks":
            result = self._tool_cb("check_tasks", {})
            for t in result.get("tasks", []):
                self.out("  " + t)
        elif session.handle_transcript(line):
            self.out("[echo] Okay — talk soon.")
            session.finish()
            return
        else:
            self.out("[echo] (heard you; real LLM lands in PR 3)")
        session.note_assistant_response_done()
        self._drain()
        session.check_silence()

    def _drain(self):
        for inj in self.session.drain_injections():
            self.out("[inject/%s] %s" % (inj.priority, inj.text))
