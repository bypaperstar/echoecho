"""Keyless scripted conversation agent for sandbox CI / smoke runs.

Plays a fixture script standing in for both the user and the LLM's tool-call
decisions, driving the real Session FSM + orchestrator. Script line format:
    # comment / blank        ignored
    echoecho                wake phrase (while IDLE)
    !tool_name {json args}   the agent "decides" to call one of the 4 tools
    ~wait 1.5                let background workers finish (real seconds)
    anything else            a plain user utterance (end phrases end the session)
"""
import asyncio
import json

from echoecho_app import config, events
from echoecho_app.conversation.port import ConversationPort, TOOL_NAMES
from echoecho_app.conversation.session import Session


class ScriptedAgent(ConversationPort):
    def __init__(self, script_path, session=None, out=print):
        self.script_path = script_path
        self.out = out
        self.session = session or Session()
        self.session.on_state_change = self._on_state_change
        self._tool_cb = None

    # -- ConversationPort ---------------------------------------------------

    def on_tool(self, cb):
        self._tool_cb = cb

    def inject(self, injection):
        self.session.queue_injection(injection)

    async def end(self):
        if self.session.begin_ending("forced"):
            self.session.finish()

    async def run(self):
        with open(self.script_path) as f:
            lines = [ln.rstrip("\n") for ln in f]
        for raw in lines:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            await self._step(line)
        if self.session.state == "ACTIVE":  # script ended without an end phrase
            self.session.begin_ending("script_eof")
        if self.session.state == "ENDING":
            self.out("[echoecho] Bye!")
            self.session.finish()

    # -- script interpretation ----------------------------------------------

    async def _step(self, line):
        session = self.session
        if session.state == "IDLE":
            if config.WAKE_PHRASE in line.lower():
                self.out('[wake] heard "%s" — wake feed paused, chime' % config.WAKE_PHRASE)
                session.wake()
            else:
                self.out("[idle] (ignored: %s)" % line)
            return
        if line.startswith("~wait"):
            secs = float(line.split()[1]) if len(line.split()) > 1 else 0.5
            await asyncio.sleep(secs)
            self._drain()  # still at the previous turn boundary
            return
        if line.startswith("!"):
            name, _, rest = line[1:].partition(" ")
            args = json.loads(rest) if rest.strip() else {}
            self._call_tool(name, args)
            return
        # plain user utterance
        self.out("[user] %s" % line)
        events.emit("user_text", text=line)
        session.note_user_speech_started()
        session.note_user_speech_stopped()
        if session.handle_transcript(line):
            self.out("[echoecho] Okay — talk soon.")
            events.emit("assistant_text", text="Okay — talk soon.")
            session.finish()
            return
        self.out("[echoecho] (chats back)")
        events.emit("assistant_text", text="(chats back)")
        session.note_assistant_response_done()
        self._drain()
        session.check_silence()

    def _call_tool(self, name, args):
        assert name in TOOL_NAMES, "unknown tool %r" % name
        self.out("[tool] %s %s" % (name, json.dumps(args)))
        events.emit("tool_call", name=name, args=args)
        result = self._tool_cb(name, args) if self._tool_cb else {}
        self.out("[tool] -> %s" % json.dumps(result))
        if name == "dispatch_task":
            ack = ("On it — task %s is queued; I'll keep talking."
                   % result.get("task_id"))
            self.out("[echoecho] %s" % ack)
            events.emit("assistant_text", text=ack)
        if name == "end_session":
            self.session.begin_ending("end_session_tool")
            return
        self.session.note_assistant_response_done()
        self._drain()

    def _drain(self):
        for inj in self.session.drain_injections():
            self.out("[gate] turn boundary — injecting (%s) %s"
                     % (inj.priority, inj.text))
            events.emit("injection", text=inj.text, priority=inj.priority)
            if inj.priority == "interrupt":
                self.out("[echoecho] (speaks up) %s"
                         % inj.text.split("] ", 1)[-1])
                events.emit("assistant_text",
                            text=inj.text.split("] ", 1)[-1])

    def _on_state_change(self, old, new, reason):
        self.out("[state] %s -> %s (%s)" % (old, new, reason))
        if new == "IDLE":
            self.out("[wake] wake feed resumed — listening for \"echoecho\"")
