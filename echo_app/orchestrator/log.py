"""Append-only JSONL task log (workspace/.tasks.jsonl) + replay helper."""
import json
import time
from pathlib import Path


def append_event(path, event, **fields):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": round(time.time(), 3), "event": event}
    rec.update(fields)
    with open(path, "a") as f:
        f.write(json.dumps(rec) + "\n")


def replay(path):
    """Return the logged events as a list of dicts (empty if no log yet)."""
    path = Path(path)
    if not path.exists():
        return []
    events = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return events
