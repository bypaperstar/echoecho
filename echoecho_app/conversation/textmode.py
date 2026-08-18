"""Real text mode: stdin REPL driving a Responses-API tool loop (Contract A).

Keyed: RealConversationLLM calls the OpenAI Responses API with the 4 tools.
Keyless (ECHOECHO_FAKE_LLM=1 or no key): FakeConversationLLM replays scripted
"rounds" from a JSON fixture through the *same* tool loop — a round with a
function_call must be followed by the round the model would produce after
seeing the function output.

REPL extras: "~wait N" sleeps N real seconds so background workers can land
(useful when piping a transcript), "/quit" exits.
"""
import asyncio
import json
import os
import time
from pathlib import Path

from echoecho_app import config, diagnostics, events
from echoecho_app.conversation.port import ConversationPort, TOOL_NAMES
from echoecho_app.conversation.session import Session

def build_tools():
    """The exact 4 Contract-A tools; the dispatch_task kind enum is generated
    from the worker registry (call after load_all())."""
    from echoecho_app.workers import base
    return [
        {"type": "function", "name": "dispatch_task",
         "description": "Queue background work; returns {task_id, status} "
                        "instantly. Kinds: %s." % (base.kinds_fragment()
                                                   or "(none)"),
         "parameters": {"type": "object", "properties": {
             "kind": {"type": "string", "enum": base.kinds_enum()},
             "instructions": {"type": "string"},
             "args": {"type": "object"}},
             "required": ["kind", "instructions"]}},
        {"type": "function", "name": "check_tasks",
         "description": "Status summaries for background tasks.",
         "parameters": {"type": "object", "properties": {
             "task_id": {"type": "string"}}}},
        {"type": "function", "name": "read_artifact",
         "description": "Read a text file from the workspace "
                        "(e.g. grocery.md or research/notes.md).",
         "parameters": {"type": "object", "properties": {
             "name": {"type": "string"}}, "required": ["name"]}},
        {"type": "function", "name": "end_session",
         "description": "End the session when the user is done.",
         "parameters": {"type": "object", "properties": {}}},
    ]


class RealConversationLLM:
    """One Responses API call per round; returns normalized item dicts."""

    def __init__(self, model=None):
        self.model = model or os.environ.get("ECHOECHO_TEXT_MODEL", "gpt-4o-mini")
        self._client = None

    async def turn(self, history):
        started = time.monotonic()
        diagnostics.info("text_llm.request.started", model=self.model,
                         history_items=len(history))
        phase = "client_init"
        try:
            if self._client is None:
                from openai import AsyncOpenAI  # lazy: keyless paths never import
                self._client = AsyncOpenAI()
            phase = "request"
            resp = await self._client.responses.create(
                model=self.model, instructions=config.system_prompt(),
                input=history, tools=build_tools())
        except Exception as exc:
            diagnostics.exception(
                "text_llm.request.failed", exc=exc, model=self.model,
                history_items=len(history), phase=phase,
                duration_ms=round((time.monotonic() - started) * 1000, 1))
            raise
        items = []
        for item in resp.output:
            if item.type == "function_call":
                items.append({"type": "function_call", "name": item.name,
                              "arguments": item.arguments,
                              "call_id": item.call_id})
            elif item.type == "message":
                text = "".join(c.text for c in item.content
                               if getattr(c, "type", "") == "output_text")
                items.append({"type": "message", "text": text})
        usage = getattr(resp, "usage", None)
        diagnostics.info(
            "text_llm.request.finished", model=self.model,
            duration_ms=round((time.monotonic() - started) * 1000, 1),
            output_items=len(items),
            tool_calls=sum(1 for i in items if i["type"] == "function_call"),
            input_tokens=getattr(usage, "input_tokens", None),
            output_tokens=getattr(usage, "output_tokens", None),
            response_id=getattr(resp, "id", None))
        return items


class FakeConversationLLM:
    """Replays a JSON fixture: a list of rounds, each a list of items
    ({"type": "message", "text": ...} or {"type": "function_call", "name": ...,
    "arguments": {...}}). Each turn() pops one round."""

    def __init__(self, script_path):
        self._rounds = json.loads(Path(script_path).read_text(encoding="utf-8"))
        self._calls = 0

    async def turn(self, history):
        if not self._rounds:
            diagnostics.warning("text_llm.fixture.exhausted",
                                calls=self._calls,
                                history_items=len(history))
            return [{"type": "message", "text": "(fake LLM: script exhausted)"}]
        out = []
        for item in self._rounds.pop(0):
            item = dict(item)
            if item["type"] == "function_call":
                self._calls += 1
                item.setdefault("call_id", "call_%d" % self._calls)
                if isinstance(item.get("arguments"), dict):
                    item["arguments"] = json.dumps(item["arguments"])
            out.append(item)
        diagnostics.info("text_llm.fixture.turn",
                         output_items=len(out), history_items=len(history),
                         tool_calls=sum(1 for i in out
                                        if i["type"] == "function_call"))
        return out


