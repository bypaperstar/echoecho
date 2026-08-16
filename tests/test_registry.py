"""PR 10 registry: workers declare themselves once (kind, description, arg
schema); the dispatch_task enum + system-prompt kinds line are generated from
REGISTRY; discovery is a package scan (core strict, plugins lenient); plugin
kinds stay dispatchable but are advertised only with ECHOECHO_PLUGINS=1."""
import sys
import textwrap

import pytest

from echoecho_app import config
from echoecho_app.conversation.textmode import build_tools
from echoecho_app.workers import base

PLUGIN_KINDS = {"recipe.search", "grocery.merge",
                "learn.outline", "learn.deep_dive"}


@pytest.fixture(autouse=True)
def clean_registry(monkeypatch):
    """Restore the fully-loaded REGISTRY and hide ECHOECHO_PLUGINS. Snapshot
    AFTER load_all(): module imports are cached, so a bare restore of a
    partial registry would leave later tests' load_all() a no-op."""
    monkeypatch.delenv("ECHOECHO_PLUGINS", raising=False)
    base.load_all()
    before = dict(base.REGISTRY)
    yield
    base.REGISTRY.clear()
    base.REGISTRY.update(before)


def test_load_all_discovers_workers_and_plugins():
    registry = base.load_all()
    assert "agent.run" in registry            # core generic worker
    assert "sleep.echoecho" in registry           # core smoke worker
    assert "doc.edit" in registry             # core fast path (live cowrite)
    assert PLUGIN_KINDS <= set(registry)      # demo kinds live on as plugins
    assert "code" not in registry             # code_stub superseded by agent.run


def test_worker_metadata_attached_by_register():
    base.load_all()
    fn = base.REGISTRY["agent.run"]
    assert fn.kind == "agent.run"
    assert "agent" in fn.description
    assert "task_id" in fn.arg_schema  # steering/resume arg (PR 11)
    assert fn.advertise and not fn.is_plugin
    plug = base.REGISTRY["recipe.search"]
    assert plug.is_plugin and plug.advertise
    doc = base.REGISTRY["doc.edit"]
    assert not doc.is_plugin              # promoted: the live-cowrite fast path
    assert doc.advertise_when is not None  # advertised only when its LLM can run
    assert doc.serialize == "workspace.write"  # shared write lock w/ agent.run
    assert "file" in doc.arg_schema


def test_advertised_hides_plugins_and_unadvertised_kinds(monkeypatch):
    base.load_all()
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ECHOECHO_FAKE_LLM", raising=False)
    assert base.kinds_enum() == ["agent.run"]  # sleep.echoecho + plugins hidden
    monkeypatch.setenv("ECHOECHO_PLUGINS", "1")
    kinds = base.kinds_enum()
    assert "agent.run" in kinds and PLUGIN_KINDS <= set(kinds)
    assert kinds == sorted(kinds)
    assert "sleep.echoecho" not in kinds           # advertise=False is absolute
    assert "doc.edit" not in kinds             # its LLM can't run keyless


def test_doc_edit_advertised_whenever_its_llm_can_run(monkeypatch):
    # the live-cowrite fast path needs no plugin flag — just a usable LLM
    base.load_all()
    monkeypatch.delenv("ECHOECHO_PLUGINS", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert "doc.edit" in base.kinds_enum()
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("ECHOECHO_FAKE_LLM", "1")   # keyless fixture runs too
    assert "doc.edit" in base.kinds_enum()


def test_prompt_and_tools_are_generated_from_registry(monkeypatch):
    base.load_all()
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)  # hides doc.edit
    monkeypatch.delenv("ECHOECHO_FAKE_LLM", raising=False)
    fragment = base.kinds_fragment()
    assert fragment.startswith("agent.run (")
    prompt = config.system_prompt()
    assert fragment in prompt
    dispatch = build_tools()[0]
    assert fragment in dispatch["description"]
    assert dispatch["parameters"]["properties"]["kind"]["enum"] == ["agent.run"]
    # flipping the plugin flag changes every generated surface at once
    monkeypatch.setenv("ECHOECHO_PLUGINS", "1")
    assert "recipe.search" in config.system_prompt()
    enum = build_tools()[0]["parameters"]["properties"]["kind"]["enum"]
    assert "grocery.merge" in enum


def test_register_decorator_still_contract_b():
    @base.register("t.custom", description="a test kind")
    async def worker(task, ctx):
        return None

    assert base.REGISTRY["t.custom"] is worker
    assert "t.custom" in base.kinds_enum()

    @base.register("t.hidden", advertise=False)
    async def hidden(task, ctx):
        return None

    assert "t.hidden" in base.REGISTRY
    assert "t.hidden" not in base.kinds_enum()


def test_broken_plugin_is_skipped_not_fatal(tmp_path, monkeypatch, capsys):
    pkg = tmp_path / "fakeplugpkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "broken.py").write_text("raise RuntimeError('bad plugin')\n")
    (pkg / "good.py").write_text(textwrap.dedent("""
        from echoecho_app.workers.base import register

        @register("t.good", description="works")
        async def run(task, ctx):
            return None
    """))
    monkeypatch.syspath_prepend(str(tmp_path))
    base._scan("fakeplugpkg", required=False)
    assert "t.good" in base.REGISTRY
    assert "broken" in capsys.readouterr().out  # reported, not raised
    for mod in list(sys.modules):
        if mod.startswith("fakeplugpkg"):
            del sys.modules[mod]


def test_missing_plugins_package_is_fine():
    base._scan("echoecho_app.no_such_package", required=False)  # no raise
    with pytest.raises(ImportError):
        base._scan("echoecho_app.no_such_package", required=True)
