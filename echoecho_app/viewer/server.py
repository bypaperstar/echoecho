"""Live workspace viewer: stdlib HTTP + SSE, no dependencies.

GET /            -> index.html (file tree + type-aware pane + live transcript)
GET /doc?f=path  -> any visible workspace file (relative path, subdirs ok);
                    resolution goes through artifacts.resolve, so traversal,
                    dotfiles, and workspace-escaping symlinks 404.
                    Markdown/images/PDF get their real content type; other
                    text is served as text/plain (never text/html — the
                    viewer must not execute workspace files); undecodable
                    bytes fall back to application/octet-stream. Every /doc
                    response carries a script-free CSP so script-capable
                    types (SVG!) stay inert opened as documents.
GET /proto       -> tiny HTML index of the repo's design prototypes (mockups/)
GET /proto/name  -> mockups/<name>.html served as a real page. These are
                    repo-shipped, reviewed files (NOT agent-written workspace
                    output), which is why they may run as text/html while
                    /doc never does. Names are a single [A-Za-z0-9._-] token
                    resolved strictly inside mockups/, so traversal 404s.
GET /version     -> {"version", "sha", "updatedAt"}: version from the
                    repo-root VERSION file, short sha + last-commit date from
                    git (null when git is unavailable, e.g. a bare export)
GET /transcript  -> JSON array: last 400 events from workspace/.events.jsonl
GET /events      -> SSE stream: a 'reload' event (JSON file list) on connect
                    and whenever a 250ms poll sees any visible file change
                    OR an append to the .events.jsonl feed
GET /vnc-info    -> {"url": "vnc://[:pass@]host:port"} for echoecho's Mac, or 503
                    {"error": ...}; source: ECHOECHO_VNC_URL env override, else
                    lume (vm tier, or lume on PATH — the tier may be chosen
                    per-task), read fresh on every call. The ONLY route that
                    serves credentials, so it alone requires
                    "Authorization: Bearer <token>" (else 403); the token is
                    regenerated per run and written 0o600 to
                    ECHOECHO_VIEWER_TOKEN_FILE (default ~/.echoecho/viewer.token),
                    where the Electron portal reads it fresh per call

Workspace writes are atomic (tmp + os.rename), so /doc never serves a
half-written file.
"""
import html as html_mod
import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import echoecho_app
from echoecho_app import config, diagnostics
from echoecho_app.services import artifacts, vm

POLL_INTERVAL = 0.25
INDEX = Path(__file__).with_name("index.html")
REPO_ROOT = Path(__file__).resolve().parents[2]
# Repo-shipped design prototypes (e.g. the live-writer vision mockup). Repo
# files are trusted the same way index.html is; agent-written files are not.
MOCKUPS = Path(__file__).resolve().parents[2] / "mockups"
EVENTS_FEED = ".events.jsonl"  # echoecho_app.events feed inside the workspace
TRANSCRIPT_LIMIT = 400

# Types the browser may render natively; everything else is text/plain or a
# download. Deliberately no text/html: workspace files never run as pages.
NATIVE_TYPES = {
    ".md": "text/markdown; charset=utf-8",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml",
    ".pdf": "application/pdf",
}


def workspace_state(workspace):
    """{relative posix path: (mtime, size, inode)} for every visible
    workspace file, recursively (dotted files/dirs excluded).

    Size + inode matter: coarse filesystem mtime granularity can hide two
    atomic writes in the same tick, but tmp+rename always swaps the inode.
    """
    ws = Path(workspace)
    state = {}
    for name in artifacts.list_files(ws):
        key = artifacts.stat_key(ws, name)  # None: deleted mid-poll, or a
        if key is not None:                 # symlink escaping the workspace
            state[name] = key
    return state


def events_feed_state(workspace):
    """(mtime, size, inode) of the .events.jsonl feed, or None if missing —
    folded into the SSE poll snapshot so transcript appends also fire it."""
    try:
        st = (Path(workspace) / EVENTS_FEED).stat()
        return (st.st_mtime, st.st_size, st.st_ino)
    except OSError:
        return None


def read_transcript(workspace, limit=TRANSCRIPT_LIMIT):
    """Last `limit` events from the feed as dicts; malformed lines skipped,
    [] when the feed doesn't exist yet."""
    path = Path(workspace) / EVENTS_FEED
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if isinstance(ev, dict):
            out.append(ev)
    return out


_VERSION_INFO = None


