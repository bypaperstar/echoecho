import importlib.util
import json
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "diagnostics.py"
SPEC = importlib.util.spec_from_file_location("echoecho_diagnostics_inspector", SCRIPT)
inspector = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(inspector)


def _line(path, value, append=False):
    mode = "a" if append else "w"
    with path.open(mode, encoding="utf-8") as stream:
        stream.write(json.dumps(value) + "\n")


def test_cli_count_options_have_safe_upper_bounds(tmp_path):
    args = inspector.parse_args([
        "--dir", str(tmp_path), "--latest", str(10 ** 30),
        "--tail", str(10 ** 30),
    ])
    assert args.latest == inspector.MAX_LATEST
    assert args.tail == inspector.MAX_TAIL


def test_mixed_runtime_runs_are_normalized_and_partial_lines_survive(tmp_path):
    _line(tmp_path / "latest-electron.json", {
        "run_id": "electron-run",
        "started_at": "2026-08-18T10:00:00Z",
        "last_event_at": "2026-08-18T10:00:02Z",
        "state": "running",
        "files": ["run-electron.jsonl"],
    })
    electron = tmp_path / "run-electron.jsonl"
    _line(electron, {
        "time": "2026-08-18T10:00:01Z",
        "run_id": "electron-run",
        "seq": 1,
        "level": "warn",
        "event": "viewer.retry",
        "surface": "viewer-client",
        "fields": {
            "duration_ms": 1500,
            "parent_run_id": "orb-parent",
            "session_id": "session-1",
            "error": {
                "name": "TimeoutError",
                "message_fingerprint": "abc123",
                "message_length": 20,
            },
        },
    })
    with electron.open("a", encoding="utf-8") as stream:
        stream.write('{"partial":')

    _line(tmp_path / "python.jsonl", {
        "schema_version": 1,
        "ts": "2026-08-18T11:00:00Z",
        "run_id": "python-run",
        "seq": 1,
        "level": "error",
        "event": "task.runner.crashed",
        "component": "daemon",
        "context": {"task_id": "task-1", "span_id": "span-1"},
        "duration_ms": 2300,
        "exception": {
            "type": "ValueError",
            "message": "authorization:Bearer secret-value-here",
            "fingerprint": "python-fingerprint",
        },
    })

    args = inspector.parse_args([
        "--dir", str(tmp_path), "--latest", "5", "--tail", "5",
        "--level", "warn", "--slow-ms", "1000",
    ])
    root, runs, discovery, _filters = inspector.discover(args)
    data = {run.run_id: run.as_dict(root) for run in runs}

    assert set(data) == {"electron-run", "python-run"}
    assert data["electron-run"]["components"] == {"viewer-client": 1}
    assert data["electron-run"]["correlation_ids"]["parent_run_id"] == ["orb-parent"]
    assert data["electron-run"]["correlation_ids"]["session_id"] == ["session-1"]
    assert data["electron-run"]["recent"][0]["exception"]["fingerprint"] == "abc123"
    assert data["python-run"]["correlation_ids"]["task_id"] == ["task-1"]
    assert data["python-run"]["slow_spans"][0]["duration_ms"] == 2300
    assert data["python-run"]["recent"][0]["exception"]["fingerprint"] == \
        "python-fingerprint"
    assert "secret-value-here" not in json.dumps(inspector.redact(data))
    assert discovery["malformed_lines"] == 1
    assert discovery["metadata_files"] == 1


def test_component_filter_uses_python_component_and_electron_surface(tmp_path):
    records = [
        {
            "time": "2026-08-18T10:00:00Z", "run_id": "mixed", "seq": 1,
            "level": "warn", "event": "viewer.disconnected",
            "surface": "viewer-client", "fields": {},
        },
        {
            "ts": "2026-08-18T10:00:01Z", "run_id": "mixed", "seq": 2,
            "level": "warning", "event": "realtime.transport.lost",
            "component": "daemon", "fields": {},
        },
    ]
    path = tmp_path / "mixed.jsonl"
    for index, record in enumerate(records):
        _line(path, record, append=index > 0)

    args = inspector.parse_args([
        "--dir", str(tmp_path), "--component", "viewer", "--latest", "5",
    ])
    root, runs, _discovery, filters = inspector.discover(args)

    assert filters == ["viewer"]
    assert len(runs) == 1
    assert runs[0].as_dict(root)["top_events"] == {"viewer.disconnected": 1}


