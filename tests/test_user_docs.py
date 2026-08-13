"""PR 13 mediated user documents: the outbox.apply worker is the ONLY path
that writes real documents — it re-validates the agent's manifest against the
shared-folder allowlist, backs up originals, and refuses anything outside.
Plus: read-only VM mounts, the guest outbox convention, conditional
advertisement, and the voice approval guidance."""
import asyncio
import json
from pathlib import Path

from echoecho_app import config
from echoecho_app.bus import Task, TaskRequest
from echoecho_app.orchestrator.core import Orchestrator, WorkerContext
from echoecho_app.services import artifacts
from echoecho_app.services.vm import LumeVM
from echoecho_app.workers import base
from echoecho_app.workers.agent_run import _user_docs_convention
from echoecho_app.workers.base import load_all
from echoecho_app.workers.outbox import run_apply


def run_task(kind, tmp_path, args=None, workspace=None):
    ws = workspace or (tmp_path / "ws")
    ws.mkdir(exist_ok=True)
    orch = Orchestrator(registry=load_all(), log_path=tmp_path / "t.jsonl",
                        workspace=ws)

    async def go():
        loop = asyncio.ensure_future(orch.run())
        orch.submit(TaskRequest(kind=kind, instructions="apply", args=args or {}))
        assert await orch.drain()
        loop.cancel()

    asyncio.run(go())
    return orch.tasks["t1"]


def stage(ws, task_id, entries, files, changes="# Changes\n"):
    """Write a staged outbox batch: files {relpath: content}, MANIFEST, CHANGES."""
    box = "%s/%s" % (config.OUTBOX_DIR, task_id)
    for rel, content in files.items():
        artifacts.write_atomic(ws, "%s/%s" % (box, rel), content)
    artifacts.write_atomic(ws, "%s/MANIFEST.json" % box, json.dumps(entries))
    artifacts.write_atomic(ws, "%s/CHANGES.md" % box, changes)


# -- config -------------------------------------------------------------------

def test_user_docs_parses_and_expands(monkeypatch, tmp_path):
    d1, d2 = tmp_path / "Documents", tmp_path / "Desktop"
    d1.mkdir(); d2.mkdir()
    monkeypatch.setenv("ECHOECHO_USER_DOCS", "%s%s%s" % (d1, __import__("os").pathsep, d2))
    assert config.user_docs() == [d1, d2]
    monkeypatch.delenv("ECHOECHO_USER_DOCS")
    assert config.user_docs() == []


# -- outbox.apply: the mediation gate -----------------------------------------

def test_apply_writes_over_original_with_backup(tmp_path, monkeypatch):
    docs = tmp_path / "Documents"; docs.mkdir()
    original = docs / "lease.md"
    original.write_text("# Lease\n\nold section 3\n")
    monkeypatch.setenv("ECHOECHO_USER_DOCS", str(docs))

    ws = tmp_path / "ws"; ws.mkdir()
    stage(ws, "t1",
          [{"staged": "lease.md", "target": str(original),
            "summary": "rewrote section 3"}],
          {"lease.md": "# Lease\n\nNEW section 3\n"})
    task = run_task("outbox.apply", tmp_path, workspace=ws)

    assert task.status == "done"
    assert original.read_text() == "# Lease\n\nNEW section 3\n"  # applied
    assert task.result.data["applied"] == ["lease.md"]
    # the original was backed up first
    baks = list(docs.glob("lease.md" + config.OUTBOX_BACKUP_SUFFIX + "-*"))
    assert len(baks) == 1 and baks[0].read_text() == "# Lease\n\nold section 3\n"
    assert "Saved 1 change" in task.result.say and "backed up" in task.result.say
    assert task.result.priority == "interrupt"


