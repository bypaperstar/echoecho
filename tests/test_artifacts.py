"""PR 10 artifacts: relative-path resolution guard (the one gate every
workspace consumer shares), subdirectories, any file type, binary atomic
writes, recursive listing."""
import pytest

from echoecho_app.services import artifacts


# -- resolve: the safety guard -------------------------------------------------

def test_resolve_accepts_nested_relative_paths(tmp_path):
    assert artifacts.resolve(tmp_path, "doc.md") == tmp_path / "doc.md"
    assert (artifacts.resolve(tmp_path, "outbox/lease/CHANGES.md")
            == tmp_path / "outbox" / "lease" / "CHANGES.md")
    # normpath folds internal traversal that stays inside the workspace
    assert artifacts.resolve(tmp_path, "a/../b.txt") == tmp_path / "b.txt"


@pytest.mark.parametrize("bad", [
    "", "   ", "..", "../secret", "a/../../b", "/etc/passwd",
    "~/.ssh/id_rsa", "~", ".tasks.jsonl", ".hidden.md", "sub/.hidden",
    ".git/config", "outbox/../../escape.md",
])
def test_resolve_rejects_escapes_and_dotfiles(tmp_path, bad):
    with pytest.raises(ValueError):
        artifacts.resolve(tmp_path, bad)


def test_read_and_mtime_are_lenient_on_bad_names(tmp_path):
    assert artifacts.read(tmp_path, "../etc/passwd", default="nope") == "nope"
    assert artifacts.mtime(tmp_path, "../etc/passwd") == 0.0
    (tmp_path / "sub").mkdir()
    assert artifacts.read(tmp_path, "sub", default="dir") == "dir"


def test_resolve_refuses_symlink_escapes(tmp_path):
    """Agents run shells inside the workspace: a planted symlink must not
    turn a clean-looking name into a host-file read or write."""
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    (ws / "innocent.md").symlink_to(outside)          # file symlink out
    (ws / "docs").symlink_to(tmp_path)                # dir symlink out
    with pytest.raises(ValueError):
        artifacts.resolve(ws, "innocent.md")
    with pytest.raises(ValueError):
        artifacts.resolve(ws, "docs/outside.txt")
    assert artifacts.read(ws, "innocent.md", default="") == ""
    # ...and the escaping entries carry no readable change signature either
    assert artifacts.stat_key(ws, "innocent.md") is None
    # symlinks that stay inside the workspace remain fine
    (ws / "real.md").write_text("ok")
    (ws / "alias.md").symlink_to(ws / "real.md")
    assert artifacts.read(ws, "alias.md") == "ok"


def test_tilde_named_file_is_listable_and_readable(tmp_path):
    """'~' rejection is for home-dir paths only — a file literally named
    '~notes.md' must not be listed by list_files yet refused by resolve."""
    (tmp_path / "~notes.md").write_text("x")
    assert artifacts.list_files(tmp_path) == ["~notes.md"]
    assert artifacts.read(tmp_path, "~notes.md") == "x"
    with pytest.raises(ValueError):
        artifacts.resolve(tmp_path, "~/notes.md")
    with pytest.raises(ValueError):
        artifacts.resolve(tmp_path, "~")


# -- subdirectories + any type ---------------------------------------------------

def test_write_atomic_creates_subdirs(tmp_path):
    target = artifacts.write_atomic(tmp_path, "outbox/lease/CHANGES.md", "# d\n")
    assert target.read_text() == "# d\n"
    assert artifacts.read(tmp_path, "outbox/lease/CHANGES.md") == "# d\n"
    # no tmp litter anywhere in the tree
    leftovers = [p for p in tmp_path.rglob("*") if p.name.endswith(".tmp")]
    assert leftovers == []