def default_llm():
    if config.echoecho_fake_llm() or not os.environ.get("OPENAI_API_KEY"):
        script = os.environ.get(
            "ECHOECHO_TEXT_FAKE_SCRIPT",
            str(config.FIXTURES_DIR / "textmode" / "demo2.json"))
        return FakeConversationLLM(script)
    return RealConversationLLM()


class TextRepl(ConversationPort):
    def __init__(self, session=None, out=print, llm=None, input_fn=None):
        self.out = out
        self.session = session or Session()
        self.session.on_state_change = (
            lambda old, new, reason: out("[state] %s -> %s (%s)" % (old, new, reason)))
        self.llm = llm
        self._input = input_fn or input
        self._tool_cb = None
        self._stop = False
        self.history = []

    # -- ConversationPort ---------------------------------------------------

    def on_tool(self, cb):
        self._tool_cb = cb

    def inject(self, injection):
        self.session.queue_injection(injection)
        self._drain()  # surface immediately if we're at a turn boundary

    async def end(self):
        self._stop = True

    async def run(self):
        if self.llm is None:
            self.llm = default_llm()
        self.out('echoecho text REPL — type "echoecho" to wake, /quit to exit.')
        diagnostics.info("text_repl.started", llm_type=type(self.llm).__name__)
        loop = asyncio.get_event_loop()
        while not self._stop:
            try:
                line = await loop.run_in_executor(None, self._input, "> ")
            except (EOFError, KeyboardInterrupt):
                break
            line = line.strip()
            await asyncio.sleep(0.05)  # let just-finished workers deliver
            if not line or line.startswith("#"):
                self._drain()
                continue
            if line == "/quit":
                break
            if line.startswith("~wait"):
                parts = line.split()
                try:
                    secs = float(parts[1]) if len(parts) > 1 else 0.5
                except ValueError:
                    secs = 0.5
                await asyncio.sleep(secs)
                self._drain()
                continue
            await self._handle(line)
        if self.session.state == "ACTIVE":
            self.session.begin_ending("quit")
        if self.session.state == "ENDING":
            self.session.finish()
        diagnostics.info("text_repl.finished", history_items=len(self.history),
                         state=self.session.state)

    # -- one user turn --------------------------------------------------------

    async def _handle(self, line):
        session = self.session
        diagnostics.info("text_repl.input", state=session.state,
                         input_chars=len(line), wake_phrase_present=(
                             config.WAKE_PHRASE in line.lower()))
        if session.state == "IDLE":
            if config.WAKE_PHRASE in line.lower():
                session.wake()
                self.history = []
                self.out("[echoecho] (chime) I'm listening.")
                events.emit("assistant_text", text="(chime) I'm listening.")
            else:
                self.out('(asleep — say "echoecho")')
            return
        self.out("[user] %s" % line)
        events.emit("user_text", text=line)
        session.note_user_speech_started()
        session.note_user_speech_stopped()
        session.handle_transcript(line)  # end-phrase regex belt-and-suspenders
        self.history.append({"role": "user", "content": line})
        await self._tool_loop()
        session.note_assistant_response_done()
        self._drain()
        session.check_silence()
        if session.state == "ENDING":
            self.out("[echoecho] Okay — talk soon.")
            events.emit("assistant_text", text="Okay — talk soon.")
            session.finish()

    async def _tool_loop(self):
        for round_index in range(8):  # sanity bound on rounds per user turn
            items = await self.llm.turn(self.history)
            calls = []
            for item in items:
                if item["type"] == "message":
                    self.out("[echoecho] %s" % item["text"])
                    events.emit("assistant_text", text=item["text"])
                    self.history.append({"role": "assistant",
                                         "content": item["text"]})
                elif item["type"] == "function_call":
                    self.history.append(item)
                    calls.append(item)
            if not calls:
                return
            for call in calls:
                args = call.get("arguments") or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args) if args.strip() else {}
                    except ValueError as exc:
                        safe_tool = (call.get("name") if call.get("name") in
                                     TOOL_NAMES else "unknown")
                        diagnostics.exception(
                            "text_repl.tool_arguments_invalid", exc=exc,
                            tool=safe_tool, argument_bytes=len(args),
                            round=round_index + 1)
                        args = {}
                self.out("[tool] %s %s" % (call["name"], json.dumps(args)))
                events.emit("tool_call", name=call["name"], args=args)
                result = self._tool_cb(call["name"], args) if self._tool_cb else {}
                self.out("[tool] -> %s" % json.dumps(result))
                self.history.append({"type": "function_call_output",
                                     "call_id": call["call_id"],
                                     "output": json.dumps(result)})
                if call["name"] == "end_session":
                    self.session.begin_ending("end_session_tool")
                    return
        diagnostics.warning("text_repl.tool_round_limit", max_rounds=8)

    def _drain(self):
        for inj in self.session.drain_injections():
            text = inj.text
            if text.startswith("[task"):
                text += " Weave in naturally."
            self.out("[inject/%s] %s" % (inj.priority, text))
            events.emit("injection", text=inj.text, priority=inj.priority)
            self.history.append({"role": "system", "content": text})
