"""Viewer server: GET /, /doc safety, /transcript feed, SSE reload latency,
no partial reads."""
import http.client
import json
import threading
import time

import pytest

from echoecho_app import config, events
from echoecho_app.services import artifacts
from echoecho_app.viewer.server import ViewerServer


@pytest.fixture
def server(tmp_path):
    srv = ViewerServer(tmp_path, port=0)  # ephemeral port
    srv.start()
    yield srv
    srv.stop()


def get(srv, path, timeout=2):
    host, port = srv.httpd.server_address[:2]
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


def read_sse_event(resp, deadline):
    """Read one SSE event (lines up to a blank line) before the deadline."""
    lines = []
    while time.monotonic() < deadline:
        line = resp.readline().decode("utf-8").rstrip("\n")
        if line == "" and lines:
            return "\n".join(lines)
        if line:
            lines.append(line)
    raise AssertionError("no SSE event before deadline; got %r" % lines)


def test_index_served(server):
    status, body = get(server, "/")
    assert status == 200
    assert b"marked" in body and b"EventSource" in body


def test_doc_returns_file_content(server, tmp_path):
    content = "# Plan\n\n## Goals\n- ship the demo\n"
    artifacts.write_atomic(tmp_path, "doc.md", content)
    status, body = get(server, "/doc?f=doc.md")
    assert status == 200
    assert body.decode("utf-8") == content


def test_doc_refuses_non_workspace_paths(server, tmp_path):
    (tmp_path / "doc.md").write_text("safe")
    for bad in ("../../etc/passwd", "..%2F..%2Fetc%2Fpasswd", ".tasks.jsonl",
                ".hidden.md", "absent.md", "", "sub%2F.hidden.md",
                "nested%2F..%2F..%2Fdoc.md", "%2Fetc%2Fpasswd"):
        status, _ = get(server, "/doc?f=" + bad)
        assert status == 404, bad


def test_doc_serves_nested_files_and_types(server, tmp_path):
    """v2: any visible workspace file, at any depth, typed for the browser —
    but never as text/html (workspace files must not run as pages)."""
    artifacts.write_atomic(tmp_path, "offsite/proposal.md", "# P\n")
    artifacts.write_atomic(tmp_path, "offsite/budget.csv", "item,eur\n")
    artifacts.write_atomic(tmp_path, "img/logo.png", b"\x89PNG\r\n\x1a\n\x00")
    artifacts.write_atomic(tmp_path, "page.html", "<script>x</script>")
    host, port = server.httpd.server_address[:2]

    def get_with_type(path):
        conn = http.client.HTTPConnection(host, port, timeout=2)
        try:
            conn.request("GET", path)
            resp = conn.getresponse()
            return resp.status, resp.getheader("Content-Type"), resp.read()
        finally:
            conn.close()

    status, ctype, body = get_with_type("/doc?f=offsite%2Fproposal.md")
    assert (status, body) == (200, b"# P\n")
    assert ctype.startswith("text/markdown")
    status, ctype, _ = get_with_type("/doc?f=offsite%2Fbudget.csv")
    assert status == 200 and ctype.startswith("text/plain")
    status, ctype, body = get_with_type("/doc?f=img%2Flogo.png")
    assert status == 200 and ctype == "image/png" and body.startswith(b"\x89PNG")
    # undecodable bytes fall back to a download, decodable html stays inert
    status, ctype, _ = get_with_type("/doc?f=page.html")
    assert status == 200 and ctype.startswith("text/plain")