def test_apply_refuses_target_outside_shared_folders(tmp_path, monkeypatch):
    """The agent's manifest is NOT trusted: a target outside every shared
    folder is refused, and the file it points at is never written."""
    docs = tmp_path / "Documents"; docs.mkdir()
    monkeypatch.setenv("ECHOECHO_USER_DOCS", str(docs))
    victim = tmp_path / "secret.txt"
    victim.write_text("untouched")

    ws = tmp_path / "ws"; ws.mkdir()
    stage(ws, "t1",
          [{"staged": "evil", "target": str(victim), "summary": "pwn"}],
          {"evil": "OVERWRITTEN"})
    task = run_task("outbox.apply", tmp_path, workspace=ws)

    assert victim.read_text() == "untouched"  # refused, never written
    assert task.result.data["error"] == "nothing applied"
    assert str(victim) in task.result.data["refused"]


def test_apply_refuses_symlink_escape_from_shared_folder(tmp_path, monkeypatch):
    """A symlink inside a shared folder pointing outside must not become a
    write primitive (realpath containment)."""
    docs = tmp_path / "Documents"; docs.mkdir()
    monkeypatch.setenv("ECHOECHO_USER_DOCS", str(docs))
    outside = tmp_path / "outside.txt"; outside.write_text("safe")
    (docs / "link.txt").symlink_to(outside)

    ws = tmp_path / "ws"; ws.mkdir()
    stage(ws, "t1", [{"staged": "x", "target": str(docs / "link.txt")}],
          {"x": "hacked"})
    task = run_task("outbox.apply", tmp_path, workspace=ws)
    assert outside.read_text() == "safe"  # symlink target not followed out
    assert task.result.data["error"] == "nothing applied"


def test_apply_mixed_batch_applies_valid_skips_invalid(tmp_path, monkeypatch):
    docs = tmp_path / "Documents"; docs.mkdir()
    good = docs / "notes.md"; good.write_text("v1")
    monkeypatch.setenv("ECHOECHO_USER_DOCS", str(docs))
    ws = tmp_path / "ws"; ws.mkdir()
    stage(ws, "t1",
          [{"staged": "notes.md", "target": str(good)},
           {"staged": "bad", "target": str(tmp_path / "elsewhere.md")}],
          {"notes.md": "v2", "bad": "nope"})
    task = run_task("outbox.apply", tmp_path, workspace=ws)
    assert good.read_text() == "v2"
    assert task.result.data["applied"] == ["notes.md"]
    assert len(task.result.data["refused"]) == 1
    assert "skipped 1" in task.result.say


def test_apply_new_file_needs_no_backup(tmp_path, monkeypatch):
    docs = tmp_path / "Documents"; docs.mkdir()
    monkeypatch.setenv("ECHOECHO_USER_DOCS", str(docs))
    target = docs / "fresh.md"  # does not exist yet
    ws = tmp_path / "ws"; ws.mkdir()
    stage(ws, "t1", [{"staged": "fresh.md", "target": str(target)}],
          {"fresh.md": "brand new\n"})
    task = run_task("outbox.apply", tmp_path, workspace=ws)
    assert target.read_text() == "brand new\n"
    assert task.result.data["backups"] == []


def test_apply_without_user_docs_is_a_safe_noop(tmp_path, monkeypatch):
    monkeypatch.delenv("ECHOECHO_USER_DOCS", raising=False)
    task = run_task("outbox.apply", tmp_path)
    assert task.result.data["error"] == "no user docs configured"
    assert "nothing" in task.result.say.lower()


def test_apply_picks_latest_batch_by_default(tmp_path, monkeypatch):
    docs = tmp_path / "Documents"; docs.mkdir()
    a = docs / "a.md"; a.write_text("old")
    monkeypatch.setenv("ECHOECHO_USER_DOCS", str(docs))
    ws = tmp_path / "ws"; ws.mkdir()
    stage(ws, "t1", [{"staged": "a.md", "target": str(a)}], {"a.md": "from t1"})
    import time
    time.sleep(0.02)
    stage(ws, "t2", [{"staged": "a.md", "target": str(a)}], {"a.md": "from t2"})
    task = run_task("outbox.apply", tmp_path, workspace=ws)  # no task_id arg
    assert a.read_text() == "from t2"  # newest batch