def test_defense_in_depth_redaction_and_nonfinite_values():
    value = inspector.redact({
        "api-key": "not-safe",
        "accessToken": "also-not-safe",
        "transcript": "private words",
        "requestBody": "private request",
        "message": "password=hunter2 and github_pat_abcdefghijklmnop",
        "measurement": float("inf"),
        "audio_bytes": 2048,
        "stdout_chars": 900,
        "output_tokens": 12,
        "token_count": 4,
        "transcript_s": "private-not-a-number",
        "audio_bytes_fake": "private-audio",
        "stdout_chars_fake": "private-output",
    })

    encoded = json.dumps(value, allow_nan=False)
    assert "not-safe" not in encoded
    assert "also-not-safe" not in encoded
    assert "private words" not in encoded
    assert "private request" not in encoded
    assert "hunter2" not in encoded
    assert "github_pat_" not in encoded
    assert value["measurement"] == "inf"
    assert value["audio_bytes"] == 2048
    assert value["stdout_chars"] == 900
    assert value["output_tokens"] == 12
    assert value["token_count"] == 4
    assert value["transcript_s"] == "<redacted>"
    assert value["audio_bytes_fake"] == "<redacted>"
    assert value["stdout_chars_fake"] == "<redacted>"


def test_electron_rotation_parts_feed_recent_tail_in_sequence_order(tmp_path):
    base = "orb-run-2026-08-18T10-00-00-000Z-1234-abcdef12"
    for part, seq, event in (
            (None, 1, "rotation.base"),
            (1, 2, "rotation.part_one"),
            (2, 3, "rotation.part_two")):
        suffix = "" if part is None else ".%d" % part
        _line(tmp_path / (base + suffix + ".jsonl"), {
            "time": "2026-08-18T10:00:0%dZ" % seq,
            "run_id": "electron-rotated-run",
            "seq": seq,
            "level": "warn",
            "event": event,
            "surface": "electron-main",
            "fields": {},
        })

    args = inspector.parse_args([
        "--dir", str(tmp_path), "--latest", "1", "--tail", "2",
        "--level", "warn",
    ])
    root, runs, discovery, _filters = inspector.discover(args)
    data = runs[0].as_dict(root)

    assert discovery["jsonl_files"] == 3
    assert data["events"] == 3
    assert [event["event"] for event in data["recent"]] == [
        "rotation.part_one", "rotation.part_two",
    ]


def test_recent_events_include_bounded_redacted_operational_fields(tmp_path,
                                                                   capsys):
    _line(tmp_path / "fields.jsonl", {
        "time": "2026-08-18T10:00:00Z", "run_id": "fields-run",
        "seq": 1, "level": "warn", "event": "queue.degraded",
        "surface": "worker", "fields": {
            "queue_depth": 7, "audio_bytes": 2048, "stdout_chars": 90,
            "output_tokens": 12, "errno": 32, "status": "retrying",
            "transcript": "private dictated words",
            "accessToken": "private-access-token",
        },
    })
    args = inspector.parse_args([
        "--dir", str(tmp_path), "--latest", "1", "--tail", "5",
        "--level", "warn",
    ])
    root, runs, _discovery, _filters = inspector.discover(args)
    event = runs[0].as_dict(root)["recent"][0]

    assert event["fields"]["queue_depth"] == 7
    assert event["fields"]["audio_bytes"] == 2048
    assert event["fields"]["stdout_chars"] == 90
    assert event["fields"]["output_tokens"] == 12
    encoded = json.dumps(event)
    assert "private dictated words" not in encoded
    assert "private-access-token" not in encoded
    inspector.print_event(event)
    printed = capsys.readouterr().out
    assert "queue_depth" in printed and "audio_bytes" in printed


