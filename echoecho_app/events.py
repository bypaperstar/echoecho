"""Append-only UI event feed: workspace/.events.jsonl.

One JSON line per event ({"ts": ..., "type": ..., **fields}) consumed by the
viewer's transcript pane (GET /transcript + the SSE change poll). This is a
demo-rate side channel: open-append-close per call, and emit() NEVER raises —
a broken feed must never crash the app, so every IO error is swallowed.
"""
import json
import threading
import time

from echoecho_app import config, diagnostics

FEED_NAME = ".events.jsonl"

# Per-session recording hook: cb(rec_dict, serialized_line), set/cleared by
# echoecho_app.recorder while a session recording is active. Called guarded —
# like the feed itself, the tee is never load-bearing.
TEE = None
_LOCK = threading.RLock()
_RUN_ID = None
_SEQ = 0
_MONO_START = time.monotonic()
_FAILURES = {"serialize": 0, "write": 0, "tee": 0}


def feed_path():
    """Resolved at call time so tests can monkeypatch config.WORKSPACE_DIR."""
    return config.WORKSPACE_DIR / FEED_NAME


def set_tee(callback):
    """Attach a recorder sink at a feed sequence boundary."""
    global TEE
    with _LOCK:
        TEE = callback


def detach_tee(callback=None):
    """Detach a sink after every earlier sequenced enqueue has completed."""
    global TEE
    with _LOCK:
        if callback is None or TEE == callback:
            TEE = None


def emit(etype, **fields):
    """Append one event line to the feed (and mirror it to the active session
    recording, if any). Swallows ALL exceptions."""
    global _SEQ
    rec = None
    line = None
    tee = None
    write_error = None
    tee_error = None
    try:
        with _LOCK:
            rec = {"ts": time.time(), "type": etype}
            if _RUN_ID:
                _SEQ += 1
                rec.update({"run_id": _RUN_ID, "seq": _SEQ,
                            "mono_ms": round(
                                (time.monotonic() - _MONO_START) * 1000, 1)})
            tee = TEE
            rec.update(fields)
            line = json.dumps(rec, default=str)
            # Preserve line/sequence order. The Electron reload watermark
            # assumes this append-only feed is chronological; allowing a
            # later sequence to hit disk first can make a delayed earlier
            # event permanently invisible on the next poll.
            try:
                path = feed_path()
                path.parent.mkdir(parents=True, exist_ok=True)
                with open(path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except Exception as exc:
                write_error = exc
            # SessionRecorder's sink is explicitly bounded/nonblocking. Queue
            # it while sequence assignment is still serialized so recordings
            # preserve feed order without sharing a TextIOWrapper across
            # producer threads.
            tee_func = getattr(tee, "__func__", tee)
            if (tee is not None and getattr(
                    tee_func, "_echoecho_ordered_nonblocking", False)):
                try:
                    tee(rec, line)
                except Exception as exc:
                    tee_error = exc
                tee = None
    except Exception as exc:
        _note_failure("serialize", exc)
        return
    if write_error is not None:
        _note_failure("write", write_error)
    if tee_error is not None:
        _note_failure("tee", tee_error)
    # The recording tee may flush/fsync and is not part of the UI feed's order
    # contract. Keep it outside the shared lock so a slow recorder stalls only
    # its own producer rather than every audio/task event thread.
    if tee is not None:
        try:
            tee(rec, line)
        except Exception as exc:
            _note_failure("tee", exc)


def reset(mode="text", run_id=None):
    """Truncate the feed (called once at app start so each run begins with a
    clean UI), then write the run marker. Swallows ALL exceptions."""
    global _MONO_START, _RUN_ID, _SEQ
    with _LOCK:
        _RUN_ID = run_id
        _SEQ = 0
        _MONO_START = time.monotonic()
        for stage in _FAILURES:
            _FAILURES[stage] = 0
        try:
            path = feed_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8"):
                pass
        except Exception as exc:
            _note_failure("write", exc)
    emit("run", mode=mode)


def stats():
    """Best-effort feed health for run summaries and recorder metadata."""
    with _LOCK:
        return {"run_id": _RUN_ID, "seq": _SEQ,
                "failures": dict(_FAILURES)}


def _note_failure(stage, exc):
    """Count every loss, but only emit logarithmically to avoid a full-disk
    failure flooding stderr/diagnostics on every transcript delta."""
    with _LOCK:
        _FAILURES[stage] = _FAILURES.get(stage, 0) + 1
        count = _FAILURES[stage]
    if count == 1 or count & (count - 1) == 0:  # 1, 2, 4, 8, ...
        diagnostics.warning("ui_feed.%s_failed" % stage,
                            error_type=exc.__class__.__name__, count=count)
