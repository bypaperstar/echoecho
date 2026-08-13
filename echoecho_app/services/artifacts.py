"""Workspace file access. All writes are atomic: tmp file + os.rename, always,
so a viewer polling mtime never reads a half-written file.

v2: an artifact is any file under workspace/, at any depth, any type — names
are relative paths ("outbox/lease/CHANGES.md"). resolve() is the single
safety guard shared by workers, the read_artifact tool, and the viewer.
"""
import os
import tempfile
from pathlib import Path


def resolve(workspace, name):  # type: (...) -> Path
    """Map a workspace-relative name to an absolute Path or raise ValueError.
    Rejects absolute paths, home-dir paths, traversal, and any dotted
    component — dotfiles (.tasks.jsonl, .events.jsonl, tmp files) are
    echoecho-internal, never artifacts. The lexical checks are backstopped by a
    realpath containment check: agents run shells inside the workspace, so a
    planted symlink must not turn a clean-looking name into an escape."""
    raw = str(name or "").strip()
    if not raw:
        raise ValueError("empty artifact name")
    rel = Path(os.path.normpath(raw))
    if rel.is_absolute() or rel.parts[:1] == ("~",):
        raise ValueError("artifact name must be workspace-relative: %r" % raw)
    if any(part.startswith(".") for part in rel.parts):  # covers ".." too
        raise ValueError("artifact name escapes the workspace: %r" % raw)
    path = Path(workspace) / rel
    ws_real = os.path.realpath(str(workspace))
    if not os.path.realpath(str(path)).startswith(ws_real + os.sep):
        raise ValueError("artifact name escapes the workspace: %r" % raw)
    return path


def read(workspace, name, default=""):  # type: (...) -> str
    try:
        return resolve(workspace, name).read_text(encoding="utf-8")
    except (ValueError, OSError):  # bad name, missing file, or a directory
        return default


def list_files(workspace):
    """Visible workspace files as sorted relative posix paths, recursively;
    dotted files and directories are skipped at every level."""
    ws = Path(workspace)
    if not ws.is_dir():
        return []
    out = []
    for root, dirs, names in os.walk(str(ws)):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        rel = Path(root).relative_to(ws)
        out.extend((rel / n).as_posix() for n in names
                   if not n.startswith("."))
    return sorted(out)


def mtime(workspace, name):  # type: (...) -> float
    """mtime of a workspace file, or 0.0 if it does not exist."""
    try:
        return resolve(workspace, name).stat().st_mtime
    except (ValueError, OSError):
        return 0.0


def stat_key(workspace, name):
    """(mtime, size, inode) change signature, or None if unreadable. Size +
    inode matter: coarse filesystem mtime granularity can hide two atomic
    writes in the same tick, but tmp+rename always swaps the inode."""
    try:
        st = resolve(workspace, name).stat()
    except (ValueError, OSError):
        return None
    return (st.st_mtime, st.st_size, st.st_ino)


def write_atomic(workspace, name, content):  # type: (...) -> Path
    """Write text (str) or binary (bytes) content; parent subdirectories are
    created on demand."""
    target = resolve(workspace, name)
    target.parent.mkdir(parents=True, exist_ok=True)
    binary = isinstance(content, (bytes, bytearray))
    fd, tmp = tempfile.mkstemp(dir=str(target.parent),
                               prefix="." + target.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb" if binary else "w",
                       **({} if binary else {"encoding": "utf-8"})) as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.rename(tmp, str(target))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return target