def test_apply_bad_manifest_touches_nothing(tmp_path, monkeypatch):
    docs = tmp_path / "Documents"; docs.mkdir()
    keep = docs / "keep.md"; keep.write_text("safe")
    monkeypatch.setenv("ECHOECHO_USER_DOCS", str(docs))
    ws = tmp_path / "ws"; ws.mkdir()
    box = "%s/t1" % config.OUTBOX_DIR
    artifacts.write_atomic(ws, "%s/MANIFEST.json" % box, "{ not json")
    task = run_task("outbox.apply", tmp_path, workspace=ws)
    assert keep.read_text() == "safe"
    assert task.result.data["error"] == "bad manifest"


# -- review hardening: the write path must never lose data or half-apply -----

def test_apply_is_all_or_report_never_half_crashes(tmp_path, monkeypatch):
    """A bad entry (target is a directory) mid-batch must NOT abort the batch
    or raise: it's refused, the good entries still apply, and the report is
    honest."""
    docs = tmp_path / "Documents"; docs.mkdir()
    good = docs / "good.md"; good.write_text("v1")
    (docs / "adir").mkdir()  # a directory target — copy2 would raise
    monkeypatch.setenv("ECHOECHO_USER_DOCS", str(docs))
    ws = tmp_path / "ws"; ws.mkdir()
    stage(ws, "t1",
          [{"staged": "bad", "target": str(docs / "adir")},
           {"staged": "good.md", "target": str(good)}],
          {"bad": "x", "good.md": "v2"})
    task = run_task("outbox.apply", tmp_path, workspace=ws)
    assert task.status == "done"                 # not error/crash
    assert good.read_text() == "v2"              # the good entry applied
    assert task.result.data["applied"] == ["good.md"]
    assert str(docs / "adir") in task.result.data["refused"]


def test_apply_write_failure_is_reported_not_fatal(tmp_path, monkeypatch):
    """A write that raises OSError mid-batch is recorded in `failed`, other
    entries still apply, and nothing propagates."""
    docs = tmp_path / "Documents"; docs.mkdir()
    ok = docs / "ok.md"; ok.write_text("v1")
    boom = docs / "boom.md"; boom.write_text("orig")
    monkeypatch.setenv("ECHOECHO_USER_DOCS", str(docs))
    ws = tmp_path / "ws"; ws.mkdir()
    stage(ws, "t1",
          [{"staged": "boom.md", "target": str(boom)},
           {"staged": "ok.md", "target": str(ok)}],
          {"boom.md": "new", "ok.md": "v2"})

    import echoecho_app.workers.outbox as outbox_mod
    real_write = outbox_mod._write_over

    def flaky(target, staged_path):
        if target.name == "boom.md":
            raise OSError("disk on fire")
        return real_write(target, staged_path)

    monkeypatch.setattr(outbox_mod, "_write_over", flaky)
    task = run_task("outbox.apply", tmp_path, workspace=ws)
    assert task.status == "done"
    assert ok.read_text() == "v2"                       # good one applied
    assert task.result.data["applied"] == ["ok.md"]
    assert any("boom.md" in f for f in task.result.data["failed"])


