"""Workspace file access. All writes are atomic: tmp file + os.rename, always,
so a viewer polling mtime never reads a half-written file."""
import os
import tempfile
from pathlib import Path


def read(workspace, name, default=""):  # type: (...) -> str
    path = Path(workspace) / name
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return default


def list_files(workspace):
    ws = Path(workspace)
    if not ws.is_dir():
        return []
    return sorted(p.name for p in ws.iterdir()
                  if p.is_file() and not p.name.startswith("."))


def mtime(workspace, name):  # type: (...) -> float
    """mtime of a workspace file, or 0.0 if it does not exist."""
    try:
        return (Path(workspace) / name).stat().st_mtime
    except FileNotFoundError:
        return 0.0


def write_atomic(workspace, name, content):  # type: (...) -> Path
    ws = Path(workspace)
    ws.mkdir(parents=True, exist_ok=True)
    target = ws / name
    fd, tmp = tempfile.mkstemp(dir=str(ws), prefix="." + name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
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
