"""Best-effort structured diagnostics for echoecho.

This module is deliberately independent of the product event feed.  Product
events may contain transcripts and other user-authored material; diagnostics
are operational records and redact content-bearing fields by default.

The module-level API is inert until :func:`configure` is called.  A configured
run writes newline-delimited JSON to one private file under
``~/.echoecho/diagnostics`` (or ``ECHOECHO_DIAGNOSTICS_DIR``), with a small
``latest.json`` pointer for tooling.  Every public operation is best-effort:
diagnostics must never become a reason the application fails.
"""

import atexit
import contextvars
import datetime as _datetime
import hashlib
import itertools
import json
import math
import os
import re
import stat
import sys
import tempfile
import threading
import time
import traceback
import uuid
from collections import Counter
from pathlib import Path

try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - Windows fallback uses the marker PID.
    _fcntl = None


__all__ = [
    "Run", "SCHEMA_VERSION", "configure", "get_run_id", "get_log_path",
    "get_context", "new_id", "context", "span", "debug", "info",
    "warning", "error", "exception", "metric", "counter",
    "install_asyncio", "shutdown",
]


SCHEMA_VERSION = 1
DEFAULT_RETENTION_DAYS = 14
DEFAULT_MAX_RUNS = 40
DEFAULT_MAX_EVENT_BYTES = 32 * 1024
DEFAULT_MAX_RUN_BYTES = 10 * 1024 * 1024
DEFAULT_MAX_PARTS = 10
DEFAULT_MAX_STRING = 1200
DEFAULT_MAX_ITEMS = 50
DEFAULT_MAX_DEPTH = 6
DEFAULT_MAX_NODES = 512
DEFAULT_MAX_STACK = 12 * 1024
MAX_RETENTION_DAYS = 3650
MAX_RUNS_LIMIT = 200
MAX_EVENT_BYTES_LIMIT = 1024 * 1024
MAX_RUN_BYTES_LIMIT = 100 * 1024 * 1024
MAX_PARTS_LIMIT = 100
MAX_STRING_LIMIT = 64 * 1024
MAX_ITEMS_LIMIT = 200
MAX_DEPTH_LIMIT = 12
MAX_NODES_LIMIT = 4096
MAX_SUMMARY_KEYS = 256
MAX_CLASSIFICATION_KEY = 1024
MAX_POINTER_BYTES = 256 * 1024
MAX_ACTIVE_MARKER_BYTES = 1024
MAX_ACTIVE_MARKERS = 256
MAX_RETENTION_ENTRIES = 50000
MAX_RETENTION_FILES = 25000
MAX_RETENTION_RUNS = 1000

_REDACTED = "[REDACTED]"
_CONTENT_REDACTED = "[CONTENT REDACTED]"
_TRUNCATED = "[TRUNCATED]"

# These names almost always carry credentials.  Matching is conservative for
# compound names: ``token_count`` is useful telemetry, while ``access_token``
# is a secret.
_SENSITIVE_KEYS = {
    "authorization", "proxy_authorization", "api_key", "apikey",
    "openai_api_key", "anthropic_api_key", "password", "passwd", "pwd",
    "secret", "client_secret", "access_token", "refresh_token", "id_token",
    "viewer_token", "token", "auth", "bearer", "cookie", "set_cookie", "credential",
    "credentials", "private_key", "ssh_key", "vnc_url", "key",
    "secret_key",
}
_SENSITIVE_SUFFIXES = (
    "_api_key", "_password", "_passwd", "_secret", "_access_token",
    "_refresh_token", "_private_key", "_viewer_token", "_token",
    "_secret_key",
)

# User-authored material stays out of diagnostics unless a developer explicitly
# configures ``include_content=True``.  Metadata such as lengths, ids, statuses,
# counts, and hashes remains available and is the preferred instrumentation.
_CONTENT_KEYS = {
    "text", "transcript", "transcription", "instructions", "instruction",
    "prompt", "content", "audio", "pcm", "delta", "args", "arguments",
    "output", "stdout", "stderr", "query", "document", "markdown", "say",
    "request_body", "response_body", "body", "message", "detail", "details",
}
_CONTENT_SUFFIXES = (
    "_text", "_transcript", "_instructions", "_prompt", "_content",
    "_audio", "_pcm", "_arguments", "_args", "_stdout", "_stderr",
    "_document", "_markdown",
)

# Measurements about sensitive/content values are useful and do not contain
# the values themselves. Check these suffixes before segment matching so
# ``token_count`` and ``transcript_chars`` survive while ``token_value`` and
# ``transcript_preview`` do not.
_SAFE_METADATA_SUFFIXES = {
    "available", "bytes", "chars", "count", "counts", "depth", "digest",
    "duration_ms", "enabled", "encoding", "fingerprint", "format", "frames",
    "hash", "high_water", "latency_ms", "len", "length", "ms", "peak",
    "percent", "present", "rate", "ratio", "returncode", "s", "seconds",
    "size", "status", "tokens", "type", "version",
}
_SUMMARY_COUNT_MAP_KEYS = frozenset({
    "event_counts", "counters", "latest_metrics",
})

_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_API_KEY_RE = re.compile(
    r"(?i)\b(?:sk-[A-Za-z0-9_-]{8,}|ghp_[A-Za-z0-9]{12,}|"
    r"github_pat_[A-Za-z0-9_]{12,}|xox[baprs]-[A-Za-z0-9-]{8,}|"
    r"AKIA[0-9A-Z]{12,})\b")
_JWT_RE = re.compile(
    r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\."
    r"[A-Za-z0-9_-]{8,}\b")
_ASSIGNMENT_RE = re.compile(
    r"(?i)\b([A-Z][A-Z0-9_]*(?:_KEY|_TOKEN|_PASSWORD|_SECRET)|api[_-]?key|"
    r"access[_-]?token|refresh[_-]?token|auth[_-]?token|password|passwd|"
    r"secret|authorization)\s*([:=])\s*([^\s,;]+)")
_QUERY_SECRET_RE = re.compile(
    r"(?i)([?&](?:token|api[_-]?key|password|secret|authorization)=)[^&#\s]+")
_URI_USERINFO_RE = re.compile(r"(://)[^/@\s]+@")
_RUN_ID_RE = re.compile(r"_(run-[0-9a-f]+)_")
_ACTIVE_MARKER_RE = re.compile(
    r"^active-(run-[0-9a-f]{16})-p([1-9][0-9]{0,9})-"
    r"([A-Za-z0-9_](?:[A-Za-z0-9_.-]{0,78}[A-Za-z0-9_])?)\.json$")
