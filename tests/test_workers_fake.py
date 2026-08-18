"""All five workers end-to-end, fully offline: FakeLLM fixtures + FakeWeb,
real files in a tmp workspace, atomic writes, generic follow_ups chaining."""
import asyncio
import json
import os
from pathlib import Path

import pytest

from echoecho_app.bus import TaskRequest
from echoecho_app.orchestrator.core import Orchestrator, WorkerContext
from echoecho_app.services import artifacts
from echoecho_app.services.llm import FakeLLM, LLMUnavailable, RealLLM, for_ctx
from echoecho_app.workers.base import load_all

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"

PAD_THAI_URL = "https://www.recipetineats.com/chicken-pad-thai/"
SLOW_URL = "https://pinchofyum.com/slow-braised-noodles"


class FakeWeb:
    """Offline stand-in for services.web, injected via ctx.extra['web']."""

    pages = {
        PAD_THAI_URL: FIXTURES / "web" / "recipetineats_pad_thai.html",
        SLOW_URL: FIXTURES / "web" / "pinchofyum_slow_noodles.html",
    }

    def __init__(self):
        self.calls = []

    def wp_search(self, query, sites=None, per_page=3):
        self.calls.append(("wp_search", query))
        return [{"title": "Slow Braised Noodles", "url": SLOW_URL},
                {"title": "Pad Thai", "url": PAD_THAI_URL}]

    def ddg_search(self, query, site=None, limit=10):
        self.calls.append(("ddg_search", query))
        return []

    def fetch(self, url, timeout=20):
        self.calls.append(("fetch", url))
        return self.pages[url].read_text(encoding="utf-8")

    def wiki_opensearch(self, query, limit=5):
        return ["Fermentation in food processing", "Fermentation"]

    def wiki_search(self, query, limit=8):
        return ["Fermentation in food processing", "Sourdough",
                "List of fermented foods", "Kōji (food)", "Lactic acid fermentation"]

    def wiki_extract(self, title, chars=1500):
        return "Fermentation in food processing is the conversion of carbohydrates " \
               "to alcohol or organic acids using microorganisms."


def run_orch(requests, tmp_path, extra=None, registry=None, timeout=5.0):
    injections = []
    orch = Orchestrator(registry=registry or load_all(), on_injection=injections.append,
                        log_path=tmp_path / "tasks.jsonl", workspace=tmp_path,
                        fake_llm=True)
    orch.ctx.extra.update(extra or {})

    async def go():
        loop_task = asyncio.ensure_future(orch.run())
        for req in requests:
            orch.submit(req)
        assert await orch.drain(timeout), "orchestrator did not drain in time"
        loop_task.cancel()

    asyncio.run(go())
    return orch, injections


# -- doc.edit ----------------------------------------------------------------

def test_doc_edit_end_to_end(tmp_path):
    orch, injections = run_orch(
        [TaskRequest(kind="doc.edit", instructions="add three goals")], tmp_path)
    task = orch.tasks["t1"]
    assert task.status == "done"
    content = (tmp_path / "doc.md").read_text()
    assert "## Goals" in content and "Team bonding" in content
    assert task.result.artifacts_touched == ["doc.md"]
    assert injections[0].priority == "interrupt"
    assert "add three goals" in injections[0].text


def test_doc_edit_infers_filename_from_instructions(tmp_path):
    # voice models name the doc in prose instead of args.file; the first
    # filename mentioned wins over the doc.md default
    orch, _ = run_orch(
        [TaskRequest(kind="doc.edit",
                     instructions="add three goals to speech.md please")],
        tmp_path)
    assert orch.tasks["t1"].result.artifacts_touched == ["speech.md"]
    assert (tmp_path / "speech.md").is_file()
    assert not (tmp_path / "doc.md").exists()
    # an explicit args.file still beats anything mentioned in prose
    orch, _ = run_orch(
        [TaskRequest(kind="doc.edit", instructions="update notes.md",
                     args={"file": "actual.md"})], tmp_path)
    assert orch.tasks["t1"].result.artifacts_touched == ["actual.md"]


# -- recipe.search -> grocery.merge chaining ---------------------------------