def test_exception_messages_reasons_and_stack_headlines_are_never_reexposed(
        tmp_path):
    canary = "non secret private phrase from upstream"
    _line(tmp_path / "privacy.jsonl", {
        "time": "2026-08-18T10:00:00Z", "run_id": "privacy-run",
        "seq": 1, "level": "error", "event": "upstream.failed",
        "surface": "daemon", "fields": {"reason": canary},
        "exception": {
            "type": "RuntimeError", "message": canary,
            "fingerprint": "abcdef0123456789",
            "stack": (
                "RuntimeError: %s\n"
                "    at %s (https://user:password@example.test/private/path.js"
                "?token=hidden:4:2)\n"
                "    at eval at %s (https://example.test/eval.js:9:1)"
            ) % (canary, canary, canary),
        },
    })
    args = inspector.parse_args([
        "--dir", str(tmp_path), "--latest", "1", "--tail", "5",
        "--level", "error",
    ])
    root, runs, _discovery, _filters = inspector.discover(args)
    encoded = json.dumps(runs[0].as_dict(root))
    assert canary not in encoded
    assert "abcdef0123456789" in encoded
    assert "example.test" not in encoded
    assert "password" not in encoded
    assert "token=hidden" not in encoded
    assert "private/path.js" not in encoded
    assert "path.js:4:2" in encoded


def test_discovery_does_not_follow_jsonl_symlinks_outside_root(tmp_path):
    outside = tmp_path.parent / (tmp_path.name + "-outside.jsonl")
    _line(outside, {
        "time": "2026-08-18T10:00:00Z", "run_id": "outside-run",
        "seq": 1, "level": "error", "event": "outside.private",
    })
    (tmp_path / "linked.jsonl").symlink_to(outside)
    args = inspector.parse_args([
        "--dir", str(tmp_path), "--latest", "5", "--level", "debug",
    ])
    _root, runs, discovery, _filters = inspector.discover(args)
    assert runs == []
    assert discovery["jsonl_files"] == 0


def test_text_and_json_output_escape_terminal_and_bidi_controls(tmp_path,
                                                                 capsys):
    hostile = "safe\x1b]8;;https://example.test\x07link\x1b]8;;\x07\rFAKE\u202e"
    path = tmp_path / ("hostile\x1b[31m\u202e.jsonl")
    _line(path, {
        "time": "2026-08-18T10:00:00Z",
        "run_id": "run\nFAKE-RUN\x1b[2J",
        "seq": 1,
        "level": "error",
        "event": hostile,
        "surface": "worker\tname",
        "fields": {"status": hostile, "safe\u2066key": 1},
        "exception": {"type": "Error\x1b[31m", "fingerprint": "abcdef0123456789"},
    })
    args = inspector.parse_args([
        "--dir", str(tmp_path), "--latest", "1", "--tail", "5",
        "--level", "error",
    ])
    root, runs, discovery, filters = inspector.discover(args)

    inspector.print_summary(root, runs, discovery, args, filters)
    text_output = capsys.readouterr().out
    json_output = json.dumps(inspector.redact({
        "runs": [run.as_dict(root) for run in runs],
    }), ensure_ascii=False)
    combined = text_output + json_output

    for raw in ("\x1b", "\x07", "\r", "\u202e", "\u2066"):
        assert raw not in combined
    assert r"\x1b" in combined
    assert r"\rFAKE" in combined
    assert r"\u202e" in combined
    assert "\nFAKE-RUN" not in text_output
    assert r"\nFAKE-RUN" in text_output


