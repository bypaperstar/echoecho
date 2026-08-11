#!/usr/bin/env python3
"""Assertions for scripts/demo_check.sh (the merge gate).

Usage: python3 scripts/check_demo.py <1|2|3>
Checks the final workspace/*.md contents and the .tasks.jsonl event sequence
left behind by the corresponding fixtures/demoN.txt scripted run.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WS = ROOT / "workspace"


def read(name):
    return (WS / name).read_text(encoding="utf-8")


def events():
    lines = (WS / ".tasks.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(ln) for ln in lines if ln.strip()]


def seq(evs):
    return [(e["event"], e["kind"]) for e in evs]


def check(cond, msg):
    assert cond, msg
    print("  ok: %s" % msg)


def check_demo1():
    doc = read("doc.md")
    check("# Team Offsite in Lisbon" in doc, "doc.md has the proposal title")
    check("## Goals" in doc and "- Team bonding" in doc
          and "- Planning next year" in doc and "- Shipping the demo" in doc,
          "doc.md Goals section has the three goals")
    check("## Agenda" in doc and "Day 1" in doc and "Day 2" in doc,
          "doc.md has a two-day Agenda section")
    evs = events()
    check(seq(evs) == [("queued", "doc.edit"), ("done", "doc.edit")] * 3,
          ".tasks.jsonl: three queued->done doc.edit round trips, in order")
    for e in evs:
        if e["event"] == "done":
            check(e["priority"] == "interrupt", "%s spoke up (interrupt)" % e["task_id"])
            check(e["artifacts_touched"] == ["doc.md"], "%s touched doc.md" % e["task_id"])


def check_demo2():
    g = read("grocery.md")
    check("# Grocery List" in g, "grocery.md has the list title")
    check("## Meals" in g, "grocery.md keeps a ## Meals section")
    check(any(site in g for site in
              ("recipetineats.com", "pinchofyum.com", "bbcgoodfood.com")),
          "grocery.md links a recipe from the verified whitelist")
    evs = events()
    check(seq(evs) == [("queued", "recipe.search"), ("done", "recipe.search"),
                       ("queued", "grocery.merge"), ("done", "grocery.merge"),
                       ("queued", "grocery.merge"), ("done", "grocery.merge")],
          ".tasks.jsonl: search -> chained merge -> direct user edit, in order")
    check(evs[1]["priority"] == "interrupt"
          and evs[1]["follow_ups"] == ["grocery.merge"],
          "recipe.search result interrupts and chains grocery.merge")
    check("Found a" in evs[1]["say"] and "ingredients" in evs[1]["say"],
          "recipe.search say-line is speech-ready with a count")
    check(evs[2]["source"] == "follow_up", "chained merge was enqueued as a follow_up")
    check(evs[3]["priority"] == "ambient", "chained merge is ambient enrichment")
    check(evs[3]["artifacts_touched"] == ["grocery.md"], "merge touched grocery.md")
    check(evs[4]["source"] == "user" and evs[5]["priority"] == "interrupt",
          "direct user edit interrupts")


def check_demo3():
    n = read("notes.md")
    check(n.startswith("# Notes: fermentation in food"), "notes.md has the title")
    check("## Sourdough" in n, "notes.md has the Sourdough section")
    check("**Analogy:**" in n and "**Check yourself:**" in n,
          "deep dive filled in an analogy + quiz question")
    check("## Sources" in n and "wikipedia.org" in n,
          "notes.md ends with live Wikipedia sources")
    evs = events()
    check(seq(evs) == [("queued", "learn.outline"), ("done", "learn.outline"),
                       ("queued", "learn.deep_dive"), ("done", "learn.deep_dive")],
          ".tasks.jsonl: outline then deep dive, in order")
    check(evs[1]["priority"] == "interrupt", "outline result speaks up")
    check(evs[3]["priority"] == "ambient", "deep dive weaves in ambiently")
    check(evs[3]["artifacts_touched"] == ["notes.md"], "deep dive touched notes.md")


def main():
    n = sys.argv[1]
    {"1": check_demo1, "2": check_demo2, "3": check_demo3}[n]()
    print("demo %s assertions all passed" % n)


if __name__ == "__main__":
    main()
