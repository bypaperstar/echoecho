#!/usr/bin/env python3
"""Inspect echoecho's local structured diagnostics without extra packages.

The reader is deliberately more tolerant than the writers: it accepts common
Python and JavaScript field aliases, nested ``context``/``fields`` objects,
multiple JSONL files per run, and a partial final line after a crash.  It also
redacts its output a second time.  Diagnostics are useful precisely when one
producer is broken, so one unreadable file never prevents the remaining runs
from being summarized.
"""

import argparse
import collections
import datetime as dt
import heapq
import importlib.util
import json
import math
import os
import platform
import re
import shutil
import socket
import stat
import subprocess
import sys
from pathlib import Path


DEFAULT_DIR = Path.home() / ".echoecho" / "diagnostics"
METADATA_NAMES = {"manifest.json", "metadata.json", "run.json", "meta.json"}
LEVELS = {"debug": 10, "info": 20, "warn": 30, "error": 40, "critical": 50}
LEVEL_ALIASES = {
    "trace": "debug", "notice": "info", "warning": "warn",
    "err": "error", "fatal": "critical", "panic": "critical",
}
SLOW_KEEP = 20
CORRELATION_KEEP = 40
MAX_LATEST = 100
MAX_TAIL = 2000
MAX_METADATA_BYTES = 1024 * 1024
MAX_JSONL_LINE_CHARS = 2 * 1024 * 1024
MAX_JSONL_FILE_CHARS = 128 * 1024 * 1024
MAX_DISCOVERY_FILES = 10000
MAX_DISCOVERY_ENTRIES = 50000
MAX_RUNS_DISCOVERED = 1000
MAX_SUMMARY_NAMES = 512
MAX_TOTAL_INPUT_CHARS = 256 * 1024 * 1024
MAX_TOTAL_RECORDS = 1000000
MAX_TOTAL_RETAINED_EVENTS = 20000
MAX_READ_ERRORS = 512
SUMMARY_COUNT_MAP_KEYS = frozenset({
    "components", "levels", "top_events", "event_counts", "counters",
})

# Field names are checked recursively. Content-bearing fields are hidden too:
# the diagnostic stream is metadata, while recordings are the explicit place
# for audio/transcripts/prompts.
SECRET_KEY_RE = re.compile(
    r"(?:^|_)(?:api_?key|authorization|auth|token|password|passwd|secret|"
    r"cookie|credential|private_?key|vnc_?url)(?:$|_)", re.I)
CONTENT_KEY_RE = re.compile(
    r"(?:^|_)(?:audio|audio_b64|base64|prompt|instructions?|transcript|"
    r"text|content|body|output|stdout|stderr|args|arguments|query|document|"
    r"markdown|request_body|response_body|raw_payload|message|msg|detail|"
    r"reason)(?:$|_)",
    re.I)
# Aggregate measurements remain useful even when their prefix names the data
# they summarize: ``audio_bytes`` is safe metadata, while ``audio`` is not.
# Apply this before the broad content/secret patterns, matching writer policy.
SAFE_METADATA_SUFFIXES = {
    "available", "bytes", "chars", "count", "counts", "depth", "digest",
    "duration_ms", "enabled", "encoding", "fingerprint", "format", "frames",
    "hash", "high_water", "latency_ms", "len", "length", "ms", "peak",
    "percent", "present", "rate", "ratio", "returncode", "s", "seconds",
    "size", "status", "tokens", "type", "version",
}
SAFE_REASON_RE = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,79}$")
BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}")
OPENAI_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b")
OTHER_KEY_RE = re.compile(
    r"(?i)\b(?:ghp_[A-Za-z0-9]{12,}|github_pat_[A-Za-z0-9_]{12,}|"
    r"xox[baprs]-[A-Za-z0-9-]{8,}|AKIA[0-9A-Z]{12,})\b")
ENV_SECRET_RE = re.compile(
    r"(?i)\b([A-Z][A-Z0-9_]*(?:_KEY|_TOKEN|_PASSWORD|_SECRET)|"
    r"api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|passwd|"
    r"secret|authorization)\s*([:=])\s*([^\s,;]+)")
QUERY_SECRET_RE = re.compile(
    r"(?i)([?&](?:token|api[_-]?key|password|secret|authorization)=)[^&#\s]+")
URL_USERINFO_RE = re.compile(r"([a-z][a-z0-9+.-]*://)([^/@\s]+)@", re.I)
JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
# A diagnostics directory can be shared or caller-selected, so its records and
# filenames are untrusted terminal input. Render control and bidi characters
# visibly instead of allowing ANSI/OSC escapes, fake lines, or directionality
# spoofing in either the text or JSON inspector output.
TERMINAL_CONTROL_RE = re.compile(
    r"[\x00-\x1f\x7f-\x9f\u061c\u200e\u200f\u2028\u2029"
    r"\u202a-\u202e\u2066-\u2069]")
# Current Orb files use ``orb-run-``; accepting the earlier ``run-`` prefix
# keeps existing developer histories readable after the naming change.
ELECTRON_PART_RE = re.compile(
    r"^((?:orb-)?run-.*?)(?:\.(\d+))?\.jsonl$")


def _positive_int(value):
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be an integer")
    if number < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return number


def _bounded_count(maximum):
    def parse(value):
        return min(_positive_int(value), maximum)
    return parse


def _level(value):
    value = LEVEL_ALIASES.get(value.lower(), value.lower())
    if value not in LEVELS:
        raise argparse.ArgumentTypeError(
            "level must be debug, info, warn, error, or critical")
    return value


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Summarize echoecho structured diagnostic runs.")
    parser.add_argument(
        "--dir", type=Path,
        default=Path(os.environ.get("ECHOECHO_DIAGNOSTICS_DIR", DEFAULT_DIR)),
        help="diagnostics root (default: ECHOECHO_DIAGNOSTICS_DIR or %(default)s)")
    parser.add_argument(
        "--component", action="append", default=[], metavar="NAME",
        help="component substring filter; repeat or pass comma-separated names")
    parser.add_argument(
        "--latest", type=_bounded_count(MAX_LATEST), default=1, metavar="N",
        help="show the N newest runs (default: %(default)s)")
    parser.add_argument(
        "--tail", type=_bounded_count(MAX_TAIL), default=20, metavar="N",
        help="show the last N events at or above --level per run (default: %(default)s)")
    parser.add_argument(
        "--level", type=_level, default="warn", metavar="LEVEL",
        help="minimum level in the recent-events section (default: %(default)s)")
    parser.add_argument(
        "--slow-ms", type=_positive_int, default=1000, metavar="MS",
        help="minimum duration included as a slow span (default: %(default)s)")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument(
        "--doctor", action="store_true",
        help="check local capabilities, paths, key presence, and ports")
    return parser.parse_args(argv)