def test_recipe_search_chains_grocery_merge(tmp_path):
    web = FakeWeb()
    orch, injections = run_orch(
        [TaskRequest(kind="recipe.search", instructions="pad thai")],
        tmp_path, extra={"web": web})
    t1 = orch.tasks["t1"]
    assert t1.status == "done"
    # picked the FASTER recipe (30 min pad thai) over the 90-min first hit
    assert t1.result.data["url"] == PAD_THAI_URL
    assert t1.result.data["minutes"] == 30
    assert len(t1.result.data["ingredients"]) == 9
    assert "30-minute" in t1.result.say and "9 ingredients" in t1.result.say
    assert injections[0].priority == "interrupt"
    # chaining happened purely via generic follow_ups
    assert [f.kind for f in t1.result.follow_ups] == ["grocery.merge"]
    t2 = orch.tasks["t2"]
    assert t2.kind == "grocery.merge"
    assert t2.request.source == "follow_up"
    assert t2.status == "done"
    grocery = (tmp_path / "grocery.md").read_text()
    assert "## Meals" in grocery and "Pad Thai" in grocery
    # chained enrichment is ambient, not interrupt
    assert injections[1].priority == "ambient"


def test_recipe_search_honors_max_minutes(tmp_path):
    orch, _ = run_orch(
        [TaskRequest(kind="recipe.search", instructions="noodles",
                     args={"max_minutes": 120})],
        tmp_path, extra={"web": FakeWeb()})
    # both qualify under 120 min; still picks the fastest
    assert orch.tasks["t1"].result.data["minutes"] == 30


def test_recipe_search_no_results_is_interrupt(tmp_path):
    class EmptyWeb(FakeWeb):
        def wp_search(self, query, sites=None, per_page=3):
            return []

    orch, injections = run_orch(
        [TaskRequest(kind="recipe.search", instructions="unobtainium stew")],
        tmp_path, extra={"web": EmptyWeb()})
    assert orch.tasks["t1"].result.data.get("error")
    assert injections[0].priority == "interrupt"


# -- grocery.merge -----------------------------------------------------------

def test_grocery_merge_llm_path_writes_meals_section(tmp_path):
    orch, injections = run_orch(
        [TaskRequest(kind="grocery.merge", instructions="add pad thai items",
                     args={"ingredients": ["2 cloves garlic", "1 lime"]})],
        tmp_path)
    grocery = (tmp_path / "grocery.md").read_text()
    assert "## Meals" in grocery and "## Produce" in grocery
    # direct user-requested merge is a primary result
    assert injections[0].priority == "interrupt"


def test_grocery_merge_regex_fallback_dedupes(tmp_path):
    (tmp_path / "grocery.md").write_text(
        "# Grocery List\n\n- Garlic\n- lime\n\n## Meals\n")
    no_fixtures_llm = FakeLLM(fixtures_dir=tmp_path / "empty")  # -> LLMUnavailable
    orch, injections = run_orch(
        [TaskRequest(kind="grocery.merge", instructions="",
                     args={"ingredients": ["2 cloves garlic", "Lime", "fish sauce"],
                           "source_recipe": {"title": "Pad Thai",
                                             "url": PAD_THAI_URL, "minutes": 30}})],
        tmp_path, extra={"llm": no_fixtures_llm})
    grocery = (tmp_path / "grocery.md").read_text()
    # quantity-stripped + case-insensitive dedup: only fish sauce is new
    assert grocery.count("arlic") == 1 and grocery.lower().count("lime") == 1
    assert "- fish sauce" in grocery
    assert "[Pad Thai](%s)" % PAD_THAI_URL in grocery
    assert "Added 1 items" in orch.tasks["t1"].result.say
    assert "2 you already had" in orch.tasks["t1"].result.say


# -- learn.outline / learn.deep_dive -----------------------------------------

def test_learn_outline_writes_notes_with_sources(tmp_path):
    orch, injections = run_orch(
        [TaskRequest(kind="learn.outline", instructions="fermentation in food")],
        tmp_path, extra={"web": FakeWeb()})
    notes = (tmp_path / "notes.md").read_text()
    assert notes.startswith("# Notes:")
    assert "## Sources" in notes
    assert "https://en.wikipedia.org/wiki/" in notes
    sections = orch.tasks["t1"].result.data["sections"]
    assert len(sections) == 5 and "Sourdough" in sections
    assert injections[0].priority == "interrupt"


def test_learn_deep_dive_fills_existing_section(tmp_path):
    web = FakeWeb()
    run_orch([TaskRequest(kind="learn.outline", instructions="fermentation in food")],
             tmp_path, extra={"web": web})
    orch, injections = run_orch(
        [TaskRequest(kind="learn.deep_dive", instructions="Sourdough",
                     args={"topic": "fermentation", "section": "Sourdough"})],
        tmp_path, extra={"web": web})
    notes = (tmp_path / "notes.md").read_text()
    assert notes.count("## Sourdough") == 1  # filled in place, not appended
    assert "**Analogy:**" in notes and "**Check yourself:**" in notes
    # deep dive body lives under the Sourdough heading, before the next section
    body = notes.split("## Sourdough")[1].split("##")[0]
    assert "wild yeast" in body
    dive = [i for i in injections if "Sourdough" in i.text][0]
    assert dive.priority == "ambient"