def test_doc_responses_carry_script_free_csp(server, tmp_path):
    """SVG is a script-capable document type the viewer serves natively:
    the CSP must keep agent-written SVGs inert when opened directly."""
    artifacts.write_atomic(tmp_path, "evil.svg",
                           '<svg xmlns="http://www.w3.org/2000/svg">'
                           '<script>fetch("/pwn")</script></svg>')
    host, port = server.httpd.server_address[:2]
    conn = http.client.HTTPConnection(host, port, timeout=2)
    try:
        conn.request("GET", "/doc?f=evil.svg")
        resp = conn.getresponse()
        assert resp.status == 200
        assert resp.getheader("Content-Type") == "image/svg+xml"
        csp = resp.getheader("Content-Security-Policy")
        assert csp and "default-src 'none'" in csp
        assert resp.getheader("X-Content-Type-Options") == "nosniff"
        resp.read()
    finally:
        conn.close()


def test_doc_refuses_workspace_escaping_symlink(server, tmp_path):
    outside = tmp_path.parent / ("secret-%s.txt" % tmp_path.name)
    outside.write_text("secret")
    (tmp_path / "innocent.md").symlink_to(outside)
    status, _ = get(server, "/doc?f=innocent.md")
    assert status == 404


def test_index_sanitizes_markdown_before_innerhtml(server):
    status, body = get(server, "/")
    assert status == 200
    html = body.decode("utf-8")
    assert "DOMPurify.sanitize" in html  # agent markdown never hits innerHTML raw
    assert "purify.min.js" in html


def test_sse_reload_within_500ms_of_touch(server, tmp_path):
    artifacts.write_atomic(tmp_path, "doc.md", "v1")
    host, port = server.httpd.server_address[:2]
    conn = http.client.HTTPConnection(host, port, timeout=2)
    conn.request("GET", "/events")
    resp = conn.getresponse()
    assert resp.status == 200
    assert resp.getheader("Content-Type") == "text/event-stream"
    try:
        # initial snapshot event on connect
        first = read_sse_event(resp, time.monotonic() + 1.0)
        assert "event: reload" in first and "doc.md" in first
        # touch the file -> reload event within 500ms
        artifacts.write_atomic(tmp_path, "doc.md", "v2 changed")
        t0 = time.monotonic()
        ev = read_sse_event(resp, t0 + 0.5)
        assert time.monotonic() - t0 < 0.5
        assert "event: reload" in ev and "doc.md" in ev
    finally:
        conn.close()


def test_transcript_empty_without_feed(server):
    status, body = get(server, "/transcript")
    assert status == 200
    assert json.loads(body) == []


