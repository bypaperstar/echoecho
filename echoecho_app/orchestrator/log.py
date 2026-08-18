"""Append-only JSONL task log (workspace/.tasks.jsonl) + replay helper."""
import json
import time
from pathlib import Path

from echoecho_app import diagnostics


def append_event(path, event, **fields):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = {"schema_version": 2, "ts": round(time.time(), 3),
           "event": event}
    run_id = diagnostics.get_run_id()
    if run_id:
        rec["run_id"] = run_id
    rec.update(fields)
    try:
        with open(path, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception as exc:
        diagnostics.exception("task_log.append.failed", exc=exc, event=event)
        raise


def replay(path):
    """Return the logged events as a list of dicts (empty if no log yet).
    Malformed lines are skipped, not fatal — a crash mid-append must not
    poison rehydration forever."""
    path = Path(path)
    if not path.exists():
        return []
    events = []
    malformed = 0
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                malformed += 1
                continue
            if isinstance(ev, dict):
                events.append(ev)
    try:
        size_bytes = path.stat().st_size
    except OSError:
        size_bytes = None
    diagnostics.info("task_log.replayed", event_count=len(events),
                     malformed_count=malformed, size_bytes=size_bytes)
    return events
