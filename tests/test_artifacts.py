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
