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
GET /transcript  -> JSON array: last 400 events from workspace/.events.jsonl
GET /events      -> SSE stream: a 'reload' event (JSON file list) on connect
                    and whenever a 250ms poll sees any visible file change
                    OR an append to the .events.jsonl feed

Workspace writes are atomic (tmp + os.rename), so /doc never serves a
half-written file.
"""
import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from echo_app.services import artifacts

POLL_INTERVAL = 0.25
INDEX = Path(__file__).with_name("index.html")
EVENTS_FEED = ".events.jsonl"  # echo_app.events feed inside the workspace
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


class _Handler(BaseHTTPRequestHandler):
    workspace = None   # set on the subclass by ViewerServer
    stopping = None    # threading.Event

    def log_message(self, fmt, *args):  # keep the demo console quiet
        pass

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/":
            self._respond(200, "text/html; charset=utf-8", INDEX.read_bytes())
        elif parsed.path == "/doc":
            self._doc(parsed)
        elif parsed.path == "/transcript":
            body = json.dumps(read_transcript(self.workspace)).encode("utf-8")
            self._respond(200, "application/json; charset=utf-8", body)
        elif parsed.path == "/events":
            self._events()
        else:
            self._respond(404, "text/plain", b"not found")

    def _respond(self, status, ctype, body, csp=None):
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
        body = path.read_bytes()
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
        self.send_response(200)
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
                self.stopping.wait(POLL_INTERVAL)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return  # client went away


class ViewerServer:
    """Threaded viewer; start() returns immediately, stop() tears it down."""

    def __init__(self, workspace, host="127.0.0.1", port=8765):
        self._stopping = threading.Event()
        handler = type("Handler", (_Handler,), {
            "workspace": Path(workspace), "stopping": self._stopping})
        self.httpd = ThreadingHTTPServer((host, port), handler)
        self.httpd.daemon_threads = True
        self._thread = None

    @property
    def url(self):
        host, port = self.httpd.server_address[:2]
        return "http://%s:%d/" % (host, port)

    def start(self):
        self._thread = threading.Thread(
            target=self.httpd.serve_forever, kwargs={"poll_interval": 0.1},
            daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stopping.set()
        self.httpd.shutdown()
        self.httpd.server_close()
        if self._thread:
            self._thread.join(timeout=2)