def test_write_atomic_binary(tmp_path):
    blob = b"\x89PNG\r\n\x1a\n" + bytes(range(64))
    target = artifacts.write_atomic(tmp_path, "img/chart.png", blob)
    assert target.read_bytes() == blob
    # a binary file reads back as the default, never a decode crash
    assert artifacts.read(tmp_path, "img/chart.png", default="") == ""


def test_write_instrumentation_accepts_resolve_coercions_and_cleans_failures(
        tmp_path, monkeypatch):
    # resolve() has always normalized names with str(); diagnostics must not
    # make that successful compatibility path fail afterward.
    records = []
    monkeypatch.setattr(
        artifacts.diagnostics, "info",
        lambda event, **fields: records.append((event, fields)))
    target = artifacts.write_atomic(tmp_path, 123, "café")
    assert target.name == "123" and target.read_text() == "café"
    assert records[-1][1]["content_bytes"] == len("café".encode("utf-8"))

    with pytest.raises(TypeError):
        artifacts.write_atomic(tmp_path, "bad.txt", object())
    assert not list(tmp_path.glob(".*.tmp"))


def test_write_diagnostics_only_emit_allowlisted_suffixes(tmp_path,
                                                          monkeypatch):
    records = []
    monkeypatch.setattr(
        artifacts.diagnostics, "info",
        lambda event, **fields: records.append((event, fields)))
    monkeypatch.setattr(
        artifacts.diagnostics, "exception",
        lambda event, exc=None, **fields: records.append((event, fields)))

    artifacts.write_atomic(tmp_path, "known.md", "ok")
    artifacts.write_atomic(tmp_path, "README", "ok")
    artifacts.write_atomic(tmp_path, "report.private-canary", "ok")

    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    with pytest.raises(OSError):
        artifacts.write_atomic(
            tmp_path, "blocker/report.private-canary", "fail")

    finished = [fields for event, fields in records
                if event == "artifact.write.finished"]
    failed = [fields for event, fields in records
              if event == "artifact.write.failed"]
    assert finished[0]["suffix"] == ".md"
    assert finished[1]["suffix"] == "none"
    for fields in (finished[2], failed[-1]):
        assert fields["suffix"] == "unknown"
        assert fields["suffix_length"] == len(".private-canary")
        assert len(fields["suffix_fingerprint"]) == 16
        assert all(char in "0123456789abcdef"
                   for char in fields["suffix_fingerprint"])
    assert "private-canary" not in repr(records)


def test_write_failures_are_instrumented_before_temp_file_exists(
        tmp_path, monkeypatch):
    failures = []
    monkeypatch.setattr(
        artifacts.diagnostics, "exception",
        lambda event, exc=None, **fields: failures.append((event, fields)))

    with pytest.raises(ValueError):
        artifacts.write_atomic(tmp_path, "../escape.txt", "x")
    assert failures[-1][0] == "artifact.write.failed"
    assert failures[-1][1]["stage"] == "resolve"

    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    with pytest.raises(OSError):
        artifacts.write_atomic(tmp_path, "blocker/child.txt", "x")
    assert failures[-1][1]["stage"] == "mkdir"

    monkeypatch.setattr(
        artifacts.tempfile, "mkstemp",
        lambda **_kwargs: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        artifacts.write_atomic(tmp_path, "temp-open.txt", "x")
    assert failures[-1][1]["stage"] == "temp_open"


def test_list_files_recurses_and_hides_dotted(tmp_path):
    artifacts.write_atomic(tmp_path, "a.md", "x")
    artifacts.write_atomic(tmp_path, "notes/deep/b.py", "print(1)")
    artifacts.write_atomic(tmp_path, "img/c.png", b"\x00\x01")
    (tmp_path / ".tasks.jsonl").write_text("{}")
    (tmp_path / ".cache").mkdir()
    (tmp_path / ".cache" / "junk.md").write_text("hidden dir")
    (tmp_path / "notes" / ".draft.md").write_text("hidden file")
    assert artifacts.list_files(tmp_path) == [
        "a.md", "img/c.png", "notes/deep/b.py"]