def test_duplicate_target_preserves_true_original_backup(tmp_path, monkeypatch):
    """Two manifest entries for the SAME target must not clobber the backup of
    the real original (dedup + non-clobbering backup)."""
    docs = tmp_path / "Documents"; docs.mkdir()
    lease = docs / "lease.md"; lease.write_text("TRUE ORIGINAL")
    monkeypatch.setenv("ECHOECHO_USER_DOCS", str(docs))
    ws = tmp_path / "ws"; ws.mkdir()
    stage(ws, "t1",
          [{"staged": "a", "target": str(lease)},
           {"staged": "b", "target": str(lease)}],
          {"a": "FIRST", "b": "SECOND"})
    task = run_task("outbox.apply", tmp_path, workspace=ws)
    # applied once (deduped), last staged content wins
    assert task.result.data["applied"] == ["lease.md"]
    assert lease.read_text() == "SECOND"
    # exactly one backup, and it holds the TRUE original — never overwritten
    baks = list(docs.glob("lease.md" + config.OUTBOX_BACKUP_SUFFIX + "*"))
    assert len(baks) == 1
    assert baks[0].read_text() == "TRUE ORIGINAL"


def test_backup_never_clobbers_across_two_applies(tmp_path, monkeypatch):
    docs = tmp_path / "Documents"; docs.mkdir()
    f = docs / "f.md"; f.write_text("orig")
    monkeypatch.setenv("ECHOECHO_USER_DOCS", str(docs))
    ws = tmp_path / "ws"; ws.mkdir()
    stage(ws, "t1", [{"staged": "f.md", "target": str(f)}], {"f.md": "v2"})
    # apply twice in the same second-granularity stamp window
    run_task("outbox.apply", tmp_path, workspace=ws)
    stage(ws, "t1", [{"staged": "f.md", "target": str(f)}], {"f.md": "v3"})
    run_task("outbox.apply", tmp_path, workspace=ws)
    # both backups survive; the ORIGINAL is still recoverable from one of them
    baks = sorted(docs.glob("f.md" + config.OUTBOX_BACKUP_SUFFIX + "*"))
    assert len(baks) == 2
    assert any(b.read_text() == "orig" for b in baks)


def test_malformed_only_manifest_is_honest(tmp_path, monkeypatch):
    docs = tmp_path / "Documents"; docs.mkdir()
    monkeypatch.setenv("ECHOECHO_USER_DOCS", str(docs))
    ws = tmp_path / "ws"; ws.mkdir()
    box = "%s/t1" % config.OUTBOX_DIR
    artifacts.write_atomic(ws, "%s/MANIFEST.json" % box,
                           json.dumps(["not-a-dict", {"summary": "no target"}]))
    task = run_task("outbox.apply", tmp_path, workspace=ws)
    assert task.result.data["error"] == "nothing applied"
    # the OLD bug said "0 were outside your shared folders" — must not anymore
    assert "outside" not in task.result.say
    assert task.result.data["unusable"] == 2


def test_user_docs_path_with_spaces_survives(monkeypatch, tmp_path):
    d = tmp_path / "My Documents"; d.mkdir()
    monkeypatch.setenv("ECHOECHO_USER_DOCS", str(d))
    assert config.user_docs() == [d]  # not shattered on the space


def test_pick_task_dir_rejects_dotted_arg(tmp_path, monkeypatch):
    docs = tmp_path / "Documents"; docs.mkdir()
    monkeypatch.setenv("ECHOECHO_USER_DOCS", str(docs))
    ws = tmp_path / "ws"; ws.mkdir()
    (ws / config.OUTBOX_DIR).mkdir()
    task = run_task("outbox.apply", tmp_path, args={"task_id": ".."},
                    workspace=ws)
    assert task.result.data["error"] == "no staged changes"


# -- deterministic staged-changes handoff from agent.run ----------------------