def test_transcript_returns_emitted_events(server, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WORKSPACE_DIR", tmp_path)  # emit -> server ws
    events.emit("user_text", text="hello echoecho")
    events.emit("task", task_id="t1", kind="sleep.echoecho", status="done",
                say="done!", priority="interrupt")
    # malformed lines must be skipped, not break the endpoint
    with open(tmp_path / events.FEED_NAME, "a") as f:
        f.write("{not json}\n")
    events.emit("injection", text="[task t1 done] done!", priority="interrupt")
    status, body = get(server, "/transcript")
    assert status == 200
    got = json.loads(body)
    assert [e["type"] for e in got] == ["user_text", "task", "injection"]
    assert got[0]["text"] == "hello echoecho"
    assert got[1]["say"] == "done!"


def test_transcript_caps_at_last_400_events(server, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WORKSPACE_DIR", tmp_path)
    with open(tmp_path / events.FEED_NAME, "w") as f:
        for i in range(450):
            f.write(json.dumps({"ts": float(i), "type": "user_text",
                                "text": "line %d" % i}) + "\n")
    _, body = get(server, "/transcript")
    got = json.loads(body)
    assert len(got) == 400
    assert got[0]["text"] == "line 50" and got[-1]["text"] == "line 449"


def test_sse_fires_within_500ms_of_an_emit(server, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WORKSPACE_DIR", tmp_path)
    host, port = server.httpd.server_address[:2]
    conn = http.client.HTTPConnection(host, port, timeout=2)
    conn.request("GET", "/events")
    resp = conn.getresponse()
    try:
        read_sse_event(resp, time.monotonic() + 1.0)  # initial snapshot
        events.emit("user_text", text="wake the poll")
        t0 = time.monotonic()
        ev = read_sse_event(resp, t0 + 0.5)
        assert time.monotonic() - t0 < 0.5
        assert "event: reload" in ev
    finally:
        conn.close()


def test_reload_files_list_is_visible_files_only(server, tmp_path, monkeypatch):
    """The files list now holds EVERY visible workspace file (any type, any
    depth) — but the events feed and dotfiles must never leak into it."""
    monkeypatch.setattr(config, "WORKSPACE_DIR", tmp_path)
    artifacts.write_atomic(tmp_path, "doc.md", "# hi")
    artifacts.write_atomic(tmp_path, "offsite/budget.csv", "item,eur\n")
    events.emit("user_text", text="not a doc")
    host, port = server.httpd.server_address[:2]
    conn = http.client.HTTPConnection(host, port, timeout=2)
    conn.request("GET", "/events")
    resp = conn.getresponse()
    try:
        ev = read_sse_event(resp, time.monotonic() + 1.0)
        data = json.loads(ev.split("data: ", 1)[1])
        assert [f["name"] for f in data["files"]] == ["doc.md",
                                                      "offsite/budget.csv"]
    finally:
        conn.close()


def test_sse_fires_on_subdir_and_non_md_changes(server, tmp_path):
    artifacts.write_atomic(tmp_path, "doc.md", "v1")
    host, port = server.httpd.server_address[:2]
    conn = http.client.HTTPConnection(host, port, timeout=2)
    conn.request("GET", "/events")
    resp = conn.getresponse()
    try:
        read_sse_event(resp, time.monotonic() + 1.0)  # initial snapshot
        artifacts.write_atomic(tmp_path, "offsite/budget.csv", "item,eur\n")
        t0 = time.monotonic()
        ev = read_sse_event(resp, t0 + 0.5)
        assert time.monotonic() - t0 < 0.5
        assert "offsite/budget.csv" in ev
    finally:
        conn.close()


def test_doc_never_serves_partial_content(server, tmp_path):
    """Hammer /doc while a writer flips the file between two big payloads:
    every response must be exactly one of them (atomic tmp+rename)."""
    a = ("A" * 80 + "\n") * 60
    b = ("B" * 80 + "\n") * 60
    artifacts.write_atomic(tmp_path, "doc.md", a)
    stop = threading.Event()

    def writer():
        flip = False
        while not stop.is_set():
            artifacts.write_atomic(tmp_path, "doc.md", a if flip else b)
            flip = not flip

    t = threading.Thread(target=writer, daemon=True)
    t.start()
    try:
        for _ in range(25):
            status, body = get(server, "/doc?f=doc.md")
            assert status == 200
            assert body.decode("utf-8") in (a, b), "partial file served"
    finally:
        stop.set()
        t.join(timeout=2)


def test_proto_index_lists_repo_mockups(server):
    """/proto is the home for repo-shipped design prototypes: it must list
    the live-writer mockup that ships in mockups/."""
    status, body = get(server, "/proto")
    assert status == 200
    assert b"live-writer-demo" in body


def test_proto_serves_mockup_as_html(server):
    """Repo mockups (unlike agent-written /doc files) run as real pages."""
    host, port = server.httpd.server_address[:2]
    conn = http.client.HTTPConnection(host, port, timeout=2)
    try:
        conn.request("GET", "/proto/live-writer-demo")
        resp = conn.getresponse()
        body = resp.read()
        assert resp.status == 200
        assert resp.getheader("Content-Type", "").startswith("text/html")
        assert b"Live Writer" in body
    finally:
        conn.close()


def test_proto_refuses_traversal_and_unknown_names(server):
    for path in ("/proto/../echoecho_app/viewer/server.py",
                 "/proto/%2e%2e%2fserver",
                 "/proto/no-such-mockup"):
        status, _ = get(server, path)
        assert status == 404