_MARKER_TIMESTAMP_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:"
    r"[0-9]{2}\.[0-9]{3}Z$")

_context_fields = contextvars.ContextVar("echoecho_diagnostics_context", default={})
_active = None
_active_lock = threading.RLock()


def _utc_iso(wall=None):
    wall = time.time() if wall is None else wall
    dt = _datetime.datetime.fromtimestamp(wall, tz=_datetime.timezone.utc)
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _bounded_int(value, default, minimum=0, maximum=None):
    try:
        result = max(minimum, int(value))
    except (TypeError, ValueError):
        return default
    return min(maximum, result) if maximum is not None else result


def _env_int(name, default, minimum=0, maximum=None):
    return _bounded_int(os.environ.get(name, default), default,
                        minimum, maximum)


def _bounded_float(value, default, minimum=0.0, maximum=None):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(result):
        return default
    result = max(minimum, result)
    return min(maximum, result) if maximum is not None else result


def _env_float(name, default, minimum=0.0, maximum=None):
    return _bounded_float(os.environ.get(name, default), default,
                          minimum, maximum)


def _env_flag(name, default=False):
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("", "0", "false", "no", "off")


def _safe_name(value, fallback="app"):
    try:
        raw = str(value or "").strip()
    except Exception:
        raw = ""
    name = re.sub(r"[^A-Za-z0-9_.-]+", "-", raw)
    name = name.strip(".-")[:80]
    return name or fallback


def _key_name(value):
    # Normalize camelCase/PascalCase as well as punctuation-delimited keys so
    # SDK-shaped fields such as ``accessToken`` cannot bypass the same rules
    # as ``access_token``.
    raw = str(value).strip()
    raw = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", raw)
    raw = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", raw)
    return re.sub(r"[^a-z0-9]+", "_", raw.lower()).strip("_")


def _classification_key(value):
    """Return a full key for privacy rules, or fail closed if it is huge."""
    try:
        raw = str(value)
    except Exception:
        return "unprintable_secret"
    # Processing an attacker-sized key through several regexes is unnecessary,
    # and a prefix-only classification would let a secret-bearing suffix hide
    # beyond the display-key truncation. Treat exceptionally long keys as
    # sensitive instead.
    if len(raw) > MAX_CLASSIFICATION_KEY:
        return "oversized_secret"
    return raw


def _is_sensitive_key(key):
    name = _key_name(key)
    return any(re.search(r"(?:^|_)%s(?:_|$)" % re.escape(term), name)
               for term in _SENSITIVE_KEYS)


def _is_content_key(key):
    name = _key_name(key)
    return any(re.search(r"(?:^|_)%s(?:_|$)" % re.escape(term), name)
               for term in _CONTENT_KEYS)


def _is_safe_metadata(key, value):
    """Allow measurements, never arbitrary values, through broad key rules."""
    name = _key_name(key)
    suffix = next((item for item in _SAFE_METADATA_SUFFIXES
                   if name == item or name.endswith("_" + item)), None)
    if suffix is None:
        return False
    if value is None or isinstance(value, (bool, int, float)):
        return True
    if suffix in {"fingerprint", "hash", "digest"} and isinstance(value, str):
        return re.fullmatch(r"[0-9a-fA-F]{8,128}", value) is not None
    return False


def _redact_string(value, limit=DEFAULT_MAX_STRING):
    """Redact common secret shapes and bound a string without ever raising."""
    try:
        text = str(value)
    except Exception:
        text = "<unprintable %s>" % type(value).__name__
    text = _BEARER_RE.sub("Bearer " + _REDACTED, text)
    text = _API_KEY_RE.sub(_REDACTED, text)
    text = _JWT_RE.sub(_REDACTED, text)
    text = _ASSIGNMENT_RE.sub(lambda m: "%s%s%s" % (m.group(1), m.group(2), _REDACTED), text)
    text = _QUERY_SECRET_RE.sub(lambda m: m.group(1) + _REDACTED, text)
    text = _URI_USERINFO_RE.sub(r"\1" + _REDACTED + "@", text)
    try:
        home = str(Path.home())
        if home and home != "/":
            text = text.replace(home, "~")
    except Exception:
        pass
    if len(text) > limit:
        omitted = len(text) - limit
        text = text[:limit] + "…[%d chars omitted]" % omitted
    return text


def _safe_repr(value):
    try:
        return repr(value)
    except Exception:
        return "<unprintable %s>" % type(value).__name__


def _stack_filename(value):
    """Keep a call-site basename without exposing local directory structure."""
    text = _redact_string(value, 500)
    if text.startswith("<") and text.endswith(">"):
        return text
    try:
        return Path(text).name or "<redacted-path>"
    except Exception:
        return "<redacted-path>"


def _bounded_summary_key(mapping, key, overflow):
    """Bound long-lived counter/metric cardinality for hostile plugin names."""
    if key in mapping:
        return key
    # Reserve the final slot for all future unseen keys.
    return key if len(mapping) < MAX_SUMMARY_KEYS - 1 else overflow