def normalize_level(value, event_name=""):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        if number >= 50:
            return "critical"
        if number >= 40:
            return "error"
        if number >= 30:
            return "warn"
        if number >= 20:
            return "info"
        return "debug"
    raw = str(value or "").strip().lower()
    raw = LEVEL_ALIASES.get(raw, raw)
    if raw in LEVELS:
        return raw
    name = str(event_name or "").lower()
    if any(word in name for word in ("fatal", "panic")):
        return "critical"
    if any(word in name for word in ("error", "failed", "failure", "crash", "exception")):
        return "error"
    if any(word in name for word in ("warn", "retry", "timeout", "lost", "dropped")):
        return "warn"
    return "info"


def record_level(record, name=None):
    explicit = lookup(record, "level", "severity", "log_level")
    normalized = normalize_level(explicit, name or event_name(record))
    if explicit not in (None, ""):
        return normalized
    error = lookup(record, "exception", "error", "exc", "traceback", "stack")
    if error not in (None, False, "", {}, []):
        return "error"
    return normalized


def redact_text(value, limit=1200):
    text = str(value)
    text = BEARER_RE.sub("Bearer <redacted>", text)
    text = OPENAI_KEY_RE.sub("<redacted-api-key>", text)
    text = OTHER_KEY_RE.sub("<redacted-api-key>", text)
    text = ENV_SECRET_RE.sub(
        lambda m: m.group(1) + m.group(2) + "<redacted>", text)
    text = QUERY_SECRET_RE.sub(lambda m: m.group(1) + "<redacted>", text)
    text = URL_USERINFO_RE.sub(lambda m: m.group(1) + "<redacted>@", text)
    text = JWT_RE.sub("<redacted-token>", text)
    try:
        home = str(Path.home())
        if home and home != "/":
            text = text.replace(home, "~")
    except Exception:
        pass
    def visible_control(match):
        char = match.group(0)
        names = {"\n": r"\n", "\r": r"\r", "\t": r"\t"}
        if char in names:
            return names[char]
        code = ord(char)
        return (r"\x%02x" % code) if code <= 0xff else (r"\u%04x" % code)

    text = TERMINAL_CONTROL_RE.sub(visible_control, text)
    if len(text) > limit:
        text = text[:limit] + "…[truncated]"
    return text


def _safe_metadata_value(normalized_key, value):
    suffix = next((item for item in SAFE_METADATA_SUFFIXES
                   if normalized_key == item or
                   normalized_key.endswith("_" + item)), None)
    if suffix is None:
        return False
    if value is None or isinstance(value, (bool, int, float)):
        return True
    if suffix in {"fingerprint", "hash", "digest"} and isinstance(value, str):
        return re.fullmatch(r"[0-9a-fA-F]{8,128}", value) is not None
    return False


def redact(value, key="", depth=0, _budget=None):
    """Recursive defense-in-depth redaction for the small objects we output."""
    if _budget is None:
        _budget = [512]
    if _budget[0] <= 0:
        return "<max-fields>"
    _budget[0] -= 1
    raw_key = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(key))
    raw_key = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", raw_key)
    normalized_key = re.sub(r"[^a-z0-9]+", "_", raw_key.lower()).strip("_")
    safe_metadata = _safe_metadata_value(normalized_key, value)
    if normalized_key == "reason" and isinstance(value, str) and SAFE_REASON_RE.fullmatch(value):
        return redact_text(value)
    if not safe_metadata and (
            SECRET_KEY_RE.search(normalized_key) or
            CONTENT_KEY_RE.search(normalized_key)):
        return "<redacted>"
    if depth > 8:
        return "<max-depth>"
    if isinstance(value, dict):
        items = list(value.items())[:100]
        result = {}
        for child_key, child_value in items:
            # These containers map already-redacted operational names to
            # numeric counts. Treating an event such as ``text_repl.started``
            # as a semantic field name would hide its count on the inspector's
            # second defense-in-depth pass.
            value_key = str(child_key)
            if (normalized_key in SUMMARY_COUNT_MAP_KEYS and
                    (child_value is None or
                     isinstance(child_value, (bool, int, float)))):
                value_key = "summary_count"
            result[redact_text(child_key, 160)] = redact(
                child_value, value_key, depth + 1, _budget)
        if len(value) > len(items):
            result["_truncated_fields"] = len(value) - len(items)
        return result
    if isinstance(value, (list, tuple)):
        result = [redact(v, key, depth + 1, _budget) for v in value[:100]]
        if len(value) > len(result):
            result.append({"_truncated_items": len(value) - len(result)})
        return result
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    return value


def _objects(record):
    """Objects searched for aliases, shallow-first and without recursion loops."""
    yield record
    for name in ("context", "fields", "attributes", "data", "metadata"):
        child = record.get(name)
        if isinstance(child, dict):
            yield child
            for nested_name in ("context", "fields", "attributes"):
                nested = child.get(nested_name)
                if isinstance(nested, dict):
                    yield nested


def lookup(record, *names):
    for obj in _objects(record):
        for name in names:
            if name in obj and obj[name] is not None:
                return obj[name]
    return None


def event_name(record):
    value = lookup(record, "event", "event_name", "name", "action", "type")
    return redact_text(value or "unknown", 160)


def component_name(record, fallback="unknown"):
    value = lookup(
        record, "component", "surface", "logger", "service", "producer", "process")
    if isinstance(value, dict):
        value = value.get("name") or value.get("component")
    return redact_text(value or fallback, 120)


