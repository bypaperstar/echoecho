"""echoecho's version lives in the repo-root VERSION file — one place to
bump, surfaced by both the web viewer (/version) and the Mac app."""
from pathlib import Path


def _read_version():
    try:
        return (Path(__file__).resolve().parents[1] / "VERSION") \
            .read_text(encoding="utf-8").strip()
    except OSError:  # packaged/vendored without the file: never crash imports
        return "0.0.0"


__version__ = _read_version()