def _sanitize(value, key=None, depth=0, seen=None, include_content=False,
              max_string=DEFAULT_MAX_STRING, max_items=DEFAULT_MAX_ITEMS,
              max_depth=DEFAULT_MAX_DEPTH, max_nodes=DEFAULT_MAX_NODES,
              budget=None):
    """Return a JSON-safe, redacted, recursively bounded representation."""
    if budget is None:
        budget = [max_nodes]
    if budget[0] <= 0:
        return _TRUNCATED
    budget[0] -= 1
    safe_metadata = key is not None and _is_safe_metadata(key, value)
    if key is not None and _is_sensitive_key(key) and not safe_metadata:
        return _REDACTED
    if (key is not None and _is_content_key(key) and not safe_metadata
            and not include_content):
        return _CONTENT_REDACTED
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        # JSON's NaN/Infinity extensions are poor diagnostics interchange.
        return value if value == value and abs(value) != float("inf") else str(value)
    if isinstance(value, (str, bytes, bytearray, Path)):
        if isinstance(value, (bytes, bytearray)):
            return "<%d bytes>" % len(value)
        return _redact_string(value, max_string)
    if depth >= max_depth:
        return "<max depth: %s>" % type(value).__name__

    seen = set() if seen is None else seen
    marker = id(value)
    if marker in seen:
        return "<cycle: %s>" % type(value).__name__

    if isinstance(value, dict):
        seen.add(marker)
        out = {}
        container_key = _key_name(key or "")
        try:
            # Never materialize an unbounded mapping merely to truncate it.
            items = list(itertools.islice(value.items(), max_items + 1))
        except Exception:
            seen.discard(marker)
            return _redact_string(_safe_repr(value), max_string)
        for raw_key, child in items[:max_items]:
            child_key = _redact_string(raw_key, 120)
            classification_key = _classification_key(raw_key)
            # Run summaries map controlled, already-redacted operational
            # names to numbers. Do not mistake names such as
            # ``text_repl.started`` for content-bearing field keys.
            if (container_key in _SUMMARY_COUNT_MAP_KEYS and
                    (child is None or isinstance(child, (bool, int, float)))):
                classification_key = "summary_count"
            # Truncated display keys can collide. Preserve each field so a
            # later value cannot silently replace an earlier redaction.
            base_key = child_key
            suffix = 2
            while child_key in out:
                child_key = "%s#%d" % (base_key[:112], suffix)
                suffix += 1
            out[child_key] = _sanitize(
                child, key=classification_key, depth=depth + 1, seen=seen,
                include_content=include_content, max_string=max_string,
                max_items=max_items, max_depth=max_depth,
                max_nodes=max_nodes, budget=budget)
        if len(items) > max_items:
            try:
                omitted = max(1, len(value) - max_items)
            except Exception:
                omitted = 1
            out["_truncated_items"] = omitted
        seen.discard(marker)
        return out

    if isinstance(value, (list, tuple, set, frozenset)):
        seen.add(marker)
        try:
            if isinstance(value, (list, tuple)):
                values = value[:max_items + 1]
            else:
                values = list(itertools.islice(value, max_items + 1))
        except Exception:
            seen.discard(marker)
            return _redact_string(_safe_repr(value), max_string)
        out = [_sanitize(
            child, depth=depth + 1, seen=seen, include_content=include_content,
            max_string=max_string, max_items=max_items, max_depth=max_depth,
            max_nodes=max_nodes, budget=budget)
               for child in values[:max_items]]
        if len(values) > max_items:
            try:
                omitted = max(1, len(value) - max_items)
            except Exception:
                omitted = 1
            out.append({"_truncated_items": omitted})
        seen.discard(marker)
        return out

    # Exception messages can contain prompts, response bodies, paths, and
    # credentials even when an exception is supplied as an ordinary field.
    # Apply the same privacy policy as the dedicated ``exception=`` channel.
    if isinstance(value, BaseException):
        return _exception_record(value, include_content=include_content)

    # Dataclasses, SDK objects, and arbitrary plugin values stay useful as a
    # short type-tagged repr without inviting custom serializers.
    return _redact_string(_safe_repr(value), max_string)


def _exception_record(exc, include_content=False, max_stack=DEFAULT_MAX_STACK):
    if exc is None:
        return None
    try:
        raw_message = str(exc)
    except Exception:
        raw_message = _safe_repr(exc)
    try:
        if include_content:
            stack = "".join(traceback.format_exception(
                type(exc), exc, exc.__traceback__))
        else:
            # traceback.format_tb also includes source-code lines. A dynamic
            # script or generated worker can put user content directly in that
            # source, so construct location-only frames instead.
            frames = traceback.extract_tb(exc.__traceback__)
            stack = "".join(
                '  File "%s", line %d, in %s\n' % (
                    _stack_filename(frame.filename), frame.lineno,
                    _redact_string(frame.name, 160))
                for frame in frames[-50:])
            stack += "%s.%s\n" % (
                getattr(type(exc), "__module__", ""), type(exc).__name__)
    except Exception:
        stack = type(exc).__name__
    stack = _redact_string(stack, max_stack)
    return {
        "type": type(exc).__name__,
        "module": getattr(type(exc), "__module__", ""),
        "message": (_redact_string(raw_message, DEFAULT_MAX_STRING)
                    if include_content else _CONTENT_REDACTED),
        "message_length": len(raw_message),
        "fingerprint": hashlib.sha256(
            (type(exc).__name__ + "\0" + raw_message).encode(
                "utf-8", "replace")).hexdigest()[:16],
        "stack": stack,
    }


def _write_json_atomic(path, obj):
    """Private atomic JSON write.  Callers guard every failure."""
    data = json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=".%s." % path.name, suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fd = None
            fh.write(data)
            fh.flush()
            os.fchmod(fh.fileno(), 0o600)
        os.replace(str(tmp), str(path))
    except BaseException:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.unlink(str(tmp))
        except OSError:
            pass
        raise