def test_learn_deep_dive_appends_missing_section(tmp_path):
    (tmp_path / "notes.md").write_text("# Notes: fermentation\n\n## Koji\n")
    orch, _ = run_orch(
        [TaskRequest(kind="learn.deep_dive", instructions="Sourdough")],
        tmp_path, extra={"web": FakeWeb()})
    notes = (tmp_path / "notes.md").read_text()
    assert "## Sourdough" in notes and "**Check yourself:**" in notes


# -- artifacts: atomic writes ------------------------------------------------

def test_write_atomic_full_content_at_rename(tmp_path, monkeypatch):
    seen = {}
    real_rename = os.rename

    def spying_rename(src, dst):
        seen["src_content"] = Path(src).read_text()
        seen["dst"] = dst
        real_rename(src, dst)

    monkeypatch.setattr("echoecho_app.services.artifacts.os.rename", spying_rename)
    content = "# Doc\n\n" + "line\n" * 100
    target = artifacts.write_atomic(tmp_path, "doc.md", content)
    # tmp file already held the FULL content when it was renamed into place
    assert seen["src_content"] == content
    assert seen["dst"] == str(target) == str(tmp_path / "doc.md")
    assert target.read_text() == content
    # no tmp litter left behind
    assert [p.name for p in tmp_path.iterdir()] == ["doc.md"]
    # overwrite is atomic too
    artifacts.write_atomic(tmp_path, "doc.md", "v2")
    assert (tmp_path / "doc.md").read_text() == "v2"
    assert [p.name for p in tmp_path.iterdir()] == ["doc.md"]


def test_artifacts_read_list_mtime(tmp_path):
    assert artifacts.read(tmp_path, "absent.md") == ""
    assert artifacts.mtime(tmp_path, "absent.md") == 0.0
    artifacts.write_atomic(tmp_path, "a.md", "x")
    artifacts.write_atomic(tmp_path, "b.md", "y")
    (tmp_path / ".hidden").write_text("z")
    assert artifacts.list_files(tmp_path) == ["a.md", "b.md"]
    assert artifacts.mtime(tmp_path, "a.md") > 0


# -- web: wp_search resilience -------------------------------------------------

def test_web_diagnostics_allowlist_public_hosts_and_fingerprint_unknown_ones():
    from echoecho_app.services import web

    assert web._diagnostic_host_fields("en.wikipedia.org") == {
        "host": "en.wikipedia.org"}
    fields = web._diagnostic_host_fields("private-customer-canary.example")
    assert fields["host"] == "unknown"
    assert fields["host_length"] == len("private-customer-canary.example")
    assert len(fields["host_fingerprint"]) == 16
    assert "private-customer-canary" not in repr(fields)


def test_wp_search_skips_site_returning_json_error(monkeypatch):
    from echoecho_app.services import web

    def fake_fetch(url, timeout=20):
        if "recipetineats" in url:  # WP outage: JSON error object, not a list
            return '{"code": "rest_no_route", "message": "nope"}'
        return '[{"title": "Pad <b>Thai</b>", "url": "https://pinchofyum.com/x"}]'

    monkeypatch.setattr(web, "fetch", fake_fetch)
    assert web.wp_search("pad thai") == [
        {"title": "Pad Thai", "url": "https://pinchofyum.com/x"}]


# -- LLM selection -----------------------------------------------------------

def test_fake_llm_selected_by_env(monkeypatch, tmp_path):
    monkeypatch.setenv("ECHOECHO_FAKE_LLM", "1")
    ctx = WorkerContext(workspace=tmp_path)
    assert isinstance(for_ctx(ctx), FakeLLM)
    monkeypatch.delenv("ECHOECHO_FAKE_LLM")
    assert isinstance(for_ctx(ctx), RealLLM)
    ctx.extra["llm"] = FakeLLM()
    assert for_ctx(ctx) is ctx.extra["llm"]


def test_real_llm_keyless_construct_ok_call_raises(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    llm = RealLLM()  # lazy client: constructing keyless must not fail
    with pytest.raises(LLMUnavailable):
        asyncio.run(llm.complete("doc.edit", "hello"))


def test_fake_llm_reads_fixture_by_kind():
    out = asyncio.run(FakeLLM().complete("doc.edit", "ignored"))
    assert "## Goals" in out
    expected = json.loads((FIXTURES / "llm" / "doc.edit.json").read_text())["output"]
    assert out == expected
