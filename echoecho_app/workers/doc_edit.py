"""doc.edit: LLM full-file markdown rewrite of a workspace doc.

Core (not a plugin) since the live-cowrite playtests: dictation and doc
co-writing need edits that land in seconds, and agent.run's 30-90s per edit
made "write with me while I talk" feel broken. Advertised whenever the
worker LLM can actually run; agent.run stays the path for anything beyond
one markdown file."""
import os
import re

from echoecho_app import config
from echoecho_app.bus import TaskResult
from echoecho_app.services import artifacts
from echoecho_app.services import llm as llm_mod
from echoecho_app.workers.base import register

PROMPT = """You are editing a markdown document for a voice assistant's workspace.
Apply this instruction to the document: {instruction}

Current document (may be empty):
---
{current}
---
Return ONLY the complete updated markdown document, no commentary, no code fences."""


def _llm_available():
    return bool(os.environ.get("OPENAI_API_KEY")) or config.echoecho_fake_llm()


FILENAME_RE = re.compile(r"\b([\w./-]+\.(?:md|markdown|txt))\b")


@register("doc.edit",
          description="fast rewrite of ONE workspace markdown doc — the "
                      "seconds-quick path for live co-writing and dictation; "
                      "args.file names the doc, instructions carry the full "
                      "edit (use agent.run for anything bigger)",
          arg_schema={"file": {"type": "string",
                               "description": "workspace file, e.g. doc.md"}},
          advertise_when=_llm_available,
          serialize="workspace.write")  # same group as agent.run: no races
async def run(task, ctx):
    name = task.request.args.get("file")
    instruction = task.request.instructions or "start a draft"
    if not name:
        # voice models often name the doc in prose ("...named speech.md")
        # instead of args.file; honor the first filename mentioned rather
        # than silently writing doc.md (playtest: read-back then 404s)
        match = FILENAME_RE.search(instruction)
        name = match.group(1) if match else "doc.md"
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
