"""learn.outline + learn.deep_dive: Wikipedia-grounded notes.md (demo 3)."""
import asyncio
import re

from echo_app.bus import TaskResult
from echo_app.services import artifacts
from echo_app.services import llm as llm_mod
from echo_app.services import web as web_mod
from echo_app.workers.base import register

OUTLINE_PROMPT = """Write a markdown study outline about "{topic}" for someone learning by
conversation. Format: a '# Notes: {topic}' title, then EXACTLY 5 '## Section' headings,
each with one italic line describing what belongs there (sections start empty).
Ground it in this Wikipedia intro and these related subtopics.
Intro: {summary}
Subtopics: {subtopics}
Return ONLY the markdown, no commentary, no code fences."""

DEEP_DIVE_PROMPT = """Expand the study-notes section "{section}" using this Wikipedia extract.
Write 3-5 fact bullets, then a line '**Analogy:** ...' explaining it with an everyday
analogy, then a line '**Check yourself:** ...' with one quiz question.
Extract: {extract}
Return ONLY the markdown body (no heading), no code fences."""


def _web(ctx):
    return ctx.extra.get("web") or web_mod


def _sections(markdown):
    return [m.group(1).strip() for m in re.finditer(r"^## +(.+)$", markdown, re.M)]


@register("learn.outline")
async def run_outline(task, ctx):
    web = _web(ctx)
    topic = task.request.instructions or task.request.args.get("topic", "")
    titles = await asyncio.to_thread(web.wiki_opensearch, topic)
    main = titles[0] if titles else topic
    summary = await asyncio.to_thread(web.wiki_extract, main)
    subtopics = await asyncio.to_thread(web.wiki_search, topic)

    llm = llm_mod.for_ctx(ctx)
    out = llm_mod.strip_fences(await llm.complete("learn.outline", OUTLINE_PROMPT.format(
        topic=topic, summary=summary, subtopics=", ".join(subtopics))))
    source_titles = [main] + [t for t in subtopics if t != main][:4]
    if "## Sources" not in out:  # always end with linked sources
        out += "\n\n## Sources\n" + "\n".join(
            "- [%s](%s)" % (t, web_mod.wiki_url(t)) for t in source_titles)
    artifacts.write_atomic(ctx.workspace, "notes.md", out + "\n")

    sections = [s for s in _sections(out) if s != "Sources"]
    return TaskResult(
        say="I've put a %d-section outline on %s in your notes — say a section name to dive in." % (
            len(sections), topic),
        priority="interrupt",  # primary result: user asked to learn this
        data={"topic": topic, "sections": sections, "sources": source_titles},
        artifacts_touched=["notes.md"])


@register("learn.deep_dive")
async def run_deep_dive(task, ctx):
    web = _web(ctx)
    section = task.request.args.get("section") or task.request.instructions
    topic = task.request.args.get("topic", section)
    titles = await asyncio.to_thread(web.wiki_search, "%s %s" % (topic, section), 3)
    extract = ""
    if titles:
        extract = await asyncio.to_thread(web.wiki_extract, titles[0])

    llm = llm_mod.for_ctx(ctx)
    body = llm_mod.strip_fences(await llm.complete(
        "learn.deep_dive", DEEP_DIVE_PROMPT.format(section=section, extract=extract)))

    notes = artifacts.read(ctx.workspace, "notes.md")
    lines = notes.splitlines()
    start = next((i for i, l in enumerate(lines)
                  if l.startswith("## ") and section.lower() in l.lower()), None)
    if start is None:  # no matching heading: append a new section
        lines += ["", "## " + section, body]
    else:  # replace everything under the heading up to the next '## '
        end = next((i for i in range(start + 1, len(lines))
                    if lines[i].startswith("## ")), len(lines))
        lines[start + 1:end] = ["", body, ""]
    artifacts.write_atomic(ctx.workspace, "notes.md", "\n".join(lines).strip() + "\n")

    return TaskResult(
        say="Your notes now have a deeper section on %s, with an analogy and a check-yourself question." % section,
        priority="ambient",  # enrichment: weave into the tutor's next turn
        data={"section": section, "source": titles[0] if titles else None},
        artifacts_touched=["notes.md"])
