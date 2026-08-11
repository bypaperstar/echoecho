"""Live workspace viewer: stdlib HTTP + SSE, no dependencies.

GET /            -> index.html (marked.js tabs page)
GET /doc?f=name  -> raw markdown; basename'd, *.md only, inside workspace only
GET /events      -> SSE stream: a 'reload' event (JSON file list) on connect
                    and whenever a 250ms mtime poll sees a change

Workspace writes are atomic (tmp + os.rename), so /doc never serves a
half-written file.
"""
import json
import os
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

POLL_INTERVAL = 0.25
INDEX = Path(__file__).with_name("index.html")


def workspace_state(workspace):
    """{name: (mtime, size, inode)} for visible workspace *.md files.

    Size + inode matter: coarse filesystem mtime granularity can hide two
    atomic writes in the same tick, but tmp+rename always swaps the inode.
    """
    ws = Path(workspace)
    state = {}
    if ws.is_dir():
        for p in sorted(ws.iterdir()):
            if p.is_file() and p.suffix == ".md" and not p.name.startswith("."):
                st = p.stat()
                state[p.name] = (st.st_mtime, st.st_size, st.st_ino)
    return state


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
        elif parsed.path == "/events":
            self._events()
        else:
            self._respond(404, "text/plain", b"not found")

    def _respond(self, status, ctype, body):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _doc(self, parsed):
        qs = urllib.parse.parse_qs(parsed.query)
        raw = (qs.get("f") or [""])[0]
        name = os.path.basename(raw)  # no traversal, ever
        if not name.endswith(".md") or name.startswith("."):
            self._respond(404, "text/plain", b"not a workspace markdown file")
            return
        path = Path(self.workspace) / name
        if not path.is_file():
            self._respond(404, "text/plain", b"no such file")
            return
        self._respond(200, "text/markdown; charset=utf-8",
                      path.read_bytes())

    def _events(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        last = None
        try:
            while not self.stopping.is_set():
                state = workspace_state(self.workspace)
                if state != last:
                    last = state
                    data = json.dumps({"files": [
                        {"name": n, "mtime": v[0]} for n, v in state.items()]})
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
