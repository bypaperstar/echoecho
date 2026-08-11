"""doc.edit: LLM full-file markdown rewrite of a workspace doc (demo 1)."""
from echo_app.bus import TaskResult
from echo_app.services import artifacts
from echo_app.services import llm as llm_mod
from echo_app.workers.base import register

PROMPT = """You are editing a markdown document for a voice assistant's workspace.
Apply this instruction to the document: {instruction}

Current document (may be empty):
---
{current}
---
Return ONLY the complete updated markdown document, no commentary, no code fences."""


@register("doc.edit")
async def run(task, ctx):
    name = task.request.args.get("file", "doc.md")
    instruction = task.request.instructions or "start a draft"
    current = artifacts.read(ctx.workspace, name)
    llm = llm_mod.for_ctx(ctx)
    out = llm_mod.strip_fences(await llm.complete(
        "doc.edit", PROMPT.format(instruction=instruction, current=current)))
    artifacts.write_atomic(ctx.workspace, name, out + "\n")
    verb = "Started" if not current.strip() else "Updated"
    return TaskResult(
        say="%s %s: %s." % (verb, name, instruction),
        priority="interrupt",  # primary user-requested result
        data={"file": name, "chars": len(out)},
        artifacts_touched=[name])