def version_info():
    """What echoecho is running: VERSION file + git's short sha and
    last-commit date. Computed once per process — the daemon restarts on
    update, so it can never go stale within a run."""
    global _VERSION_INFO
    if _VERSION_INFO is None:
        info = {"version": echoecho_app.__version__,
                "sha": None, "updatedAt": None}
        try:
            out = subprocess.run(
                ["git", "log", "-1", "--format=%h %cI"],
                cwd=str(REPO_ROOT), stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, timeout=5, check=True,
            ).stdout.decode("utf-8").split()
            info["sha"], info["updatedAt"] = out[0], out[1]
        except Exception:  # no git / not a checkout: version alone is fine
            pass
        _VERSION_INFO = info
    return _VERSION_INFO


def token_path():
    """Where the per-run viewer token lives — the Electron portal reads the
    same path, so the two sides must agree byte-for-byte."""
    raw = os.environ.get("ECHOECHO_VIEWER_TOKEN_FILE", "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".echoecho" / "viewer.token"


def _write_token(token):
    """Persist the token 0o600 (parent dir 0o700 if we create it): it gates
    the one route that serves credentials, so only this user may read it."""
    path = token_path()
    if not path.parent.is_dir():
        path.parent.mkdir(parents=True)
        os.chmod(str(path.parent), 0o700)
    # O_CREAT's mode is umask-filtered and skipped for an existing file, so
    # the explicit chmod below is what actually guarantees 0o600
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(token)
    os.chmod(str(path), 0o600)
    return path


class _Handler(BaseHTTPRequestHandler):
    workspace = None   # set on the subclass by ViewerServer
    stopping = None    # threading.Event
    token = None       # per-run bearer token gating /vnc-info

    def log_message(self, fmt, *args):  # keep the demo console quiet
        pass

    def do_GET(self):
        started = time.monotonic()
        parsed = urllib.parse.urlparse(self.path)
        known_routes = {
            "/", "/healthz", "/doc", "/version", "/transcript",
            "/events", "/vnc-info", "/proto", "/proto/",
        }
        route = ("/proto/*" if parsed.path.startswith("/proto/") else
                 parsed.path if parsed.path in known_routes else "/unknown")
        self._diag_status = None
        self._diag_bytes = 0
        try:
            if parsed.path == "/":
                self._respond(200, "text/html; charset=utf-8",
                              INDEX.read_bytes())
            elif parsed.path == "/healthz":
                self._json(200, {"ok": True,
                                 "run_id": diagnostics.get_run_id()})
            elif parsed.path == "/doc":
                self._doc(parsed)
            elif parsed.path == "/version":
                self._json(200, version_info())
            elif parsed.path == "/transcript":
                body = json.dumps(read_transcript(self.workspace)).encode("utf-8")
                self._respond(200, "application/json; charset=utf-8", body)
            elif parsed.path == "/events":
                self._events()
            elif parsed.path == "/vnc-info":
                self._vnc_info()
            elif parsed.path == "/proto" or parsed.path == "/proto/":
                self._proto_index()
            elif parsed.path.startswith("/proto/"):
                self._proto(parsed.path[len("/proto/"):])
            else:
                self._respond(404, "text/plain", b"not found")
        except (BrokenPipeError, ConnectionResetError):
            count, sampled = self._diag_sample("disconnected:%s" % route)
            if sampled:
                diagnostics.info("viewer.request.disconnected", route=route,
                                 occurrences=count)
        except Exception as exc:
            diagnostics.exception("viewer.request.failed", exc=exc,
                                  route=route,
                                  status=self._diag_status)
            if self._diag_status is None:
                try:
                    self._respond(500, "text/plain", b"internal error")
                except Exception:
                    pass
        finally:
            duration_ms = (time.monotonic() - started) * 1000
            should_sample = False
            sample_count = None
            if self._diag_status is not None and self._diag_status >= 400:
                sample_count, should_sample = self._diag_sample(
                    "request:%s:%s" % (route, self._diag_status))
            elif parsed.path == "/events":
                sample_count, should_sample = self._diag_sample(
                    "request:/events:finished")
            if (should_sample or
                    (duration_ms >= 250 and parsed.path != "/events")):
                diagnostics.info(
                    "viewer.request.finished", route=route,
                    status=self._diag_status, response_bytes=self._diag_bytes,
                    duration_ms=round(duration_ms, 1),
                    sse_updates=getattr(self, "_diag_sse_updates", None),
                    occurrences=sample_count)

    def _diag_sample(self, key):
        """Power-of-two sampling for client-amplifiable request failures."""
        with self.diag_lock:
            count = self.diag_counts.get(key, 0) + 1
            self.diag_counts[key] = count
        return count, count <= 3 or count & (count - 1) == 0

    def _diag_vnc_state(self, state, source):
        """Track polling health and report only transitions plus stop totals."""
        with self.diag_lock:
            self.diag_counts["vnc_requests"] = \
                self.diag_counts.get("vnc_requests", 0) + 1
            current = (state, source)
            previous = self.diag_state.get("vnc")
            self.diag_state["vnc"] = current
            return previous != current, previous

    def _proto_index(self):
        """List every mockups/*.html by stem — a home for design prototypes
        that outlive the branch they were sketched on."""
        names = sorted(p.stem for p in MOCKUPS.glob("*.html")) \
            if MOCKUPS.is_dir() else []
        items = "".join(
            '<li><a href="/proto/%s">%s</a></li>'
            % (urllib.parse.quote(n), html_mod.escape(n)) for n in names) \
            or "<li>none yet — add an .html file under mockups/</li>"
        body = ("<!doctype html><meta charset='utf-8'>"
                "<title>echoecho — prototypes</title>"
                "<body style='font:15px/1.6 -apple-system,sans-serif;"
                "background:#0e1014;color:#e9ecf4;padding:40px'>"
                "<h2>Design prototypes</h2><p style='color:#8b93a7'>"
                "Repo-shipped mockups from <code>mockups/</code> — "
                "simulations for aligning on look &amp; feel, not the real "
                "pipeline.</p><ul>%s</ul>" % items).encode("utf-8")
        self._respond(200, "text/html; charset=utf-8", body)

    def _proto(self, raw):
        """Serve mockups/<name>.html. One strict path token, resolved and
        parent-checked inside mockups/ — repo files only, no traversal."""
        name = urllib.parse.unquote(raw)
        if not re.fullmatch(r"[A-Za-z0-9._-]+", name) or ".." in name:
            self._respond(404, "text/plain", b"no such prototype")
            return
        path = (MOCKUPS / (name + ".html")).resolve()
        if path.parent != MOCKUPS.resolve() or not path.is_file():
            self._respond(404, "text/plain", b"no such prototype")
            return
        self._respond(200, "text/html; charset=utf-8", path.read_bytes())

    def _vnc_info(self):
        """Where echoecho's Mac's VNC lives: ECHOECHO_VNC_URL override (tests/CI, or
        "I already know my VM") -> else ask lume when the vm tier is
        configured OR lume is on PATH (the tier may be chosen per-task) ->
        else 503. Never cached: a re-cloned VM changes address, and a stale
        URL would strand the portal on a dead endpoint. Token-gated before
        any work: this is the only route that serves credentials."""
        auth = self.headers.get("Authorization") or ""
        if not secrets.compare_digest(
                auth.encode("utf-8", "replace"),
                ("Bearer %s" % self.token).encode("utf-8")):
            count, sampled = self._diag_sample("vnc_denied")
            if sampled:
                diagnostics.warning("viewer.vnc_info.denied", occurrences=count)
            self._json(403, {"error": "missing or bad viewer token"})
            return
        override = os.environ.get("ECHOECHO_VNC_URL", "").strip()
        if override:
            changed, previous = self._diag_vnc_state("resolved", "override")
            if changed:
                diagnostics.info("viewer.vnc_info.resolved", source="override",
                                 previous_state=previous[0] if previous else None)
            self._json(200, {"url": override})
            return
        if config.sandbox_tier() != "vm" and shutil.which("lume") is None:
            changed, previous = self._diag_vnc_state(
                "unavailable", "configuration")
            if changed:
                diagnostics.info("viewer.vnc_info.unavailable",
                                 reason="vm_not_configured",
                                 previous_state=previous[0] if previous else None)
            self._json(503, {"error": (
                "no VM configured: set ECHOECHO_SANDBOX=vm, or point "
                "ECHOECHO_VNC_URL at any VNC server")})
            return
        try:
            url = vm.vnc_url()
        except Exception as exc:  # lume missing/failed: human-readable 503
            changed, previous = self._diag_vnc_state("failed", "lume")
            if changed:
                diagnostics.exception("viewer.vnc_info.failed", exc=exc,
                                      source="lume",
                                      previous_state=previous[0] if previous else None)
            self._json(503, {"error": str(exc) or type(exc).__name__})
            return
        changed, previous = self._diag_vnc_state("resolved", "lume")
        if changed:
            diagnostics.info("viewer.vnc_info.resolved", source="lume",
                             previous_state=previous[0] if previous else None)
        self._json(200, {"url": url})

    def _json(self, status, obj):
        self._respond(status, "application/json; charset=utf-8",
                      json.dumps(obj).encode("utf-8"))

    def _respond(self, status, ctype, body, csp=None):
        self._diag_status = status
        self._diag_bytes = len(body)
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # workspace files are agent-written: text/plain must stay text/plain
        self.send_header("X-Content-Type-Options", "nosniff")
        if csp:
            self.send_header("Content-Security-Policy", csp)
        self.end_headers()
        self.wfile.write(body)

    def _doc(self, parsed):
        qs = urllib.parse.parse_qs(parsed.query)
        raw = (qs.get("f") or [""])[0]
        try:
            path = artifacts.resolve(self.workspace, raw)  # no traversal, ever
        except ValueError:
            self._respond(404, "text/plain", b"not a workspace file")
            return
        if not path.is_file():
            self._respond(404, "text/plain", b"no such file")
            return
        try:
            body = path.read_bytes()
        except OSError as exc:
            diagnostics.exception("viewer.document.read_failed", exc=exc,
                                  **artifacts.diagnostic_suffix_fields(path))
            self._respond(500, "text/plain", b"could not read file")
            return
        ctype = NATIVE_TYPES.get(path.suffix.lower())
        if ctype is None:
            try:
                body.decode("utf-8")
                ctype = "text/plain; charset=utf-8"
            except UnicodeDecodeError:
                ctype = "application/octet-stream"
        # script-free CSP: an agent-written SVG (image/svg+xml is a
        # script-capable document type) opened directly must stay inert
        self._respond(200, ctype, body,
                      csp="default-src 'none'; img-src 'self' data:; "
                          "style-src 'unsafe-inline'; object-src 'self'")

    def _events(self):
        self._diag_sse_updates = 0
        count, sampled = self._diag_sample("sse_connected")
        if sampled:
            diagnostics.info("viewer.sse.connected", occurrences=count)
        self.send_response(200)
        self._diag_status = 200
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        last = None
        try:
            while not self.stopping.is_set():
                docs = workspace_state(self.workspace)
                state = dict(docs)
                # non-file sentinel: a transcript append must also fire SSE,
                # but never leak into the "files" list the tree JS renders
                state["__events__"] = events_feed_state(self.workspace)
                if state != last:
                    last = state
                    data = json.dumps({"files": [
                        {"name": n, "mtime": v[0]} for n, v in docs.items()]})
                    self.wfile.write(
                        ("event: reload\ndata: %s\n\n" % data).encode("utf-8"))
                    self.wfile.flush()
                    self._diag_sse_updates += 1
                self.stopping.wait(POLL_INTERVAL)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return  # client went away
        finally:
            with self.diag_lock:
                self.diag_counts["sse_updates"] = \
                    self.diag_counts.get("sse_updates", 0) + \
                    self._diag_sse_updates
            count, sampled = self._diag_sample("sse_disconnected")
            if sampled:
                diagnostics.info("viewer.sse.disconnected",
                                 updates=self._diag_sse_updates,
                                 occurrences=count)


class ViewerServer:
    """Threaded viewer; start() returns immediately, stop() tears it down."""

    def __init__(self, workspace, host="127.0.0.1", port=8765):
        self._stopping = threading.Event()
        # per-run /vnc-info token, generated once per server (never per
        # request) and written where the portal expects to read it
        self.token = secrets.token_hex(16)
        _write_token(self.token)
        self._diag_counts = {}
        self._diag_state = {}
        self._diag_lock = threading.Lock()
        handler = type("Handler", (_Handler,), {
            "workspace": Path(workspace), "stopping": self._stopping,
            "token": self.token, "diag_counts": self._diag_counts,
            "diag_state": self._diag_state, "diag_lock": self._diag_lock})
        self.httpd = ThreadingHTTPServer((host, port), handler)
        self.httpd.daemon_threads = True
        self._thread = None
        self._host_scope = (
            "loopback" if host in ("127.0.0.1", "localhost", "::1")
            else "non_loopback")
        if self._host_scope == "non_loopback":
            diagnostics.warning(
                "viewer.non_loopback_bind", host_scope=self._host_scope)

    @property
    def url(self):
        host, port = self.httpd.server_address[:2]
        return "http://%s:%d/" % (host, port)

    def start(self):
        self._thread = threading.Thread(
            target=self.httpd.serve_forever, kwargs={"poll_interval": 0.1},
            daemon=True)
        self._thread.start()
        diagnostics.info("viewer.server.started",
                         host_scope=self._host_scope,
                         port=self.httpd.server_address[1])
        return self

    def stop(self):
        self._stopping.set()
        self.httpd.shutdown()
        self.httpd.server_close()
        if self._thread:
            self._thread.join(timeout=2)
        with self._diag_lock:
            request_counts = dict(self._diag_counts)
        diagnostics.info("viewer.server.stopped",
                         thread_alive=bool(self._thread and self._thread.is_alive()),
                         request_counts=request_counts)
