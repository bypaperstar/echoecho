"""Viewer server: GET /, /doc safety, /transcript feed, SSE reload latency,
no partial reads."""
import http.client
import json
import threading
import time

import pytest

from echo_app import config, events
from echo_app.services import artifacts
from echo_app.viewer.server import ViewerServer


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
                ".hidden.md", "absent.md", "", "doc.txt"):
        status, _ = get(server, "/doc?f=" + bad)
        assert status == 404, bad
    # basename'd traversal that lands on a real file is still fine to serve
    status, body = get(server, "/doc?f=nested%2Fdoc.md")
    assert (status, body) == (200, b"safe")


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
    events.emit("user_text", text="hello echo")
    events.emit("task", task_id="t1", kind="sleep.echo", status="done",
                say="done!", priority="interrupt")
    # malformed lines must be skipped, not break the endpoint
    with open(tmp_path / events.FEED_NAME, "a") as f:
        f.write("{not json}\n")
    events.emit("injection", text="[task t1 done] done!", priority="interrupt")
    status, body = get(server, "/transcript")
    assert status == 200
    got = json.loads(body)
    assert [e["type"] for e in got] == ["user_text", "task", "injection"]
    assert got[0]["text"] == "hello echo"
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


def test_reload_files_list_stays_md_only(server, tmp_path, monkeypatch):
    """The events feed drives SSE but must never leak into 'files' — the
    tabs JS (and this contract) depend on it holding only *.md docs."""
    monkeypatch.setattr(config, "WORKSPACE_DIR", tmp_path)
    artifacts.write_atomic(tmp_path, "doc.md", "# hi")
    events.emit("user_text", text="not a doc")
    host, port = server.httpd.server_address[:2]
    conn = http.client.HTTPConnection(host, port, timeout=2)
    conn.request("GET", "/events")
    resp = conn.getresponse()
    try:
        ev = read_sse_event(resp, time.monotonic() + 1.0)
        data = json.loads(ev.split("data: ", 1)[1])
        assert [f["name"] for f in data["files"]] == ["doc.md"]
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