def test_agent_run_stages_changes_gets_apply_it_completion(tmp_path,
                                                           monkeypatch):
    """When an agent run leaves an outbox MANIFEST, the completion say names
    the file and tells the user to say 'apply it' — deterministically, not
    left to the agent's free text."""
    from echoecho_app.services.agent_cli import FakeAgentCLI
    docs = tmp_path / "Documents"; docs.mkdir()
    (docs / "lease.md").write_text("orig")
    monkeypatch.setenv("ECHOECHO_USER_DOCS", str(docs))
    ws = tmp_path / "ws"; ws.mkdir()
    manifest = json.dumps([{"staged": "lease.md", "target": str(docs / "lease.md"),
                            "summary": "rewrote section 3"}])
    script = ws.parent / "s.jsonl"
    script.write_text("\n".join(json.dumps(e) for e in [
        {"type": "system", "subtype": "init", "session_id": "s1"},
        {"type": "_write", "file": "outbox/t1/lease.md", "content": "new lease\n"},
        {"type": "_write", "file": "outbox/t1/MANIFEST.json", "content": manifest},
        {"type": "result", "subtype": "success", "is_error": False,
         "session_id": "s1", "result": "I revised the lease."},
    ]) + "\n")

    orch = Orchestrator(registry=load_all(), log_path=tmp_path / "t.jsonl",
                        workspace=ws)
    orch.ctx.extra["agent_cli"] = FakeAgentCLI(script)

    async def go():
        loop = asyncio.ensure_future(orch.run())
        orch.submit(TaskRequest(kind="agent.run", instructions="revise the lease"))
        assert await orch.drain()
        loop.cancel()

    asyncio.run(go())
    result = orch.tasks["t1"].result
    assert "apply it" in result.say and "lease.md" in result.say
    assert result.data["staged"][0]["summary"] == "rewrote section 3"
    assert result.priority == "interrupt"


# -- conditional advertisement + prompt ---------------------------------------

def test_outbox_apply_advertised_only_with_user_docs(tmp_path, monkeypatch):
    load_all()
    monkeypatch.delenv("ECHOECHO_USER_DOCS", raising=False)
    monkeypatch.delenv("ECHOECHO_PLUGINS", raising=False)
    assert "outbox.apply" not in base.kinds_enum()  # hidden by default
    assert "outbox.apply" in base.REGISTRY          # but still dispatchable
    monkeypatch.setenv("ECHOECHO_USER_DOCS", str(tmp_path))
    assert "outbox.apply" in base.kinds_enum()      # appears once configured
    assert "apply it" in base.REGISTRY["outbox.apply"].description


def test_approval_guidance_only_when_docs_configured(tmp_path, monkeypatch):
    load_all()
    monkeypatch.delenv("ECHOECHO_USER_DOCS", raising=False)
    assert "apply it" not in config.system_prompt()
    monkeypatch.setenv("ECHOECHO_USER_DOCS", str(tmp_path))
    p = config.system_prompt()
    assert "apply it" in p and "outbox.apply" in p


def test_agent_prompt_teaches_outbox_convention_only_with_docs(tmp_path,
                                                               monkeypatch):
    task = Task(id="t7", request=TaskRequest(kind="agent.run"))
    monkeypatch.delenv("ECHOECHO_USER_DOCS", raising=False)
    assert _user_docs_convention(task) == ""
    monkeypatch.setenv("ECHOECHO_USER_DOCS", str(tmp_path))
    conv = _user_docs_convention(task)
    assert "READ-ONLY" in conv and "MANIFEST.json" in conv
    assert "outbox/t7" in conv  # keyed to this task's outbox dir


# -- read-only VM mounts ------------------------------------------------------

def test_vm_boot_argv_mounts_user_docs_read_only(tmp_path, monkeypatch):
    docs = tmp_path / "Documents"; docs.mkdir()
    monkeypatch.setenv("ECHOECHO_USER_DOCS", str(docs))
    vm = LumeVM(vm_name="echoecho-vm", workspace=tmp_path / "ws")
    argv = vm._boot_argv("lume")
    assert "%s:rw" % (tmp_path / "ws") in argv       # workspace read-write
    assert "%s:ro" % docs in argv                    # user docs read-only
    # the read-only share is a --shared-dir value
    i = argv.index("%s:ro" % docs)
    assert argv[i - 1] == "--shared-dir"
