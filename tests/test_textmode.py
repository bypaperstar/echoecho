"""Text REPL with a fixture-scripted FakeConversationLLM: the full 4-tool
Contract A loop runs keyless — dispatch -> queued ack -> delayed result
injected with the 'Weave in naturally' wording + ambient workspace snapshot ->
read_artifact -> end_session -> back to IDLE."""
import asyncio
import json

import echoecho
from echoecho_app import config
from echoecho_app.bus import TaskResult
from echoecho_app.conversation.textmode import FakeConversationLLM, TextRepl
from echoecho_app.orchestrator.core import Orchestrator
from echoecho_app.services import artifacts

DOC = "# Doc\n\n## Goals\n- ship the demo\n"

ROUNDS = [
    [{"type": "function_call", "name": "dispatch_task",
      "arguments": {"kind": "doc.write", "instructions": "draft the goals"}}],
    [{"type": "message", "text": "On it — drafting the goals."}],
    [{"type": "function_call", "name": "read_artifact",
      "arguments": {"name": "doc.md"}}],
    [{"type": "message", "text": "Your goals: ship the demo."}],
    [{"type": "function_call", "name": "end_session", "arguments": {}}],
]


async def doc_write(task, ctx):
    await asyncio.sleep(0.05)
    artifacts.write_atomic(ctx.workspace, "doc.md", DOC)
    return TaskResult(say="Drafted the goals doc.", priority="interrupt",
                      artifacts_touched=["doc.md"])


def run_repl(tmp_path, monkeypatch, lines, rounds):
    monkeypatch.setattr(config, "WORKSPACE_DIR", tmp_path)  # read_artifact tool
    script = tmp_path / "script.json"
    script.write_text(json.dumps(rounds))
    queue = list(lines)

    def fake_input(prompt=""):
        if not queue:
            raise EOFError
        return queue.pop(0)

    out = []
    repl = TextRepl(out=out.append, llm=FakeConversationLLM(script),
                    input_fn=fake_input)
    orch = Orchestrator(registry={"doc.write": doc_write},
                        on_injection=repl.inject,
                        log_path=tmp_path / "tasks.jsonl", workspace=tmp_path)
    repl.on_tool(echoecho.make_tool_handler(orch, repl))

    async def go():
        loop_task = asyncio.ensure_future(orch.run())
        await repl.run()
        assert await orch.drain(2.0)
        loop_task.cancel()

    asyncio.run(go())
    return repl, out


def test_full_tool_loop_keyless(tmp_path, monkeypatch):
    repl, out = run_repl(
        tmp_path, monkeypatch,
        ["echoecho", "let's write the goals doc", "~wait 0.4",
         "read me the goals", "that's it"],
        ROUNDS)
    text = "\n".join(out)

    # dispatch acked instantly with a queued task id, then the model kept talking
    assert '[tool] dispatch_task {"kind": "doc.write"' in text
    assert '"task_id": "t1", "status": "queued"' in text
    assert "[echoecho] On it — drafting the goals." in text

    # delayed result injected at a turn boundary with the prompt wording
    assert ("[inject/interrupt] [task t1 done] Drafted the goals doc. "
            "Weave in naturally.") in text
    # ambient workspace snapshot so the agent can answer "read me the goals"
    assert "[inject/ambient] [workspace] doc.md now contains: " \
           "# Doc / ## Goals / - ship the demo" in text
    # injections also entered the LLM history as system context
    assert any(m.get("role") == "system" and "[workspace] doc.md" in m["content"]
               for m in repl.history)

    # read_artifact returned the real file content
    assert "ship the demo" in text.split("[tool] read_artifact")[1]

    # end_session tool ended the session cleanly
    assert repl.session.state == "IDLE"
    assert "[state] ACTIVE -> ENDING (end_phrase)" in text \
        or "[state] ACTIVE -> ENDING (end_session_tool)" in text
    assert "[state] ENDING -> IDLE" in text
    # injections happened strictly after the queued ack
    assert text.index('"status": "queued"') < text.index("[task t1 done]")


def test_script_exhausted_is_graceful(tmp_path, monkeypatch):
    repl, out = run_repl(tmp_path, monkeypatch,
                         ["echoecho", "hello there", "hello again"],
                         [[{"type": "message", "text": "Hi!"}]])
    text = "\n".join(out)
    assert "[echoecho] Hi!" in text
    assert "(fake LLM: script exhausted)" in text
    assert repl.session.state == "IDLE"  # EOF forced a clean quit