def test_oversized_untrusted_records_are_skipped_without_hiding_later_lines(
        tmp_path):
    (tmp_path / "latest-hostile.json").write_text(
        '{"padding":"' + "m" * inspector.MAX_METADATA_BYTES + '"}',
        encoding="utf-8")
    log = tmp_path / "hostile.jsonl"
    with log.open("w", encoding="utf-8") as stream:
        stream.write("x" * (inspector.MAX_JSONL_LINE_CHARS + 1) + "\n")
        stream.write(json.dumps({
            "time": "2026-08-18T10:00:00Z", "run_id": "bounded-run",
            "seq": 1, "level": "error", "event": "later.valid",
            "surface": "worker", "fields": {},
        }) + "\n")
    args = inspector.parse_args([
        "--dir", str(tmp_path), "--latest", "5", "--tail", "5",
        "--level", "debug",
    ])

    root, runs, discovery, _filters = inspector.discover(args)
    data = {run.run_id: run.as_dict(root) for run in runs}

    assert data["bounded-run"]["events"] == 1
    assert data["bounded-run"]["recent"][0]["event"] == "later.valid"
    assert discovery["malformed_lines"] == 1
    assert len(discovery["read_errors"]) == 1


def test_per_run_read_error_sample_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(inspector, "MAX_READ_ERRORS", 2)
    run = inspector.RunSummary("path:hostile", tail=0)
    discovery = {"read_errors": [], "read_errors_omitted": 0}

    for index in range(7):
        run.note_read_error("unreadable-%d" % index)
        inspector._note_discovery_error(discovery, "unreadable-%d" % index)

    data = run.as_dict(tmp_path)
    assert data["read_errors"] == ["unreadable-0", "unreadable-1"]
    assert data["read_errors_omitted"] == 5
    assert discovery["read_errors"] == ["unreadable-0", "unreadable-1"]
    assert discovery["read_errors_omitted"] == 5


def test_second_redaction_preserves_numeric_event_summary_counts():
    result = inspector.redact({
        "top_events": {
            "text_repl.started": 2,
            "transcript.pipeline.finished": 1,
        },
        "fields": {"text": "must remain hidden"},
    })

    assert result["top_events"] == {
        "text_repl.started": 2,
        "transcript.pipeline.finished": 1,
    }
    assert result["fields"]["text"] == "<redacted>"


def test_global_reader_budgets_bound_records_and_retained_event_views(
        tmp_path, monkeypatch):
    log = tmp_path / "bounded.jsonl"
    for index in range(10):
        _line(log, {
            "time": "2026-08-18T10:00:%02dZ" % index,
            "run_id": "budget-run", "seq": index + 1,
            "level": "info", "event": "sample.%d" % index,
            "surface": "worker", "fields": {"count": index},
        }, append=index > 0)
    monkeypatch.setattr(inspector, "MAX_TOTAL_RECORDS", 2)
    monkeypatch.setattr(inspector, "MAX_TOTAL_RETAINED_EVENTS", 1)
    args = inspector.parse_args([
        "--dir", str(tmp_path), "--latest", "5", "--tail", "5",
        "--level", "debug",
    ])

    _root, runs, discovery, _filters = inspector.discover(args)
    data = runs[0].as_dict(tmp_path)

    assert discovery["records_read"] == 2
    assert discovery["work_limited"] >= 1
    assert discovery["events_retained"] == 1
    assert discovery["tail_events_dropped"] == 1
    assert [event["event"] for event in data["recent"]] == ["sample.1"]


def test_discovery_caps_nonmatching_directory_entries(tmp_path, monkeypatch):
    for index in range(8):
        (tmp_path / ("noise-%d.txt" % index)).write_text("x")
    monkeypatch.setattr(inspector, "MAX_DISCOVERY_ENTRIES", 3)
    args = inspector.parse_args([
        "--dir", str(tmp_path), "--latest", "1", "--tail", "1",
    ])

    _root, _runs, discovery, _filters = inspector.discover(args)

    assert discovery["work_limited"] == 1
    assert discovery["files_skipped"] == 1


def test_secure_reader_rejects_symlinked_intermediate_directories(tmp_path):
    root = tmp_path / "diagnostics"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.jsonl").write_text("PRIVATE-CANARY\n")
    (root / "nested").symlink_to(outside, target_is_directory=True)

    with pytest.raises(OSError):
        inspector._open_regular_text(
            root / "nested" / "private.jsonl", root=root)
