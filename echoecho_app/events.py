"""Append-only UI event feed: workspace/.events.jsonl.

One JSON line per event ({"ts": ..., "type": ..., **fields}) consumed by the
viewer's transcript pane (GET /transcript + the SSE change poll). This is a
demo-rate side channel: open-append-close per call, and emit() NEVER raises —
a broken feed must never crash the app, so every IO error is swallowed.
"""
import json
import time

from echoecho_app import config

FEED_NAME = ".events.jsonl"

# Per-session recording hook: cb(rec_dict, serialized_line), set/cleared by
# echoecho_app.recorder while a session recording is active. Called guarded —
# like the feed itself, the tee is never load-bearing.
TEE = None


def feed_path():
    """Resolved at call time so tests can monkeypatch config.WORKSPACE_DIR."""
    return config.WORKSPACE_DIR / FEED_NAME


def emit(etype, **fields):
    """Append one event line to the feed (and mirror it to the active session
    recording, if any). Swallows ALL exceptions."""
    try:
        rec = {"ts": time.time(), "type": etype}
        rec.update(fields)
        line = json.dumps(rec, default=str)
    except Exception:
        return
    try:
        path = feed_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass  # the feed is best-effort UI plumbing, never load-bearing
    tee = TEE
    if tee is not None:
        try:
            tee(rec, line)
        except Exception:
            pass  # a broken recording must never break the feed (or the app)


def reset(mode="text"):
    """Truncate the feed (called once at app start so each run begins with a
    clean UI), then write the run marker. Swallows ALL exceptions."""
    try:
        path = feed_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8"):
            pass
    except Exception:
        pass
    emit("run", mode=mode)
