"""echoecho's version: MAJOR.MINOR lives in the repo-root VERSION file (the
one place to bump deliberately); the PATCH is the git commit count, so every
deploy that ships new commits carries a new version number with zero manual
discipline. Writing a full MAJOR.MINOR.PATCH into VERSION overrides the
derived patch (an explicit pin wins). Surfaced by the web viewer (/version)
and the Mac app's control panel."""
import subprocess
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def _read_version():
    try:
        base = (_ROOT / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:  # packaged/vendored without the file: never crash imports
        return "0.0.0"
    if base.count(".") >= 2:
        return base  # explicit full version pinned by hand
    try:
        count = subprocess.run(
            ["git", "rev-list", "--count", "HEAD"],
            cwd=str(_ROOT), stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=5, check=True,
        ).stdout.decode("utf-8").strip()
        return "%s.%s" % (base, count)
    except Exception:  # no git (exported tree): stable but unnumbered
        return base + ".0"


__version__ = _read_version()