def parse_timestamp(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        stamp = float(value)
        if not math.isfinite(stamp):
            return None
        if stamp > 10 ** 12:
            stamp /= 1000.0
        # Reject monotonic offsets as wall time; mtime remains the fallback.
        return stamp if 946684800 <= stamp <= 32503680000 else None
    text = str(value).strip()
    if not text:
        return None
    try:
        return parse_timestamp(float(text))
    except ValueError:
        pass
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.timestamp()
    except ValueError:
        return None


def record_timestamp(record):
    return parse_timestamp(lookup(
        record, "ts", "timestamp", "time", "@timestamp", "created_at", "started_at"))


def format_timestamp(value):
    if value is None:
        return "?"
    try:
        return dt.datetime.fromtimestamp(value, dt.timezone.utc).isoformat(
            timespec="milliseconds").replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        return "?"


def duration_ms(record):
    for name in ("duration_ms", "elapsed_ms", "latency_ms", "took_ms", "wall_ms"):
        value = lookup(record, name)
        if value is not None:
            try:
                number = float(value)
                return max(0.0, number) if math.isfinite(number) else None
            except (TypeError, ValueError):
                return None
    for name in ("duration_s", "elapsed_s"):
        value = lookup(record, name)
        if value is not None:
            try:
                number = float(value) * 1000.0
                return max(0.0, number) if math.isfinite(number) else None
            except (TypeError, ValueError):
                return None
    return None


def _stack_basename(location):
    """Return a code-file basename, never a URL, directory, or eval payload."""
    value = str(location).strip().split("?", 1)[0].split("#", 1)[0]
    value = value.replace("\\", "/").rsplit("/", 1)[-1]
    if not re.fullmatch(r"[A-Za-z0-9_.@+-]{1,120}", value):
        return "<redacted-file>"
    return redact_text(value, 120)


def _safe_stack_frame(line):
    """Reduce Python/V8 frames to basename and numeric source coordinates."""
    stripped = str(line).strip()
    python_frame = re.match(
        r'^File\s+["\'](?P<path>.+?)["\'],\s+line\s+(?P<line>\d+)',
        stripped)
    if python_frame:
        return 'File "%s", line %s' % (
            _stack_basename(python_frame.group("path")),
            python_frame.group("line"))
    if not stripped.startswith("at "):
        return None
    body = stripped[3:].rstrip(")").strip()
    if "(" in body:
        body = body.rsplit("(", 1)[-1].strip()
    js_frame = re.fullmatch(
        r"(?P<location>.+):(?P<line>\d+):(?P<column>\d+)", body)
    if not js_frame:
        js_frame = re.fullmatch(
            r"(?P<location>.+):(?P<line>\d+)", body)
    if not js_frame:
        return None
    result = "at %s:%s" % (
        _stack_basename(js_frame.group("location")), js_frame.group("line"))
    column = js_frame.groupdict().get("column")
    if column:
        result += ":" + column
    return result


def exception_info(record):
    value = lookup(record, "exception", "error", "exc")
    trace = lookup(record, "traceback", "stack", "stacktrace", "exception_stack")
    out = {}
    if isinstance(value, dict):
        typ = value.get("type") or value.get("name") or value.get("class") or value.get("code")
        message = value.get("message") or value.get("detail") or value.get("reason")
        trace = trace or value.get("stack") or value.get("traceback")
        if typ:
            out["type"] = redact_text(typ, 160)
        if message:
            # Exception messages routinely embed request bodies, transcripts,
            # local paths, and SDK payloads. Writers provide a fingerprint and
            # length for diagnosis; never re-expose an unsafe producer's text.
            out["message"] = "<redacted>"
        fingerprint = value.get("fingerprint") or value.get("message_fingerprint")
        if fingerprint not in (None, ""):
            out["fingerprint"] = redact_text(fingerprint, 160)
        for source, target in (("code", "code"), ("module", "module"),
                               ("message_length", "message_length")):
            if value.get(source) not in (None, ""):
                raw = value[source]
                out[target] = (raw if source == "message_length" and
                               isinstance(raw, int) else redact_text(raw, 160))
    elif value not in (None, False, True, ""):
        out["message"] = "<redacted>"
    if trace:
        # An older/untrusted producer may place URLs, eval strings, paths, or
        # user-controlled property names in V8 frames. Preserve only source
        # basenames and numeric coordinates.
        frames = [frame for frame in (
            _safe_stack_frame(line) for line in str(trace).splitlines())
                  if frame]
        if frames:
            out["stack"] = "\n".join(frames[-20:])
    return out or None


def safe_message(record):
    for obj in _objects(record):
        for name in ("message", "msg", "detail", "reason"):
            value = obj.get(name)
            if value in (None, ""):
                continue
            if name == "reason" and isinstance(value, str) and SAFE_REASON_RE.fullmatch(value):
                return redact_text(value, 80)
            return "<redacted>"
    return None


def correlation(record, name):
    aliases = {
        "run_id": ("run_id", "runId"),
        "parent_run_id": ("parent_run_id", "parentRunId"),
        "session_id": ("session_id", "sessionId", "local_session_id"),
        "task_id": ("task_id", "taskId"),
        "trace_id": ("trace_id", "traceId"),
        "span_id": ("span_id", "spanId"),
        "parent_span_id": ("parent_span_id", "parentSpanId"),
    }
    value = lookup(record, *aliases[name])
    if value in (None, ""):
        return None
    return redact_text(value, 180)


def component_matches(component, filters):
    if not filters:
        return True
    haystack = component.lower()
    return any(needle in haystack for needle in filters)


def _summary_name(counter, value, overflow):
    if value in counter:
        return value
    return value if len(counter) < MAX_SUMMARY_NAMES - 1 else overflow


def safe_event(record, source):
    name = event_name(record)
    level = record_level(record, name)
    result = {
        "ts": format_timestamp(record_timestamp(record)),
        "level": level,
        "component": component_name(record, source.stem),
        "event": name,
    }
    for field in ("run_id", "parent_run_id", "session_id", "task_id", "trace_id",
                  "span_id", "parent_span_id"):
        value = correlation(record, field)
        if value:
            result[field] = value
    duration = duration_ms(record)
    if duration is not None:
        result["duration_ms"] = round(duration, 3)
    message = safe_message(record)
    if message:
        result["message"] = message
    error = exception_info(record)
    if error:
        result["exception"] = error
    raw_fields = record.get("fields")
    if isinstance(raw_fields, dict) and raw_fields:
        result["fields"] = redact(raw_fields, "fields")
    return result


class RunSummary:
    def __init__(self, key, run_id=None, tail=20, slow_ms=1000):
        self.key = key
        self.run_id = redact_text(run_id, 180) if run_id else None
        self.tail = collections.deque(maxlen=max(1, tail))
        self.tail_enabled = tail > 0
        self.slow_ms = slow_ms
        self.slow = []
        self._slow_seq = 0
        self.files = set()
        self.metadata_files = set()
        self.metadata = {}
        self.components = collections.Counter()
        self.levels = collections.Counter()
        self.event_names = collections.Counter()
        self.correlations = collections.defaultdict(set)
        self.events = 0
        self.malformed_lines = 0
        self.read_errors = []
        self.read_errors_omitted = 0
        self.first_ts = None
        self.last_ts = None
        self.latest_mtime = 0.0

    def note_file(self, path):
        self.files.add(str(path))
        try:
            self.latest_mtime = max(self.latest_mtime, path.stat().st_mtime)
        except OSError:
            pass

    def note_read_error(self, detail):
        """Retain a useful sample without letting hostile trees grow output."""
        if len(self.read_errors) < MAX_READ_ERRORS:
            self.read_errors.append(redact_text(detail, 1000))
        else:
            self.read_errors_omitted += 1

    def add_metadata(self, data, path):
        self.metadata_files.add(str(path))
        self.note_file(path)
        if not self.run_id:
            rid = correlation(data, "run_id")
            if not rid and path.name in ("manifest.json", "run.json"):
                candidate = data.get("id")
                rid = candidate if isinstance(candidate, (str, int)) else None
            if rid:
                self.run_id = redact_text(rid, 180)
        allowed = {
            "schema_version", "version", "sha", "revision", "mode", "model",
            "platform", "python", "node", "pid", "component", "surface",
            "started", "started_at", "ended", "ended_at", "closed_at",
            "last_event_at", "state", "outcome", "seq", "log_file",
            "log_part", "log_files", "files", "build",
        }
        for key in allowed:
            value = lookup(data, key)
            if value not in (None, ""):
                self.metadata[key] = redact(value, key)
        for name in ("started", "started_at", "ended", "ended_at", "closed_at",
                     "last_event_at", "ts", "timestamp"):
            stamp = parse_timestamp(lookup(data, name))
            if stamp is not None:
                self._note_timestamp(stamp)

    def _note_timestamp(self, stamp):
        self.first_ts = stamp if self.first_ts is None else min(self.first_ts, stamp)
        self.last_ts = stamp if self.last_ts is None else max(self.last_ts, stamp)

    def add_record(self, record, path, component_filters, min_level):
        component = component_name(record, path.stem)
        if not component_matches(component, component_filters):
            return False
        name = event_name(record)
        level = record_level(record, name)
        view = safe_event(record, path)
        self.note_file(path)
        self.events += 1
        self.components[_summary_name(
            self.components, component, "<other-components>")] += 1
        self.levels[level] += 1
        self.event_names[_summary_name(
            self.event_names, name, "<other-events>")] += 1
        stamp = record_timestamp(record)
        if stamp is not None:
            self._note_timestamp(stamp)
        for field in ("parent_run_id", "session_id", "task_id", "trace_id", "span_id",
                      "parent_span_id"):
            value = correlation(record, field)
            if value and len(self.correlations[field]) < CORRELATION_KEEP:
                self.correlations[field].add(value)
        if self.tail_enabled and LEVELS[level] >= LEVELS[min_level]:
            self.tail.append(view)
        duration = duration_ms(record)
        if duration is not None and duration >= self.slow_ms:
            self._slow_seq += 1
            item = (duration, self._slow_seq, view)
            if len(self.slow) < SLOW_KEEP:
                heapq.heappush(self.slow, item)
            elif duration > self.slow[0][0]:
                heapq.heapreplace(self.slow, item)
        return True

    @property
    def sort_time(self):
        return self.last_ts or self.latest_mtime

    def as_dict(self, root):
        rid = self.run_id or Path(self.key.split(":", 1)[-1]).name or "unknown"
        correlations = {}
        for name, values in sorted(self.correlations.items()):
            correlations[name] = sorted(values)
        def relative(value):
            try:
                return str(Path(value).resolve().relative_to(root.resolve()))
            except (OSError, ValueError):
                return str(value)
        return {
            "run_id": rid,
            "started": format_timestamp(self.first_ts),
            "ended": format_timestamp(self.last_ts),
            "events": self.events,
            "components": dict(self.components.most_common()),
            "levels": dict(self.levels.most_common()),
            "top_events": dict(self.event_names.most_common(20)),
            "correlation_ids": correlations,
            "recent": list(self.tail),
            "slow_spans": [item[2] for item in sorted(self.slow, reverse=True)],
            "malformed_lines": self.malformed_lines,
            "read_errors": list(self.read_errors),
            "read_errors_omitted": self.read_errors_omitted,
            "metadata": redact(self.metadata),
            "files": sorted(relative(p) for p in self.files),
            "metadata_files": sorted(relative(p) for p in self.metadata_files),
        }


def _run_id_from_metadata(data, path):
    rid = correlation(data, "run_id")
    if not rid and path.name in ("manifest.json", "run.json"):
        value = data.get("id")
        if isinstance(value, (str, int)):
            rid = value
    return str(rid) if rid not in (None, "") else None


def _is_metadata(path):
    name = path.name.lower()
    return (name in METADATA_NAMES or name == "latest.json" or
            (name.startswith("latest-") and name.endswith(".json")))


def _fallback_key(path, root):
    try:
        rel = path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return "path:" + str(path.parent.resolve())
    parts = rel.parts
    if len(parts) >= 3 and parts[0] == "runs":
        return "path:" + str((root / parts[0] / parts[1]).resolve())
    return "path:" + str(path.parent.resolve())


def _nearest_metadata_key(path, root, directory_keys):
    current = path.parent.resolve()
    stop = root.resolve()
    while True:
        if current in directory_keys:
            return directory_keys[current]
        if current == stop or current.parent == current:
            break
        current = current.parent
    return _fallback_key(path, root)


def _jsonl_sort_key(path):
    """Order Electron rotation parts as base, .1, .2, ... within a run."""
    match = ELECTRON_PART_RE.match(path.name)
    if match:
        return (str(path.parent), match.group(1), int(match.group(2) or 0))
    return (str(path.parent), path.name, 0)


def _bounded_tree(root, state, limit):
    """Yield a directory tree with bounded, no-symlink directory traversal."""
    flags = os.O_RDONLY
    for option in ("O_CLOEXEC", "O_DIRECTORY", "O_NOFOLLOW"):
        flags |= getattr(os, option, 0)
    stack = []
    try:
        root_fd = os.open(str(root), flags)
        try:
            root_entries = os.scandir(root_fd)
        except BaseException:
            os.close(root_fd)
            raise
        stack.append((root_fd, root, root_entries))
        visited = 0
        while stack:
            directory_fd, logical_dir, entries = stack[-1]
            try:
                entry = next(entries)
            except StopIteration:
                entries.close()
                os.close(directory_fd)
                stack.pop()
                continue
            visited += 1
            if visited > limit:
                state["capped"] = True
                return
            logical_path = logical_dir / entry.name
            yield logical_path
            try:
                is_directory = entry.is_dir(follow_symlinks=False)
            except OSError:
                is_directory = False
            if not is_directory:
                continue
            child_fd = None
            try:
                child_fd = os.open(entry.name, flags, dir_fd=directory_fd)
                if not stat.S_ISDIR(os.fstat(child_fd).st_mode):
                    os.close(child_fd)
                    continue
                stack.append((child_fd, logical_path, os.scandir(child_fd)))
            except OSError:
                if child_fd is not None:
                    try:
                        os.close(child_fd)
                    except OSError:
                        pass
    finally:
        while stack:
            directory_fd, _logical_dir, entries = stack.pop()
            try:
                entries.close()
            except Exception:
                pass
            try:
                os.close(directory_fd)
            except OSError:
                pass


def _run_summary(runs, key, run_id, args, discovery):
    existing = runs.get(key)
    if existing is not None:
        return existing
    if len(runs) >= MAX_RUNS_DISCOVERED - 1:
        discovery["runs_coalesced"] += 1
        key = "overflow:too-many-runs"
        run_id = None
    return runs.setdefault(
        key, RunSummary(key, run_id, args.tail, args.slow_ms))


def _open_regular_text(path, max_bytes=None, root=None):
    """Open an untrusted regular file without following any path symlink."""
    file_flags = os.O_RDONLY
    for option in ("O_CLOEXEC", "O_NOFOLLOW", "O_NONBLOCK"):
        file_flags |= getattr(os, option, 0)
    directory_flags = os.O_RDONLY
    for option in ("O_CLOEXEC", "O_DIRECTORY", "O_NOFOLLOW"):
        directory_flags |= getattr(os, option, 0)
    directory_fd = None
    if root is None:
        fd = os.open(str(path), file_flags)
    else:
        root = Path(root)
        path = Path(path)
        try:
            relative = path.relative_to(root)
        except ValueError as exc:
            raise OSError("diagnostic input escapes requested root") from exc
        if not relative.parts:
            raise OSError("diagnostic input has no filename")
        directory_fd = os.open(str(root), directory_flags)
        try:
            for component in relative.parts[:-1]:
                next_fd = os.open(
                    component, directory_flags, dir_fd=directory_fd)
                os.close(directory_fd)
                directory_fd = next_fd
            fd = os.open(
                relative.parts[-1], file_flags, dir_fd=directory_fd)
        finally:
            if directory_fd is not None:
                os.close(directory_fd)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError("diagnostic input is not a regular file")
        if max_bytes is not None and info.st_size > max_bytes:
            raise ValueError("diagnostic input exceeds %d bytes" % max_bytes)
        return os.fdopen(fd, "r", encoding="utf-8", errors="replace")
    except BaseException:
        os.close(fd)
        raise


def _read_metadata(path, total_budget=None, root=None):
    if total_budget is not None and total_budget[0] <= 0:
        raise ValueError("diagnostic reader input budget exhausted")
    with _open_regular_text(
            path, MAX_METADATA_BYTES, root=root) as stream:
        raw = stream.read(MAX_METADATA_BYTES + 1)
    if total_budget is not None:
        total_budget[0] -= len(raw)
    if len(raw) > MAX_METADATA_BYTES:
        raise ValueError("metadata exceeds %d characters" % MAX_METADATA_BYTES)
    return json.loads(raw)


def _bounded_jsonl_lines(stream, total_budget=None):
    """Yield physical lines without materializing an attacker-sized record."""
    number = 0
    consumed = 0
    while True:
        fragment = stream.readline(MAX_JSONL_LINE_CHARS + 1)
        if not fragment:
            return
        consumed += len(fragment)
        if total_budget is not None:
            total_budget[0] -= len(fragment)
        number += 1
        if (consumed > MAX_JSONL_FILE_CHARS or
                total_budget is not None and total_budget[0] < 0):
            yield number, None
            return
        oversized = len(fragment) > MAX_JSONL_LINE_CHARS
        if fragment.endswith("\n"):
            yield number, None if oversized else fragment
            if consumed >= MAX_JSONL_FILE_CHARS:
                return
            continue
        continuation = stream.readline(MAX_JSONL_LINE_CHARS + 1)
        if not continuation:
            yield number, None if oversized else fragment
            return
        consumed += len(continuation)
        if total_budget is not None:
            total_budget[0] -= len(continuation)
        if (consumed > MAX_JSONL_FILE_CHARS or
                total_budget is not None and total_budget[0] < 0):
            yield number, None
            return
        oversized = True
        while continuation and not continuation.endswith("\n"):
            continuation = stream.readline(MAX_JSONL_LINE_CHARS + 1)
            consumed += len(continuation)
            if total_budget is not None:
                total_budget[0] -= len(continuation)
            if (consumed > MAX_JSONL_FILE_CHARS or
                    total_budget is not None and total_budget[0] < 0):
                yield number, None
                return
        yield number, None
        if consumed >= MAX_JSONL_FILE_CHARS:
            return


def _note_discovery_error(discovery, detail):
    if len(discovery["read_errors"]) < MAX_READ_ERRORS:
        discovery["read_errors"].append(redact_text(detail, 1000))
    else:
        discovery["read_errors_omitted"] += 1


def discover(args):
    root = args.dir.expanduser().resolve()
    component_filters = []
    for raw in args.component:
        component_filters.extend(part.strip().lower() for part in raw.split(",") if part.strip())
    runs = {}
    directory_keys = {}
    discovery = {
        "jsonl_files": 0, "metadata_files": 0, "malformed_lines": 0,
        "read_errors": [], "read_errors_omitted": 0,
        "files_skipped": 0, "runs_coalesced": 0,
        "work_limited": 0, "records_read": 0, "events_retained": 0,
        "tail_events_dropped": 0,
    }
    if not root.is_dir():
        return root, [], discovery, component_filters

    seen = set()
    metadata_paths = []
    jsonl_paths = []
    try:
        walk_state = {"capped": False}
        for path in _bounded_tree(root, walk_state, MAX_DISCOVERY_ENTRIES):
            try:
                if not stat.S_ISREG(path.lstat().st_mode):
                    continue
                resolved = path.resolve()
                # A shared diagnostics directory is untrusted input. Never
                # follow JSONL/metadata symlinks; a later no-follow open also
                # closes the validation/use race for replaced regular files.
                resolved.relative_to(root)
            except (OSError, ValueError):
                continue
            is_jsonl = resolved.suffix.lower() == ".jsonl"
            is_metadata = _is_metadata(resolved)
            if not (is_jsonl or is_metadata):
                continue
            if len(metadata_paths) + len(jsonl_paths) >= MAX_DISCOVERY_FILES:
                discovery["files_skipped"] += 1
                break
            # Ignore aliases/races that resolve to a file already selected.
            identity = str(resolved)
            if identity in seen:
                continue
            seen.add(identity)
            if is_jsonl:
                jsonl_paths.append(resolved)
            elif is_metadata:
                metadata_paths.append(resolved)
        if walk_state["capped"]:
            discovery["files_skipped"] += 1
            discovery["work_limited"] += 1
    except OSError as exc:
        _note_discovery_error(discovery, exc)

    total_budget = [MAX_TOTAL_INPUT_CHARS]

    # Metadata first: it lets component subdirectories inherit the run id from
    # a parent manifest even when individual events predate correlation fields.
    for path in sorted(metadata_paths):
        if total_budget[0] <= 0:
            discovery["work_limited"] += 1
            break
        discovery["metadata_files"] += 1
        try:
            data = _read_metadata(path, total_budget, root=root)
            if not isinstance(data, dict):
                raise ValueError("top-level JSON is not an object")
        except (OSError, ValueError) as exc:
            _note_discovery_error(
                discovery, "%s: %s" % (path, redact_text(exc)))
            continue
        rid = _run_id_from_metadata(data, path)
        key = "id:" + rid if rid else _fallback_key(path, root)
        # A latest pointer describes one process in a flat shared directory;
        # it must not cause unrelated JSONL files to inherit that run id.
        if not path.name.lower().startswith("latest"):
            directory_keys[path.parent.resolve()] = key
        run = _run_summary(runs, key, rid, args, discovery)
        run.add_metadata(data, path)

    for path in sorted(jsonl_paths, key=_jsonl_sort_key):
        if (total_budget[0] <= 0 or
                discovery["records_read"] >= MAX_TOTAL_RECORDS):
            discovery["work_limited"] += 1
            break
        discovery["jsonl_files"] += 1
        default_key = _nearest_metadata_key(path, root, directory_keys)
        default_run = _run_summary(
            runs, default_key, None, args, discovery)
        default_run.note_file(path)
        try:
            stream = _open_regular_text(path, root=root)
        except OSError as exc:
            detail = "%s: %s" % (path, redact_text(exc))
            default_run.note_read_error(detail)
            _note_discovery_error(discovery, detail)
            continue
        with stream:
            for number, line in _bounded_jsonl_lines(stream, total_budget):
                if line is None:
                    default_run.malformed_lines += 1
                    discovery["malformed_lines"] += 1
                    continue
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                    if not isinstance(record, dict):
                        raise ValueError("record is not an object")
                except ValueError:
                    default_run.malformed_lines += 1
                    discovery["malformed_lines"] += 1
                    continue
                if discovery["records_read"] >= MAX_TOTAL_RECORDS:
                    discovery["work_limited"] += 1
                    break
                discovery["records_read"] += 1
                rid = correlation(record, "run_id")
                key = "id:" + str(rid) if rid else default_key
                run = _run_summary(runs, key, rid, args, discovery)
                if (rid and not run.run_id and
                        run.key != "overflow:too-many-runs"):
                    run.run_id = redact_text(rid, 180)
                before = len(run.tail)
                run.add_record(record, path, component_filters, args.level)
                growth = len(run.tail) - before
                if (growth > 0 and discovery["events_retained"] + growth >
                        MAX_TOTAL_RETAINED_EVENTS):
                    run.tail.popleft()
                    discovery["tail_events_dropped"] += growth
                else:
                    discovery["events_retained"] += growth
            if total_budget[0] <= 0:
                discovery["work_limited"] += 1
                break

    selected = [run for run in runs.values() if run.events or (not component_filters and run.metadata)]
    selected.sort(key=lambda run: run.sort_time, reverse=True)
    if args.latest:
        selected = selected[:args.latest]
    else:
        selected = []
    return root, selected, discovery, component_filters


def _counter_text(values):
    return ", ".join("%s=%s" % item for item in values.items()) or "none"


def _id_text(values):
    if not values:
        return "none"
    parts = []
    for name, ids in values.items():
        shown = ids[:8]
        suffix = " +%d" % (len(ids) - len(shown)) if len(ids) > len(shown) else ""
        parts.append("%s=%s%s" % (name, ",".join(shown), suffix))
    return "; ".join(parts)


def print_event(event, indent="  "):
    ids = " ".join(
        "%s=%s" % (key, redact_text(event[key], 180)) for key in (
            "parent_run_id", "session_id", "task_id", "trace_id", "span_id")
        if key in event)
    duration = " %.0fms" % event["duration_ms"] if "duration_ms" in event else ""
    print("%s%s %-8s %-20s %-32s%s%s" % (
        indent, redact_text(event.get("ts", "?"), 40),
        redact_text(event.get("level", "info"), 20).upper(),
        redact_text(event.get("component", "unknown"), 20),
        redact_text(event.get("event", "unknown"), 32),
        duration, (" " + ids) if ids else ""))
    if event.get("message"):
        print(indent + "  " + redact_text(event["message"], 1200))
    if event.get("fields"):
        print(indent + "  fields: " + redact_text(json.dumps(
            event["fields"], ensure_ascii=False, sort_keys=True), 1600))
    error = event.get("exception") or {}
    if error:
        headline = ": ".join(
            redact_text(x, 500)
            for x in (error.get("type"), error.get("message")) if x)
        print(indent + "  exception: " + (headline or "recorded"))
        details = ", ".join(
            "%s=%s" % (name, redact_text(error[name], 180))
            for name in ("fingerprint", "code", "module", "message_length")
            if name in error)
        if details:
            print(indent + "    " + details)
        if error.get("stack"):
            for line in error["stack"].splitlines()[-20:]:
                print(indent + "    " + redact_text(line, 500))


def print_summary(root, runs, discovery, args, component_filters):
    print("diagnostics: %s" % redact_text(root, 1000))
    if component_filters:
        print("filter: component=%s level>=%s" % (
            redact_text(",".join(component_filters), 500), args.level))
    if not runs:
        print("no structured diagnostic runs found")
        if not root.exists():
            print("the directory does not exist yet; start echoecho with diagnostics enabled")
        if (discovery["malformed_lines"] or discovery["read_errors"] or
                discovery["read_errors_omitted"]):
            print("reader totals: malformed=%d read_errors=%d "
                  "read_errors_omitted=%d (no valid records found)" % (
                      discovery["malformed_lines"],
                      len(discovery["read_errors"]),
                      discovery["read_errors_omitted"]))
        if (discovery["files_skipped"] or discovery["runs_coalesced"] or
                discovery["work_limited"] or discovery["tail_events_dropped"]):
            print("reader limits: files_skipped=%d runs_coalesced=%d "
                  "work_limited=%d tail_events_dropped=%d" % (
                      discovery["files_skipped"], discovery["runs_coalesced"],
                      discovery["work_limited"],
                      discovery["tail_events_dropped"]))
        return
    for run in runs:
        data = run.as_dict(root)
        print("\nrun %s  %s → %s" % (
            redact_text(data["run_id"], 180), data["started"], data["ended"]))
        print("  events: %d  components: %s" % (data["events"], _counter_text(data["components"])))
        print("  levels: %s" % _counter_text(data["levels"]))
        print("  top events: %s" % _counter_text(data["top_events"]))
        print("  correlations: %s" % _id_text(data["correlation_ids"]))
        if data["metadata"]:
            print("  metadata: " + redact_text(json.dumps(
                data["metadata"], ensure_ascii=False, sort_keys=True), 1000))
        print("  files: " + ", ".join(
            redact_text(name, 500) for name in data["files"]))
        if (data["malformed_lines"] or data["read_errors"] or
                data["read_errors_omitted"]):
            print("  reader warnings: malformed=%d read_errors=%d "
                  "read_errors_omitted=%d" % (
                      data["malformed_lines"], len(data["read_errors"]),
                      data["read_errors_omitted"]))
        if data["slow_spans"]:
            print("  slow spans (>= %dms):" % args.slow_ms)
            for event in data["slow_spans"][:10]:
                print_event(event, "    ")
        if data["recent"]:
            print("  recent %s+ events:" % args.level)
            for event in data["recent"]:
                print_event(event, "    ")
        elif args.tail:
            print("  recent %s+ events: none" % args.level)
    if (discovery["malformed_lines"] or discovery["read_errors"] or
            discovery["read_errors_omitted"]):
        print("\nreader totals: malformed=%d read_errors=%d "
              "read_errors_omitted=%d (valid records were still used)" % (
                  discovery["malformed_lines"],
                  len(discovery["read_errors"]),
                  discovery["read_errors_omitted"]))
    if (discovery["files_skipped"] or discovery["runs_coalesced"] or
            discovery["work_limited"] or discovery["tail_events_dropped"]):
        print("\nreader limits: files_skipped=%d runs_coalesced=%d "
              "work_limited=%d tail_events_dropped=%d" % (
                  discovery["files_skipped"], discovery["runs_coalesced"],
                  discovery["work_limited"],
                  discovery["tail_events_dropped"]))


def _run_command(argv, cwd=None):
    try:
        proc = subprocess.run(
            argv, cwd=str(cwd) if cwd else None, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, timeout=5, check=False)
        return proc.returncode, redact_text(proc.stdout.strip(), 500)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, redact_text(exc, 500)


def _env_keys(path):
    keys = set()
    try:
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            for line in stream:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                name, raw_value = line.split("=", 1)
                name = name.strip()
                value_present = bool(raw_value.strip().strip("'\""))
                if value_present and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                    keys.add(name)
    except OSError:
        pass
    return keys


def _nearest_existing(path):
    path = path.expanduser()
    while not path.exists() and path.parent != path:
        path = path.parent
    return path


def _port_status(port):
    if port is None:
        return False
    sock = socket.socket()
    sock.settimeout(0.4)
    try:
        return sock.connect_ex(("127.0.0.1", port)) == 0
    finally:
        sock.close()


def _port_env(name, default):
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
        return value if 1 <= value <= 65535 else None
    except ValueError:
        return None


def doctor(root):
    repo = Path(__file__).resolve().parents[1]
    state = Path(os.environ.get("ECHOECHO_STATE_DIR", Path.home() / ".echoecho")).expanduser()
    checks = []

    def add(name, status, detail, category="runtime"):
        checks.append({"name": name, "status": status, "detail": redact_text(detail, 800),
                       "category": category})

    py_ok = sys.version_info >= (3, 9)
    add("Python", "ok" if py_ok else "fail", "%s (%s)" % (platform.python_version(), sys.executable))
    add("platform", "ok" if sys.platform == "darwin" else "warn",
        "%s %s; voice/audio/VM features require macOS" % (platform.system(), platform.machine()))
    add("repository", "ok" if (repo / "echoecho.py").is_file() else "fail", str(repo), "paths")

    rc, revision = _run_command(["git", "rev-parse", "--short", "HEAD"], repo)
    add("git revision", "ok" if rc == 0 else "warn", revision or "unavailable", "source")
    rc, dirty = _run_command(["git", "status", "--porcelain"], repo)
    add("git worktree", "ok" if rc == 0 and not dirty else "warn",
        "clean" if rc == 0 and not dirty else ("has local changes" if rc == 0 else "unavailable"), "source")

    venv_python = repo / ".venv" / "bin" / "python"
    add("project virtualenv", "ok" if venv_python.is_file() else "warn",
        str(venv_python) if venv_python.is_file() else "missing; headless system Python may still work")
    for module, required in (("openai", True), ("websockets", True), ("vosk", False),
                             ("sounddevice", False), ("livekit", False)):
        present = importlib.util.find_spec(module) is not None
        status = "ok" if present else ("warn" if not required else "warn")
        add("Python module: " + module, status,
            "installed" if present else "not importable in this Python", "dependencies")

    for command, capability in (("node", "Electron development"), ("npm", "Electron install/build"),
                                ("lume", "macOS VM"), ("claude", "agent runtime"),
                                ("codex", "agent runtime fallback")):
        path = shutil.which(command)
        add("command: " + command, "ok" if path else "warn",
            path or ("missing; %s unavailable" % capability), "dependencies")
    electron = repo / "app" / "node_modules" / ".bin" / "electron"
    add("Electron install", "ok" if electron.exists() else "warn",
        str(electron) if electron.exists() else "run npm install in app/", "dependencies")

    configured_keys = {name for name, value in os.environ.items() if value}
    configured_keys.update(_env_keys(repo / ".env.local"))
    configured_keys.update(_env_keys(state / "daemon.env"))
    for name, use in (("OPENAI_API_KEY", "Realtime, workers, and Live Writer"),
                      ("ANTHROPIC_API_KEY", "Claude agent runtime / VM forwarding")):
        add("key presence: " + name, "ok" if name in configured_keys else "warn",
            "present (value never shown)" if name in configured_keys else
            "absent; %s may be unavailable" % use,
            "credentials")

    workspace = repo / "workspace"
    workspace_ok = workspace.is_dir() and os.access(str(workspace), os.W_OK | os.X_OK)
    add("workspace", "ok" if workspace_ok else "fail", str(workspace), "paths")
    model = repo / "models" / "vosk-model-small-en-us-0.15" / "am" / "final.mdl"
    add("wake model", "ok" if model.is_file() else "warn",
        str(model) if model.is_file() else "missing; run scripts/fetch_models.sh", "paths")

    root_parent = _nearest_existing(root)
    root_writable = root_parent.exists() and os.access(str(root_parent), os.W_OK | os.X_OK)
    add("diagnostics directory", "ok" if root_writable else "fail",
        "%s (%s)" % (root, "exists" if root.exists() else "will be created on first run"), "paths")
    jsonl_count = 0
    jsonl_scan_capped = False
    if root.is_dir():
        try:
            walk_state = {"capped": False}
            for candidate in _bounded_tree(
                    root, walk_state, MAX_DISCOVERY_ENTRIES):
                if candidate.suffix.lower() != ".jsonl":
                    continue
                try:
                    if stat.S_ISREG(candidate.lstat().st_mode):
                        jsonl_count += 1
                except OSError:
                    continue
            jsonl_scan_capped = walk_state["capped"]
        except OSError:
            pass
    add("structured logs", "ok" if jsonl_count else "warn",
        "%s%d JSONL file(s) under %s" % (
            "at least " if jsonl_scan_capped else "", jsonl_count, root),
        "paths")
    try:
        usage = shutil.disk_usage(str(root_parent))
        free_gib = usage.free / float(1024 ** 3)
        add("free disk", "ok" if free_gib >= 1.0 else "warn",
            "%.1f GiB free on filesystem containing diagnostics" % free_gib, "paths")
    except OSError as exc:
        add("free disk", "warn", "unavailable: %s" % exc, "paths")

    for env_name, default, label in (("ECHOECHO_VIEWER_PORT", 8765, "viewer/daemon"),
                                     ("LIVEWRITER_PORT", 8799, "Live Writer")):
        port = _port_env(env_name, default)
        if port is None:
            add(label + " port", "fail", "%s is not a valid TCP port" % env_name, "services")
        else:
            listening = _port_status(port)
            add(label + " port", "ok" if listening else "warn",
                "127.0.0.1:%d %s" % (port, "is listening" if listening else "is free/not running"),
                "services")
    return {
        "ok": not any(item["status"] == "fail" for item in checks),
        "repo": str(repo), "diagnostics_dir": str(root), "checks": checks,
    }


def print_doctor(result):
    print("echoecho doctor")
    current = None
    for item in result["checks"]:
        if item["category"] != current:
            current = item["category"]
            print("\n%s:" % current)
        marker = {"ok": "OK", "warn": "WARN", "fail": "FAIL"}[item["status"]]
        print("  %-4s %-28s %s" % (marker, item["name"], item["detail"]))
    failures = sum(item["status"] == "fail" for item in result["checks"])
    warnings = sum(item["status"] == "warn" for item in result["checks"])
    print("\nresult: %s (%d warning%s, %d failure%s)" % (
        "ready" if not failures else "needs attention", warnings, "" if warnings == 1 else "s",
        failures, "" if failures == 1 else "s"))


def main(argv=None):
    args = parse_args(argv)
    args.dir = args.dir.expanduser()
    if args.doctor:
        result = doctor(args.dir.resolve())
        if args.json:
            print(json.dumps(redact(result), indent=2, ensure_ascii=False, sort_keys=True))
        else:
            print_doctor(result)
        return 0 if result["ok"] else 1

    root, runs, discovery, filters = discover(args)
    payload = {
        "diagnostics_dir": str(root),
        "filters": {"components": filters, "minimum_level": args.level,
                    "latest": args.latest, "tail": args.tail, "slow_ms": args.slow_ms},
        "discovery": redact(discovery),
        "runs": [run.as_dict(root) for run in runs],
    }
    if args.json:
        print(json.dumps(redact(payload), indent=2, ensure_ascii=False, sort_keys=True))
    else:
        print_summary(root, runs, discovery, args, filters)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        # `diagnostics | head` is a normal inspection pattern.
        try:
            sys.stdout.close()
        except OSError:
            pass
        sys.exit(0)