def _read_json_regular_nofollow(path, max_bytes=MAX_POINTER_BYTES,
                                return_identity=False, return_fd=False):
    """Read one bounded regular JSON file without following a final symlink."""
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise OSError("diagnostics pointer is not a regular file")
    if info.st_size > max_bytes:
        raise ValueError("diagnostics pointer is too large")
    flags = os.O_RDONLY
    for option in ("O_CLOEXEC", "O_NOFOLLOW", "O_NONBLOCK"):
        flags |= getattr(os, option, 0)
    fd = os.open(str(path), flags)
    keep_fd = False
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise OSError("diagnostics pointer is not a regular file")
        if opened.st_size > max_bytes:
            raise ValueError("diagnostics pointer is too large")
        chunks = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(fd, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > max_bytes:
            raise ValueError("diagnostics pointer is too large")
        data = json.loads(raw.decode("utf-8"))
        if return_fd:
            keep_fd = True
            return data, (opened.st_dev, opened.st_ino), fd
        if return_identity:
            return data, (opened.st_dev, opened.st_ino)
        return data
    finally:
        if not keep_fd:
            os.close(fd)


def _unlink_regular_identity(path, identity):
    """Unlink only the same regular inode a caller previously opened."""
    try:
        current = path.lstat()
        if not stat.S_ISREG(current.st_mode):
            return False
        if (current.st_dev, current.st_ino) != identity:
            return False
        path.unlink()
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return False


def _write_json_exclusive_private(path, obj, max_bytes, hold_lock=False):
    """Create one bounded private JSON file without replacing any entry."""
    raw = (json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    if len(raw) > max_bytes:
        raise ValueError("diagnostics marker is too large")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    for option in ("O_CLOEXEC", "O_NOFOLLOW"):
        flags |= getattr(os, option, 0)
    fd = None
    identity = None
    try:
        fd = os.open(str(path), flags, 0o600)
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise OSError("diagnostics marker is not a regular file")
        identity = (opened.st_dev, opened.st_ino)
        os.fchmod(fd, 0o600)
        if hold_lock and _fcntl is not None:
            _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        offset = 0
        while offset < len(raw):
            written = os.write(fd, raw[offset:])
            if written <= 0:
                raise OSError("short diagnostics marker write")
            offset += written
        if hold_lock and _fcntl is not None:
            held_fd = fd
            fd = None
            return identity, held_fd
        os.close(fd)
        fd = None
        return identity, None
    except BaseException:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if identity is not None:
            _unlink_regular_identity(path, identity)
        raise


def _marker_lock_is_held(fd):
    """Return True/False for a POSIX marker lock, or None if unsupported."""
    if _fcntl is None:
        return None
    try:
        _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    except OSError:
        # Some shared filesystems do not implement flock. PID liveness remains
        # the portable fallback rather than making diagnostics load-bearing.
        return None
    try:
        _fcntl.flock(fd, _fcntl.LOCK_UN)
    except OSError:
        pass
    return False


def new_id(prefix):
    """Return a short, process-independent correlation id."""
    return "%s-%s" % (_safe_name(prefix, "id"), uuid.uuid4().hex[:16])


class Run(object):
    """One process run and its component-specific JSONL file."""

    def __init__(self, component, mode=None, log_dir=None, **fields):
        # Optional tuning travels through **fields to preserve configure's small,
        # stable public signature.
        self.retention_days = _bounded_float(fields.pop(
            "retention_days", _env_float("ECHOECHO_DIAGNOSTICS_RETENTION_DAYS",
                                          DEFAULT_RETENTION_DAYS, 0,
                                          MAX_RETENTION_DAYS)),
            DEFAULT_RETENTION_DAYS, 0, MAX_RETENTION_DAYS)
        self.max_runs = _bounded_int(fields.pop(
            "max_runs", _env_int("ECHOECHO_DIAGNOSTICS_MAX_RUNS",
                                  DEFAULT_MAX_RUNS, 1, MAX_RUNS_LIMIT)),
            DEFAULT_MAX_RUNS, 1, MAX_RUNS_LIMIT)
        self.max_event_bytes = _bounded_int(fields.pop(
            "max_event_bytes", _env_int("ECHOECHO_DIAGNOSTICS_MAX_EVENT_BYTES",
                                         DEFAULT_MAX_EVENT_BYTES, 2048,
                                         MAX_EVENT_BYTES_LIMIT)),
            DEFAULT_MAX_EVENT_BYTES, 2048, MAX_EVENT_BYTES_LIMIT)
        self.max_run_bytes = _bounded_int(fields.pop(
            "max_run_bytes", _env_int("ECHOECHO_DIAGNOSTICS_MAX_RUN_BYTES",
                                       DEFAULT_MAX_RUN_BYTES, 64 * 1024,
                                       MAX_RUN_BYTES_LIMIT)),
            DEFAULT_MAX_RUN_BYTES, 64 * 1024, MAX_RUN_BYTES_LIMIT)
        self.max_parts = _bounded_int(fields.pop(
            "max_parts", _env_int("ECHOECHO_DIAGNOSTICS_MAX_PARTS",
                                    DEFAULT_MAX_PARTS, 1, MAX_PARTS_LIMIT)),
            DEFAULT_MAX_PARTS, 1, MAX_PARTS_LIMIT)
        self.max_string = _env_int(
            "ECHOECHO_DIAGNOSTICS_MAX_STRING", DEFAULT_MAX_STRING,
            128, MAX_STRING_LIMIT)
        self.max_items = _env_int(
            "ECHOECHO_DIAGNOSTICS_MAX_ITEMS", DEFAULT_MAX_ITEMS,
            5, MAX_ITEMS_LIMIT)
        self.max_depth = _env_int(
            "ECHOECHO_DIAGNOSTICS_MAX_DEPTH", DEFAULT_MAX_DEPTH,
            2, MAX_DEPTH_LIMIT)
        self.max_nodes = _bounded_int(fields.pop(
            "max_nodes", _env_int("ECHOECHO_DIAGNOSTICS_MAX_NODES",
                                   DEFAULT_MAX_NODES, 32, MAX_NODES_LIMIT)),
            DEFAULT_MAX_NODES, 32, MAX_NODES_LIMIT)
        self.include_content = bool(fields.pop(
            "include_content", _env_flag("ECHOECHO_DIAGNOSTICS_INCLUDE_CONTENT")))

        self.component = _safe_name(component)
        self.mode = _redact_string(mode, 80) if mode is not None else None
        self.run_id = new_id("run")
        self.started_wall = time.time()
        self.started_mono_ns = time.monotonic_ns()
        self.directory = Path(log_dir).expanduser() if log_dir is not None else Path(
            os.environ.get("ECHOECHO_DIAGNOSTICS_DIR", "") or
            (Path.home() / ".echoecho" / "diagnostics")).expanduser()
        self.path = None
        self._base_path = None
        self._fh = None
        self._part = 0
        self._part_paths = []
        self._bytes = 0
        self._rotation_retention_failed = False
        self._lock = threading.RLock()
        self._seq = 0
        self._closed = False
        self._write_failures = 0
        self._dropped_events = 0
        self._level_counts = Counter()
        self._event_counts = Counter()
        self._counters = Counter()
        self._metric_latest = {}
        self._pointer = None
        self._active_marker = None
        self._active_marker_identity = None
        self._active_marker_fd = None
        self._previous_sys_hook = None
        self._previous_thread_hook = None
        self._previous_unraisable_hook = None
        self._sys_hook = None
        self._thread_hook = None
        self._unraisable_hook = None
        self._asyncio_hooks = {}
        self._run_fields = dict(fields)

        self._open_file()
        if self._fh is not None:
            self._install_process_hooks()
        self.emit("run.start", level="info", mode=self.mode, **self._run_fields)

    @property
    def enabled(self):
        return self._fh is not None and not self._closed

    def _open_file(self):
        # Keep correlation ids available to the UI feed even when an operator
        # explicitly disables disk diagnostics.
        if not _env_flag("ECHOECHO_DIAGNOSTICS", True):
            return
        try:
            directory_existed = self.directory.exists()
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            # Do not silently change permissions on an operator-supplied
            # existing directory (for example a shared diagnostic volume).
            # Each file and pointer remains private regardless.
            if not directory_existed:
                try:
                    os.chmod(str(self.directory), 0o700)
                except OSError:
                    pass
            if not self._create_active_marker():
                self._write_failures += 1
                return
            if not self._apply_retention():
                # If a bounded scan cannot establish/enforce retention, do not
                # add another file. Diagnostics degrade to no-op while the app
                # itself continues normally.
                self._remove_active_marker()
                self._write_failures += 1
                return
            stamp = _datetime.datetime.now(_datetime.timezone.utc).strftime(
                "%Y%m%dT%H%M%S.%fZ")
            name = "%s_%s_%s.jsonl" % (stamp, self.run_id, self.component)
            self._base_path = self.directory / name
            self.path = self._base_path
            fd = os.open(str(self.path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                self._fh = os.fdopen(fd, "a", encoding="utf-8", buffering=1)
            except BaseException:
                os.close(fd)
                try:
                    self.path.unlink()
                except OSError:
                    pass
                raise
            self._bytes = 0
            self._part_paths = [self.path]
            self._write_pointer()
        except Exception:
            self._remove_active_marker()
            self.path = None
            self._fh = None
            self._write_failures += 1

    def _create_active_marker(self):
        """Publish this run's independently discoverable liveness marker."""
        try:
            pid = os.getpid()
            name = "active-%s-p%d-%s.json" % (
                self.run_id, pid, self.component)
            if _ACTIVE_MARKER_RE.fullmatch(name) is None:
                return False
            marker = self.directory / name
            identity, marker_fd = _write_json_exclusive_private(marker, {
                "schema_version": SCHEMA_VERSION,
                "run_id": self.run_id,
                "component": self.component,
                "pid": pid,
                "started_at": _utc_iso(self.started_wall),
            }, MAX_ACTIVE_MARKER_BYTES, hold_lock=True)
            self._active_marker = marker
            self._active_marker_identity = identity
            self._active_marker_fd = marker_fd
            return True
        except Exception:
            return False

    def _remove_active_marker(self):
        marker = self._active_marker
        identity = self._active_marker_identity
        marker_fd = self._active_marker_fd
        self._active_marker = None
        self._active_marker_identity = None
        self._active_marker_fd = None
        if marker is None or identity is None:
            if marker_fd is not None:
                try:
                    os.close(marker_fd)
                except OSError:
                    pass
            return True
        removed = _unlink_regular_identity(marker, identity)
        if marker_fd is not None:
            try:
                if _fcntl is not None:
                    _fcntl.flock(marker_fd, _fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(marker_fd)
            except OSError:
                pass
        return removed

    def _pointer_data(self, ended_at=None, outcome=None):
        data = {
            "schema_version": SCHEMA_VERSION,
            "run_id": self.run_id,
            "component": self.component,
            "mode": self.mode,
            "started_at": _utc_iso(self.started_wall),
            "pid": os.getpid(),
            "log_file": self.path.name if self.path is not None else None,
            "log_part": self._part,
            "log_files": [path.name for path in self._part_paths
                          if path.exists()],
        }
        if ended_at is not None:
            data["ended_at"] = ended_at
        if outcome is not None:
            data["outcome"] = _redact_string(outcome, 80)
        return data

    def _write_pointer(self, ended_at=None, outcome=None):
        if self.path is None:
            return
        try:
            data = self._pointer_data(ended_at=ended_at, outcome=outcome)
            self._pointer = self.directory / "latest.json"
            _write_json_atomic(self._pointer, data)
            _write_json_atomic(
                self.directory / ("latest-%s.json" % self.component), data)
        except Exception:
            self._write_failures += 1

    def _apply_retention(self):
        """Delete only old diagnostics JSONL files inside the configured dir."""
        try:
            # Multiple long-lived components share this directory.  A quiet
            # daemon's mtime can be older than many short Live Writer runs, but
            # unlinking its open file would make every later write invisible.
            # Each run owns a private active marker, so overlapping runs of the
            # same component remain independently discoverable even though the
            # component's latest pointer names only one of them.
            markers = []
            files = []
            scan_complete = True
            marker_limit_exceeded = False
            with os.scandir(str(self.directory)) as entries:
                for entries_seen, entry in enumerate(entries, 1):
                    if entries_seen > MAX_RETENTION_ENTRIES:
                        scan_complete = False
                        break
                    name = entry.name
                    try:
                        regular = entry.is_file(follow_symlinks=False)
                    except OSError:
                        continue
                    if not regular:
                        continue
                    if _ACTIVE_MARKER_RE.fullmatch(name) is not None:
                        if len(markers) >= MAX_ACTIVE_MARKERS:
                            # Keep scanning bounded files/entries, and clean the
                            # markers already collected below. A later startup
                            # can then make progress through a crash pile-up.
                            marker_limit_exceeded = True
                            continue
                        markers.append((self.directory / name, name))
                    elif name.endswith(".jsonl") and _RUN_ID_RE.search(name):
                        if len(files) >= MAX_RETENTION_FILES:
                            scan_complete = False
                            break
                        try:
                            files.append((
                                self.directory / name,
                                entry.stat(follow_symlinks=False).st_mtime))
                        except OSError:
                            continue
            if not scan_complete:
                return False

            active_run_ids = set()
            for marker, name in markers:
                try:
                    match = _ACTIVE_MARKER_RE.fullmatch(name)
                    if match is None:
                        continue
                    data, identity, marker_fd = _read_json_regular_nofollow(
                        marker, max_bytes=MAX_ACTIVE_MARKER_BYTES,
                        return_fd=True)
                    try:
                        if not isinstance(data, dict) or set(data) != {
                                "schema_version", "run_id", "component", "pid",
                                "started_at"}:
                            continue
                        run_id, pid_text, component = match.groups()
                        pid = int(pid_text)
                        if (type(data["schema_version"]) is not int or
                                data["schema_version"] != SCHEMA_VERSION or
                                data["run_id"] != run_id or
                                data["component"] != component or
                                type(data["pid"]) is not int or
                                data["pid"] != pid or pid > 2147483647 or
                                not isinstance(data["started_at"], str) or
                                _MARKER_TIMESTAMP_RE.fullmatch(
                                    data["started_at"]) is None):
                            continue
                        locked = _marker_lock_is_held(marker_fd)
                        if locked is None:
                            try:
                                os.kill(pid, 0)
                            except ProcessLookupError:
                                locked = False
                            except PermissionError:
                                locked = True
                            else:
                                locked = True
                        if locked:
                            active_run_ids.add(run_id)
                        else:
                            # Kernel locks are released on a crash and cannot
                            # be fooled by PID reuse. On platforms/filesystems
                            # without flock, exact-PID liveness is the fallback.
                            _unlink_regular_identity(marker, identity)
                    finally:
                        os.close(marker_fd)
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    continue
            if marker_limit_exceeded:
                # Never accept new runs when the active-marker namespace cannot
                # be fully enumerated within its hard cap. Dead markers among
                # the bounded prefix were still cleaned above for recovery.
                return False
            groups = {}
            for path, mtime in files:
                match = _RUN_ID_RE.search(path.name)
                # Other producers (the Electron app) share this directory and
                # own their own rotation/retention.  Never delete files whose
                # naming scheme doesn't prove they belong to this writer.
                if match is None:
                    continue
                run_key = match.group(1)
                if run_key not in groups and len(groups) >= MAX_RETENTION_RUNS:
                    return False
                parts = groups.setdefault(run_key, [])
                if len(parts) >= MAX_PARTS_LIMIT:
                    return False
                parts.append((path, mtime))
            ordered = sorted(
                groups.items(),
                key=lambda item: max(mtime for _path, mtime in item[1]),
                reverse=True)
            cutoff = (time.time() - self.retention_days * 86400.0
                      if self.retention_days > 0 else None)
            for index, (_run_key, paths) in enumerate(ordered):
                if _run_key in active_run_ids:
                    continue
                newest = max(mtime for _path, mtime in paths)
                expired = cutoff is not None and newest < cutoff
                over_limit = index >= self.max_runs
                if not (expired or over_limit):
                    continue
                for path, _mtime in paths:
                    try:
                        path.unlink()
                    except OSError:
                        scan_complete = False
            return scan_complete
        except Exception:
            self._write_failures += 1
            return False

    def _rotate(self, next_bytes):
        """Open the next part before closing the current one.

        A creation failure leaves the working current handle in place, so a
        rotation problem cannot disable all subsequent diagnostics.
        """
        if self._fh is None or self._base_path is None:
            return False
        if self._bytes == 0:
            return next_bytes <= self.max_run_bytes
        if self._bytes + next_bytes <= self.max_run_bytes:
            return True
        # If an old part could not be removed, stop at the current part's cap.
        # Continuing to mint new part names would turn a permission failure or
        # hostile directory entry into unbounded disk growth.
        if self._rotation_retention_failed:
            return False
        next_part = self._part + 1
        next_path = self._base_path.with_name(
            "%s.part-%03d.jsonl" % (self._base_path.stem, next_part))
        fd = None
        created = False
        try:
            fd = os.open(str(next_path),
                         os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            created = True
            next_fh = os.fdopen(fd, "a", encoding="utf-8", buffering=1)
            fd = None
        except Exception:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            # A collision belongs to whoever created it; never unlink a
            # pre-planted symlink or regular file in a shared diagnostics dir.
            # Cleanup is only valid after our own exclusive create succeeded.
            if created:
                try:
                    next_path.unlink()
                except OSError:
                    pass
            self._write_failures += 1
            return False
        previous = self._fh
        self._fh = next_fh
        self.path = next_path
        self._part = next_part
        self._bytes = 0
        self._part_paths.append(next_path)
        try:
            previous.close()
        except Exception:
            self._write_failures += 1
        # Retain a bounded rolling window for a single noisy process.  Delete
        # only closed parts and keep the current handle even if unlink fails.
        while len(self._part_paths) > self.max_parts:
            expired = self._part_paths.pop(0)
            if expired == self.path:
                self._part_paths.insert(0, expired)
                break
            try:
                expired.unlink()
            except OSError:
                # It may remain on disk, but omitting it from the live pointer
                # prevents an ever-growing metadata list. Permanently stop
                # future rotations so the on-disk orphan count is bounded too.
                self._rotation_retention_failed = True
                self._write_failures += 1
                break
        self._write_pointer()
        return True

    def _install_process_hooks(self):
        try:
            self._previous_sys_hook = sys.excepthook

            def sys_hook(exc_type, exc, tb):
                try:
                    if exc is not None and exc.__traceback__ is None:
                        exc = exc.with_traceback(tb)
                    self.emit("process.uncaught_exception", level="critical",
                              exception=exc)
                except Exception:
                    pass
                previous = self._previous_sys_hook
                if previous is not None and previous is not sys_hook:
                    previous(exc_type, exc, tb)

            self._sys_hook = sys_hook
            sys.excepthook = sys_hook
        except Exception:
            pass

        try:
            if hasattr(threading, "excepthook"):
                self._previous_thread_hook = threading.excepthook

                def thread_hook(args):
                    self.emit(
                        "thread.uncaught_exception", level="critical",
                        exception=getattr(args, "exc_value", None),
                        thread_name=getattr(getattr(args, "thread", None),
                                            "name", None))
                    previous = self._previous_thread_hook
                    if previous is not None and previous is not thread_hook:
                        previous(args)

                self._thread_hook = thread_hook
                threading.excepthook = thread_hook
        except Exception:
            pass

        try:
            if hasattr(sys, "unraisablehook"):
                self._previous_unraisable_hook = sys.unraisablehook

                def unraisable_hook(args):
                    self.emit(
                        "process.unraisable_exception", level="error",
                        exception=getattr(args, "exc_value", None),
                        object_type=type(getattr(args, "object", None)).__name__)
                    previous = self._previous_unraisable_hook
                    if previous is not None and previous is not unraisable_hook:
                        previous(args)

                self._unraisable_hook = unraisable_hook
                sys.unraisablehook = unraisable_hook
        except Exception:
            pass

    def install_asyncio(self, loop=None):
        """Install an asyncio exception handler, preserving an existing one."""
        try:
            if loop is None:
                import asyncio
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = asyncio.get_event_loop()
            if loop in self._asyncio_hooks:
                return True
            previous = loop.get_exception_handler()

            def handler(active_loop, details):
                exc = details.get("exception") if isinstance(details, dict) else None
                message = details.get("message") if isinstance(details, dict) else None
                future = details.get("future") if isinstance(details, dict) else None
                task = details.get("task") if isinstance(details, dict) else None
                try:
                    safe_message = "" if message is None else str(message)
                except Exception:
                    safe_message = "<unprintable>"
                self.emit(
                    "asyncio.unhandled_exception", level="error", exception=exc,
                    message=message, message_length=len(safe_message),
                    message_fingerprint=hashlib.sha256(
                        safe_message.encode("utf-8", "replace")
                    ).hexdigest()[:16],
                    future_type=type(future).__name__ if future is not None else None,
                    task_type=type(task).__name__ if task is not None else None)
                try:
                    if previous is not None:
                        previous(active_loop, details)
                    else:
                        active_loop.default_exception_handler(details)
                except Exception:
                    pass

            self._asyncio_hooks[loop] = (previous, handler)
            loop.set_exception_handler(handler)
            return True
        except Exception:
            return False

    def _restore_hooks(self):
        try:
            if self._sys_hook is not None and sys.excepthook is self._sys_hook:
                sys.excepthook = self._previous_sys_hook
        except Exception:
            pass
        try:
            if (self._thread_hook is not None and hasattr(threading, "excepthook")
                    and threading.excepthook is self._thread_hook):
                threading.excepthook = self._previous_thread_hook
        except Exception:
            pass
        try:
            if (self._unraisable_hook is not None and hasattr(sys, "unraisablehook")
                    and sys.unraisablehook is self._unraisable_hook):
                sys.unraisablehook = self._previous_unraisable_hook
        except Exception:
            pass
        for loop, (previous, handler) in list(self._asyncio_hooks.items()):
            try:
                if not loop.is_closed() and loop.get_exception_handler() is handler:
                    loop.set_exception_handler(previous)
            except Exception:
                pass
        self._asyncio_hooks.clear()

    def _sanitized(self, value, key=None):
        return _sanitize(
            value, key=key, include_content=self.include_content,
            max_string=self.max_string, max_items=self.max_items,
            max_depth=self.max_depth, max_nodes=self.max_nodes)

    def _encode_bounded(self, record):
        encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":"),
                             sort_keys=False, allow_nan=False)
        size = len(encoded.encode("utf-8"))
        if size <= self.max_event_bytes:
            return encoded

        record["truncated"] = True
        record["original_bytes"] = size
        fields = record.get("fields") or {}
        record["fields"] = {
            "_truncated": True,
            "field_names": list(fields.keys())[:20] if isinstance(fields, dict) else [],
        }
        if record.get("context"):
            ctx = record["context"]
            record["context"] = {
                "_truncated": True,
                "field_names": list(ctx.keys())[:20] if isinstance(ctx, dict) else [],
            }
        exc = record.get("exception")
        if isinstance(exc, dict) and exc.get("stack"):
            exc["stack"] = _redact_string(exc["stack"], 1500)
        encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":"),
                             sort_keys=False, allow_nan=False)
        if len(encoded.encode("utf-8")) <= self.max_event_bytes:
            return encoded

        # A very small configured bound still retains the diagnostic identity.
        record.pop("exception", None)
        record["context"] = {"_truncated": True}
        record["fields"] = {"_truncated": True}
        return json.dumps(record, ensure_ascii=False, separators=(",", ":"),
                          sort_keys=False, allow_nan=False)

    def emit(self, event_name, level="info", duration_ms=None, exception=None,
             **fields):
        """Append one structured event; return False when the sink is unavailable."""
        try:
            if self._closed or self._fh is None:
                self._dropped_events += 1
                return False
            wall = time.time()
            mono_ms = (time.monotonic_ns() - self.started_mono_ns) / 1000000.0
            thread = threading.current_thread()
            clean_level = str(level or "info").strip().lower()
            if clean_level not in ("debug", "info", "warning", "error", "critical"):
                clean_level = "info"
            with self._lock:
                self._seq += 1
                record = {
                    "schema_version": SCHEMA_VERSION,
                    "run_id": self.run_id,
                    "seq": self._seq,
                    "ts": _utc_iso(wall),
                    "wall_time": round(wall, 6),
                    "monotonic_ms": round(mono_ms, 3),
                    "level": clean_level,
                    "event": _redact_string(event_name, 160),
                    "component": self.component,
                    "pid": os.getpid(),
                    "thread": {"name": _redact_string(thread.name, 100),
                               "ident": thread.ident},
                    "context": self._sanitized(dict(_context_fields.get() or {})),
                    "fields": self._sanitized(fields),
                }
                if duration_ms is not None:
                    try:
                        record["duration_ms"] = round(max(0.0, float(duration_ms)), 3)
                    except (TypeError, ValueError):
                        record["duration_ms"] = self._sanitized(duration_ms)
                exc_record = _exception_record(exception,
                                               include_content=self.include_content)
                if exc_record is not None:
                    record["exception"] = exc_record
                line = self._encode_bounded(record)
                line_bytes = len(line.encode("utf-8")) + 1
                if not self._rotate(line_bytes):
                    self._dropped_events += 1
                    return False
                self._fh.write(line + "\n")
                self._fh.flush()
                self._bytes += line_bytes
                self._level_counts[clean_level] += 1
                event_key = _bounded_summary_key(
                    self._event_counts, record["event"], "__other_events__")
                self._event_counts[event_key] += 1
            return True
        except Exception:
            self._write_failures += 1
            self._dropped_events += 1
            return False

    def metric(self, name, value, unit=None, **fields):
        try:
            safe_name = _redact_string(name, 120)
            with self._lock:
                key = _bounded_summary_key(
                    self._metric_latest, safe_name, "__other_metrics__")
                self._metric_latest[key] = self._sanitized(value)
                event_name = safe_name if key == safe_name else key
                return self.emit(
                    "metric", level="info", name=event_name, value=value,
                    unit=unit, **fields)
        except Exception:
            return False

    def counter(self, name, amount=1, **fields):
        try:
            safe_name = _redact_string(name, 120)
            numeric = float(amount)
            with self._lock:
                key = _bounded_summary_key(
                    self._counters, safe_name, "__other_counters__")
                self._counters[key] += numeric
                return self.emit(
                    "counter", level="info", name=key,
                    delta=numeric, value=self._counters[key], **fields)
        except Exception:
            return False

    def shutdown(self, outcome="ok", **fields):
        """Write the terminal summary, close, restore hooks, and apply retention."""
        with self._lock:
            if self._closed:
                return False
            duration_ms = (time.monotonic_ns() - self.started_mono_ns) / 1000000.0
            summary = {
                "outcome": outcome,
                "events_before_summary": sum(self._event_counts.values()),
                "level_counts": dict(self._level_counts),
                "event_counts": dict(self._event_counts.most_common(50)),
                "counters": dict(self._counters),
                "latest_metrics": dict(self._metric_latest),
                "write_failures": self._write_failures,
                "dropped_events": self._dropped_events,
            }
            for key, value in fields.items():
                safe_key = ("shutdown_%s" % key
                            if key in {"level", "duration_ms", "exception"}
                            else key)
                summary[safe_key] = value
            outcome_tokens = set(re.split(
                r"[^a-z0-9]+", str(outcome).strip().lower()))
            level = "error" if outcome_tokens.intersection({
                "error", "failed", "failure", "crash",
            }) else "info"
            try:
                self.emit("run.summary", level=level, duration_ms=duration_ms,
                          **summary)
            finally:
                self._closed = True
                try:
                    if self._fh is not None:
                        self._fh.close()
                except Exception:
                    self._write_failures += 1
                self._fh = None
                self._write_pointer(ended_at=_utc_iso(), outcome=outcome)
                self._restore_hooks()
                if not self._remove_active_marker():
                    self._write_failures += 1
                self._apply_retention()
            return True


class _BoundContext(object):
    def __init__(self, fields):
        self.fields = fields
        self._token = None

    def __enter__(self):
        current = dict(_context_fields.get() or {})
        current.update(self.fields)
        self._token = _context_fields.set(current)
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._token is not None:
            _context_fields.reset(self._token)
        return False

    async def __aenter__(self):
        return self.__enter__()

    async def __aexit__(self, exc_type, exc, tb):
        return self.__exit__(exc_type, exc, tb)


class _Span(object):
    def __init__(self, run, event, level, fields):
        self.run = run
        self.event = event
        self.level = level
        # Avoid collisions with Run.emit's control parameters and the span's
        # own terminal outcome. Instrumentation must remain safe even when a
        # caller uses one of those common domain-field names.
        self.fields = {
            ("caller_%s" % key if key in {
                "level", "duration_ms", "exception", "outcome"} else key): value
            for key, value in fields.items()
        }
        self.span_id = new_id("span")
        self.started_ns = None
        self._bound = None

    def __enter__(self):
        self.started_ns = time.monotonic_ns()
        self._bound = _BoundContext({"span_id": self.span_id})
        self._bound.__enter__()
        if self.run is not None:
            self.run.emit(self.event + ".start", level=self.level, **self.fields)
        return self

    def __exit__(self, exc_type, exc, tb):
        duration_ms = ((time.monotonic_ns() - self.started_ns) / 1000000.0
                       if self.started_ns is not None else 0.0)
        try:
            if self.run is not None:
                end_level = "error" if exc is not None else self.level
                self.run.emit(
                    self.event + ".end", level=end_level, duration_ms=duration_ms,
                    exception=exc, outcome="error" if exc is not None else "ok",
                    **self.fields)
        finally:
            if self._bound is not None:
                self._bound.__exit__(exc_type, exc, tb)
        return False

    async def __aenter__(self):
        return self.__enter__()

    async def __aexit__(self, exc_type, exc, tb):
        return self.__exit__(exc_type, exc, tb)


def configure(component, mode=None, log_dir=None, **fields):
    """Start and return the active diagnostics :class:`Run`.

    Reconfiguring cleanly closes the preceding run.  Optional tuning keys in
    ``fields`` are ``retention_days``, ``max_runs``, ``max_event_bytes``,
    ``max_run_bytes``, ``max_parts``, ``max_nodes``, and ``include_content``;
    every other field is sanitized run-start metadata.
    """
    global _active
    try:
        with _active_lock:
            if _active is not None:
                try:
                    _active.shutdown(outcome="reconfigured")
                except Exception:
                    pass
            _active = Run(component, mode=mode, log_dir=log_dir, **fields)
            return _active
    except Exception:
        # Never expose a partially initialized Run: span/context APIs would
        # call missing attributes and make diagnostics load-bearing. Ordinary
        # filesystem failures are already represented by a fully initialized,
        # disabled Run; constructor/tuning failures fall back to global no-ops.
        _active = None
        return _active


def get_run_id():
    try:
        return _active.run_id if _active is not None else None
    except Exception:
        return None


def get_log_path():
    try:
        return _active.path if _active is not None else None
    except Exception:
        return None


def get_context():
    """Return a copy of the currently bound correlation context.

    The copy prevents callers (notably events/recorder bridges) from mutating
    the context owned by surrounding work.  Before configuration it is an
    intentionally empty mapping.
    """
    if _active is None:
        return {}
    try:
        return dict(_context_fields.get() or {})
    except Exception:
        return {}


def context(**fields):
    """Temporarily bind correlation fields to nested sync or async work."""
    return _BoundContext(fields)


def span(event, /, level="info", **fields):
    """Log ``<event>.start``/``<event>.end`` with duration and exception."""
    return _Span(_active, str(event), level, fields)


def _emit(level, event_name, **fields):
    run = _active
    if run is None:
        return False
    try:
        return run.emit(event_name, level=level, **fields)
    except Exception:
        return False


def debug(event, /, **fields):
    return _emit("debug", event, **fields)


def info(event, /, **fields):
    return _emit("info", event, **fields)


def warning(event, /, **fields):
    return _emit("warning", event, **fields)


def error(event, /, **fields):
    return _emit("error", event, **fields)


def exception(event, /, exc=None, **fields):
    run = _active
    if run is None:
        return False
    try:
        if exc is None:
            exc = sys.exc_info()[1]
        return run.emit(event, level="error", exception=exc, **fields)
    except Exception:
        return False


def metric(name, value, /, unit=None, **fields):
    run = _active
    if run is None:
        return False
    try:
        return run.metric(name, value, unit=unit, **fields)
    except Exception:
        return False


def counter(name, /, amount=1, **fields):
    """Increment a named run counter and record its delta/current value."""
    run = _active
    if run is None:
        return False
    try:
        return run.counter(name, amount=amount, **fields)
    except Exception:
        return False


def install_asyncio(loop=None):
    run = _active
    if run is None:
        return False
    try:
        return run.install_asyncio(loop)
    except Exception:
        return False


def shutdown(outcome="ok", **fields):
    global _active
    with _active_lock:
        run = _active
        if run is None:
            return False
        try:
            return run.shutdown(outcome=outcome, **fields)
        except Exception:
            return False
        finally:
            if _active is run:
                _active = None


def _atexit_shutdown():
    try:
        shutdown(outcome="process_exit")
    except Exception:
        pass


atexit.register(_atexit_shutdown)
