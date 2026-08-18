"""Structured diagnostics: safe schema, correlation, redaction, and hooks."""

import asyncio
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import pytest

from echoecho_app import diagnostics


@pytest.fixture(autouse=True)
def isolated_run():
    diagnostics.shutdown(outcome="test_reset")
    yield
    diagnostics.shutdown(outcome="test_teardown")


def records(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def one_event(path, name):
    return next(item for item in records(path) if item["event"] == name)


def test_functions_are_safe_noops_before_configure():
    assert diagnostics.get_run_id() is None
    assert diagnostics.get_log_path() is None
    assert diagnostics.get_context() == {}
    assert diagnostics.info("nothing") is False
    assert diagnostics.metric("queue.depth", 2) is False
    assert diagnostics.counter("ignored") is False
    assert diagnostics.install_asyncio() is False
    with diagnostics.context(session_id="not-active"):
        assert diagnostics.get_context() == {}
    with diagnostics.span("not-active") as operation:
        assert operation.span_id.startswith("span-")
    assert re.fullmatch(r"task-[0-9a-f]{16}", diagnostics.new_id("task"))


def test_schema_sequence_pointer_and_private_files(tmp_path):
    run = diagnostics.configure(
        "voice-daemon", mode="voice", log_dir=tmp_path,
        version="2.4", sha="abc123")
    run_id = diagnostics.get_run_id()
    path = diagnostics.get_log_path()
    assert run is not None and run_id.startswith("run-")
    assert path is not None and path.parent == tmp_path
    markers = list(tmp_path.glob("active-*.json"))
    assert len(markers) == 1
    assert re.fullmatch(
        r"active-run-[0-9a-f]{16}-p[1-9][0-9]{0,9}-voice-daemon\.json",
        markers[0].name)
    marker_data = json.loads(markers[0].read_text(encoding="utf-8"))
    assert marker_data == {
        "schema_version": diagnostics.SCHEMA_VERSION,
        "run_id": run_id,
        "component": "voice-daemon",
        "pid": os.getpid(),
        "started_at": marker_data["started_at"],
    }
    assert re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:"
        r"[0-9]{2}\.[0-9]{3}Z", marker_data["started_at"])
    if os.name != "nt":
        assert markers[0].stat().st_mode & 0o077 == 0

    diagnostics.info("daemon.ready", port=8765)
    diagnostics.warning("daemon.degraded", reason="fake device")
    assert diagnostics.shutdown(outcome="ok", sessions=1)

    got = records(path)
    assert [event["event"] for event in got] == [
        "run.start", "daemon.ready", "daemon.degraded", "run.summary"]
    assert [event["seq"] for event in got] == list(range(1, len(got) + 1))
    assert {event["run_id"] for event in got} == {run_id}
    for event in got:
        assert event["schema_version"] == diagnostics.SCHEMA_VERSION
        assert event["component"] == "voice-daemon"
        assert event["ts"].endswith("Z")
        assert isinstance(event["wall_time"], float)
        assert event["monotonic_ms"] >= 0
        assert event["pid"] == os.getpid()
        assert event["thread"]["name"]
        assert "context" in event and "fields" in event
    assert got[-1]["duration_ms"] >= 0
    assert got[-1]["fields"]["outcome"] == "ok"

    pointer = json.loads((tmp_path / "latest.json").read_text(encoding="utf-8"))
    component_pointer = json.loads(
        (tmp_path / "latest-voice-daemon.json").read_text(encoding="utf-8"))
    assert pointer == component_pointer
    assert pointer["run_id"] == run_id
    assert pointer["log_file"] == path.name
    assert pointer["outcome"] == "ok" and pointer["ended_at"].endswith("Z")
    assert not list(tmp_path.glob("active-*.json"))
    if os.name != "nt":
        assert path.stat().st_mode & 0o077 == 0
        assert (tmp_path / "latest.json").stat().st_mode & 0o077 == 0


def test_compound_error_outcome_marks_terminal_summary_as_error(tmp_path):
    diagnostics.configure("configuration", log_dir=tmp_path)
    path = diagnostics.get_log_path()
    diagnostics.shutdown(outcome="configuration_error")

    summary = one_event(path, "run.summary")
    assert summary["level"] == "error"
    assert summary["fields"]["outcome"] == "configuration_error"


def test_invalid_cli_still_honors_early_diagnostics_privacy_controls(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    disabled_dir = tmp_path / "disabled"
    env = os.environ.copy()
    env["ECHOECHO_DIAGNOSTICS_DIR"] = str(disabled_dir)
    disabled = subprocess.run(
        [sys.executable, str(repo / "echoecho.py"),
         "--no-diagnostics", "--not-a-real-option"],
        cwd=repo, env=env, capture_output=True, text=True, timeout=10)
    assert disabled.returncode == 2
    assert not disabled_dir.exists()

    custom_dir = tmp_path / "custom"
    relocated = subprocess.run(
        [sys.executable, str(repo / "echoecho.py"),
         "--diagnostics-dir", str(custom_dir), "--not-a-real-option"],
        cwd=repo, env=env, capture_output=True, text=True, timeout=10)
    assert relocated.returncode == 2
    assert list(custom_dir.glob("*.jsonl"))
    assert not disabled_dir.exists()


def test_recursive_redaction_content_privacy_and_hard_size_bound(tmp_path):
    diagnostics.configure(
        "privacy", mode="test", log_dir=tmp_path, max_event_bytes=4096)
    path = diagnostics.get_log_path()
    diagnostics.info(
        "privacy.check",
        authorization="Bearer top-secret-auth-token",
        nested={
            "api_key": "sk-live-abcdefghijklmnop",
            "token": "plain-token-value",
            "refresh_token": "refresh-me-please",
            "endpoint": "vnc://user:supersecret@127.0.0.1:5900/?token=url-secret",
            "text": "the user's private dictated sentence",
            "safe_count": 7,
        },
        cyclic=None)
    # Force final-record bounding, not just per-string truncation.
    diagnostics.info(
        "privacy.bounded",
        payload={"item_%02d" % i: "z" * 5000 for i in range(50)})
    diagnostics.shutdown()

    private = one_event(path, "privacy.check")
    encoded = json.dumps(private)
    for secret in (
            "top-secret-auth-token", "sk-live-abcdefghijklmnop",
            "plain-token-value", "refresh-me-please", "supersecret", "url-secret",
            "the user's private dictated sentence"):
        assert secret not in encoded
    assert private["fields"]["authorization"] == "[REDACTED]"
    assert private["fields"]["nested"]["api_key"] == "[REDACTED]"
    assert private["fields"]["nested"]["text"] == "[CONTENT REDACTED]"
    assert private["fields"]["nested"]["safe_count"] == 7
    assert "vnc://[REDACTED]@127.0.0.1:5900/?token=[REDACTED]" in encoded

    bounded_line = next(line for line in path.read_text(encoding="utf-8").splitlines()
                        if '"event":"privacy.bounded"' in line)
    assert len(bounded_line.encode("utf-8")) <= 4096
    bounded = json.loads(bounded_line)
    assert bounded["truncated"] is True
    assert bounded["fields"]["_truncated"] is True


def test_summary_maps_preserve_numeric_counts_for_content_named_events():
    clean = diagnostics._sanitize({
        "event_counts": {"text_repl.started": 2},
        "latest_metrics": {"transcript.latency": 14.5},
        "text": "must remain hidden",
    })

    assert clean["event_counts"]["text_repl.started"] == 2
    assert clean["latest_metrics"]["transcript.latency"] == 14.5
    assert clean["text"] == "[CONTENT REDACTED]"


def test_redaction_classifies_full_key_before_truncating_its_display_name():
    secret = "credential-canary-after-a-long-key"
    private = "private-body-canary-after-a-long-key"
    value = diagnostics._sanitize({
        "x" * 300 + "_accessToken": secret,
        "y" * 300 + "_requestBody": private,
        "z" * (diagnostics.MAX_CLASSIFICATION_KEY + 1): "oversized-canary",
    })

    encoded = json.dumps(value)
    assert secret not in encoded
    assert private not in encoded
    assert "oversized-canary" not in encoded
    assert "[REDACTED]" in encoded


def test_redaction_handles_camel_case_keys_and_empty_uri_usernames():
    clean = diagnostics._sanitize({
        "accessToken": "top-secret",
        "authToken": "auth-secret",
        "session_token": "session-secret",
        "secretKey": "key-secret",
        "requestBody": "private prompt",
        "endpoint": "vnc://:viewer-password@127.0.0.1:5900",
        "username_only_endpoint": "https://username-token-canary@example.test/path",
        "assignment": ("OPENAI_API_KEY=plain-key-canary "
                       "AUTH_TOKEN=plain-token-canary "
                       "MY_PASSWORD: plain-password-canary"),
        "token_count": 3,
        "input_tokens": 10,
        "apiKeyValue": "credential-canary",
        "passwordValue": "password-canary",
        "authorizationHeader": "Basic authorization-canary",
        "transcriptPreview": "dictation-canary",
        "promptValue": "prompt-canary",
        "requestBodyJson": "body-canary",
        "textSnippet": "text-canary",
        "audio_bytes": "not-a-real-measurement-canary",
    })
    assert clean["accessToken"] == "[REDACTED]"
    assert clean["authToken"] == "[REDACTED]"
    assert clean["session_token"] == "[REDACTED]"
    assert clean["secretKey"] == "[REDACTED]"
    assert clean["requestBody"] == "[CONTENT REDACTED]"
    assert clean["token_count"] == 3
    assert clean["input_tokens"] == 10
    assert clean["apiKeyValue"] == "[REDACTED]"
    assert clean["passwordValue"] == "[REDACTED]"
    assert clean["authorizationHeader"] == "[REDACTED]"
    assert clean["transcriptPreview"] == "[CONTENT REDACTED]"
    assert clean["promptValue"] == "[CONTENT REDACTED]"
    assert clean["requestBodyJson"] == "[CONTENT REDACTED]"
    assert clean["textSnippet"] == "[CONTENT REDACTED]"
    assert clean["audio_bytes"] == "[CONTENT REDACTED]"
    assert "viewer-password" not in clean["endpoint"]
    assert clean["endpoint"].startswith("vnc://[REDACTED]@")
    assert "username-token-canary" not in clean["username_only_endpoint"]
    assert clean["username_only_endpoint"].startswith("https://[REDACTED]@")
    assert "plain-key-canary" not in clean["assignment"]
    assert "plain-token-canary" not in clean["assignment"]
    assert "plain-password-canary" not in clean["assignment"]


def _raise_secret_error():
    raise RuntimeError("upstream rejected Bearer secret-session-token")


def test_exception_has_type_private_message_fingerprint_and_stack(tmp_path):
    diagnostics.configure("errors", log_dir=tmp_path)
    path = diagnostics.get_log_path()
    try:
        _raise_secret_error()
    except RuntimeError as exc:
        # ``event`` is also a useful domain field (task-log failures use it),
        # so the public event-name argument is positional-only.
        diagnostics.exception("request.failed", exc=exc, phase="connect",
                              event="upstream.disconnect")
    diagnostics.shutdown(outcome="error")

    failed = one_event(path, "request.failed")
    detail = failed["exception"]
    assert failed["level"] == "error"
    assert detail["type"] == "RuntimeError"
    assert detail["module"] == "builtins"
    assert failed["fields"]["event"] == "upstream.disconnect"
    assert detail["message"] == "[CONTENT REDACTED]"
    assert detail["message_length"] > 0
    assert re.fullmatch(r"[0-9a-f]{16}", detail["fingerprint"])
    assert "secret-session-token" not in json.dumps(detail)
    assert "_raise_secret_error" in detail["stack"]
    assert "RuntimeError" in detail["stack"]


def test_exception_stack_keeps_basename_but_not_private_directories(tmp_path):
    diagnostics.configure("stack-privacy", log_dir=tmp_path)
    path = diagnostics.get_log_path()
    try:
        exec(compile(
            "raise RuntimeError('private')",
            "/tmp/private-customer-canary/generated_worker.py", "exec"), {})
    except RuntimeError as exc:
        diagnostics.exception("worker.failed", exc=exc)

    detail = one_event(path, "worker.failed")["exception"]
    assert "generated_worker.py" in detail["stack"]
    assert "private-customer-canary" not in detail["stack"]


def test_context_span_metrics_counters_and_shutdown_summary(tmp_path):
    diagnostics.configure("worker", mode="test", log_dir=tmp_path)
    path = diagnostics.get_log_path()
    assert diagnostics.get_context() == {}

    with diagnostics.context(session_id="s-1", task_id="t-9"):
        snapshot = diagnostics.get_context()
        snapshot["task_id"] = "mutated-copy"
        assert diagnostics.get_context()["task_id"] == "t-9"
        with diagnostics.span("agent.execute", sandbox="shell") as operation:
            diagnostics.info("agent.progress", progress_count=2)
            diagnostics.metric("agent.latency", 12.5, unit="ms", phase="spawn")
            diagnostics.counter("agent.lines", amount=2)
            diagnostics.counter("agent.lines", amount=3)
    assert diagnostics.get_context() == {}
    diagnostics.shutdown(outcome="ok")

    got = records(path)
    start = next(event for event in got if event["event"] == "agent.execute.start")
    progress = next(event for event in got if event["event"] == "agent.progress")
    end = next(event for event in got if event["event"] == "agent.execute.end")
    assert start["context"]["session_id"] == "s-1"
    assert progress["context"]["task_id"] == "t-9"
    assert start["context"]["span_id"] == operation.span_id
    assert end["context"]["span_id"] == operation.span_id
    assert end["fields"]["outcome"] == "ok" and end["duration_ms"] >= 0

    metric = next(event for event in got if event["event"] == "metric")
    assert metric["fields"] == {
        "name": "agent.latency", "value": 12.5, "unit": "ms", "phase": "spawn"}
    counters = [event for event in got if event["event"] == "counter"]
    assert counters[-1]["fields"]["value"] == 5.0
    summary = next(event for event in got if event["event"] == "run.summary")
    assert summary["fields"]["counters"]["agent.lines"] == 5.0
    assert summary["fields"]["latest_metrics"]["agent.latency"] == 12.5
    assert summary["fields"]["event_counts"]["agent.progress"] == 1


def test_span_records_exception_and_does_not_suppress_it(tmp_path):
    diagnostics.configure("span-error", log_dir=tmp_path)
    path = diagnostics.get_log_path()
    with pytest.raises(ValueError, match="broken"):
        with diagnostics.span("fragile.operation"):
            raise ValueError("broken")
    diagnostics.shutdown(outcome="error")

    ended = one_event(path, "fragile.operation.end")
    assert ended["level"] == "error"
    assert ended["fields"]["outcome"] == "error"
    assert ended["exception"]["type"] == "ValueError"
    assert ended["duration_ms"] >= 0


def test_asyncio_hook_records_and_chains_existing_handler(tmp_path):
    loop = asyncio.new_event_loop()
    chained = []

    def previous(active_loop, details):
        chained.append((active_loop, details["message"]))

    loop.set_exception_handler(previous)
    try:
        diagnostics.configure("async-runtime", log_dir=tmp_path)
        path = diagnostics.get_log_path()
        assert diagnostics.install_asyncio(loop) is True
        installed = loop.get_exception_handler()
        installed(loop, {
            "message": "Task exception was never retrieved",
            "exception": LookupError("missing item"),
        })
        diagnostics.shutdown(outcome="ok")
        assert loop.get_exception_handler() is previous
    finally:
        loop.close()

    assert chained and chained[0][0] is loop
    event = one_event(path, "asyncio.unhandled_exception")
    assert event["exception"]["type"] == "LookupError"
    assert event["fields"]["message"] == "[CONTENT REDACTED]"
    assert event["fields"]["message_length"] == len(
        "Task exception was never retrieved")
    assert re.fullmatch(r"[0-9a-f]{16}",
                        event["fields"]["message_fingerprint"])


def test_unusable_directory_disables_sink_without_raising(tmp_path):
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("occupied", encoding="utf-8")
    run = diagnostics.configure("disabled", log_dir=blocker / "child")
    assert run is not None
    assert diagnostics.get_run_id().startswith("run-")
    assert diagnostics.get_log_path() is None
    assert diagnostics.error("still.safe", reason="disk unavailable") is False
    assert diagnostics.shutdown(outcome="error") is True


def test_diagnostics_env_can_disable_disk_but_keeps_correlation_id(
        tmp_path, monkeypatch):
    monkeypatch.setenv("ECHOECHO_DIAGNOSTICS", "0")
    run = diagnostics.configure("disabled-by-env", log_dir=tmp_path)
    assert run is not None
    assert diagnostics.get_run_id().startswith("run-")
    assert diagnostics.get_log_path() is None
    assert diagnostics.info("not.written") is False
    diagnostics.shutdown()
    assert list(tmp_path.iterdir()) == []


def test_existing_custom_directory_permissions_are_not_changed(tmp_path):
    existing = tmp_path / "shared-diagnostics"
    existing.mkdir(mode=0o755)
    os.chmod(existing, 0o755)
    before = existing.stat().st_mode & 0o777

    diagnostics.configure("permissions", log_dir=existing)
    path = diagnostics.get_log_path()
    diagnostics.shutdown()

    assert existing.stat().st_mode & 0o777 == before
    if os.name != "nt":
        assert path.stat().st_mode & 0o077 == 0


def test_retention_removes_only_this_writers_old_runs(tmp_path):
    old = tmp_path / "20200101T000000Z_run-aabbcc_worker.jsonl"
    newer = tmp_path / "20210101T000000Z_run-ddeeff_worker.jsonl"
    foreign = tmp_path / "run-electron-format.jsonl"
    for path in (old, newer, foreign):
        path.write_text("{}\n", encoding="utf-8")
    os.utime(old, (1, 1))
    os.utime(newer, (2, 2))
    os.utime(foreign, (1, 1))

    diagnostics.configure(
        "retention", log_dir=tmp_path, retention_days=0, max_runs=1)
    current = diagnostics.get_log_path()
    diagnostics.shutdown()

    assert current.exists()
    assert foreign.exists()  # Electron/unknown producer owns its own retention.
    assert not old.exists() and not newer.exists()
    assert sorted(tmp_path.glob("*.jsonl")) == [current, foreign]


def test_rotation_keeps_bounded_recent_parts_and_pointer(tmp_path):
    run = diagnostics.configure(
        "rotation", log_dir=tmp_path, max_run_bytes=64 * 1024, max_parts=2)
    run_id = run.run_id
    for index in range(180):
        diagnostics.info("rotation.sample", index=index,
                         payload={"blob": "x" * 4000})
    diagnostics.shutdown()

    parts = sorted(tmp_path.glob("*_%s_*.jsonl" % run_id))
    assert len(parts) == 2
    got = sorted((record for part in parts for record in records(part)),
                 key=lambda record: record["seq"])
    assert got and got[-1]["event"] == "run.summary"
    assert [record["seq"] for record in got] == sorted(
        record["seq"] for record in got)
    pointer = json.loads((tmp_path / "latest-rotation.json").read_text())
    assert pointer["log_part"] >= 2
    assert pointer["log_files"] == [part.name for part in parts]
    assert all((tmp_path / name).is_file() for name in pointer["log_files"])


def test_retention_preserves_old_run_proven_live_by_active_marker(tmp_path):
    active = diagnostics.Run(
        "daemon", log_dir=tmp_path, retention_days=0, max_runs=1)
    trigger = None
    try:
        newer = tmp_path / "20210101T000000Z_run-ddeeff_livewriter.jsonl"
        newer.write_text("{}\n", encoding="utf-8")
        os.utime(active.path, (1, 1))
        os.utime(newer, (2, 2))
        trigger = diagnostics.Run(
            "retention-new", log_dir=tmp_path,
            retention_days=0, max_runs=1)
        assert trigger._apply_retention()

        assert active.path.exists()
        assert not newer.exists()
    finally:
        if trigger is not None:
            trigger.shutdown(outcome="test")
        active.shutdown(outcome="test")


def test_overlapping_same_component_runs_each_protect_their_parts(tmp_path):
    first = diagnostics.Run(
        "daemon", log_dir=tmp_path, retention_days=0, max_runs=1)
    second = None
    third = None
    try:
        assert first.enabled and first._active_marker.is_file()
        os.utime(first.path, (1, 1))
        second = diagnostics.Run(
            "daemon", log_dir=tmp_path, retention_days=0, max_runs=1)
        assert second.enabled and second._active_marker.is_file()
        os.utime(second.path, (2, 2))

        # The component pointer can represent only the newer daemon, while
        # both per-run markers remain independently visible to retention.
        pointer = json.loads(
            (tmp_path / "latest-daemon.json").read_text(encoding="utf-8"))
        assert pointer["run_id"] == second.run_id
        assert pointer["run_id"] != first.run_id
        assert len(list(tmp_path.glob("active-*.json"))) == 2

        third = diagnostics.Run(
            "retention-trigger", log_dir=tmp_path,
            retention_days=0, max_runs=1)
        assert third.enabled
        assert first.path.exists()
        assert second.path.exists()
    finally:
        for run in (third, second, first):
            if run is not None:
                run.shutdown(outcome="test")


def test_stale_crash_marker_is_pruned_and_does_not_protect_run(
        tmp_path, monkeypatch):
    run_id = "run-deadbeefdeadbeef"
    dead_pid = 2147483647
    stale_log = tmp_path / ("20200101T000000Z_%s_daemon.jsonl" % run_id)
    stale_log.write_text("{}\n", encoding="utf-8")
    os.utime(stale_log, (1, 1))
    marker = tmp_path / (
        "active-%s-p%d-daemon.json" % (run_id, dead_pid))
    marker.write_text(json.dumps({
        "schema_version": diagnostics.SCHEMA_VERSION,
        "run_id": run_id,
        "component": "daemon",
        "pid": dead_pid,
        "started_at": "2026-08-18T00:00:00.000Z",
    }), encoding="utf-8")
    os.chmod(marker, 0o600)
    real_kill = os.kill

    def process_probe(pid, signal):
        if pid == dead_pid and signal == 0:
            raise ProcessLookupError(pid)
        return real_kill(pid, signal)

    monkeypatch.setattr(diagnostics.os, "kill", process_probe)
    run = diagnostics.configure(
        "stale-cleanup", log_dir=tmp_path,
        retention_days=1, max_runs=1)

    assert run.enabled
    assert not marker.exists()
    assert not stale_log.exists()
    assert len(list(tmp_path.glob("active-*.json"))) == 1


def test_retention_never_follows_or_blocks_on_hostile_metadata_entries(tmp_path):
    diagnostics_dir = tmp_path / "diagnostics"
    diagnostics_dir.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({
        "pid": os.getpid(), "run_id": "run-deadbeef",
    }), encoding="utf-8")
    (diagnostics_dir / "latest-hostile.json").symlink_to(outside)
    marker_name = "active-run-deadbeefdeadbeef-p%d-hostile.json" % os.getpid()
    (diagnostics_dir / marker_name).symlink_to(outside)
    if hasattr(os, "mkfifo"):
        os.mkfifo(diagnostics_dir / "latest-pipe.json")
        os.mkfifo(diagnostics_dir /
                  "active-run-feedfacefeedface-p999999999-hostile.json")

    run = diagnostics.configure(
        "hostile-pointers", log_dir=diagnostics_dir,
        retention_days=0, max_runs=1)

    assert run is not None and run.enabled
    assert outside.read_text(encoding="utf-8").startswith("{")


def test_bounded_retention_scan_disables_only_diagnostics_when_exhausted(
        tmp_path, monkeypatch):
    monkeypatch.setattr(diagnostics, "MAX_RETENTION_ENTRIES", 3)
    for index in range(4):
        (tmp_path / ("unrelated-%d.txt" % index)).write_text("x")

    run = diagnostics.configure("bounded-retention", log_dir=tmp_path)

    assert run is not None and not run.enabled
    assert diagnostics.info("application.still.runs") is False
    assert list(tmp_path.glob("*.jsonl")) == []


def test_failed_historical_prune_does_not_add_an_unbounded_new_run(
        tmp_path, monkeypatch):
    old = tmp_path / "20200101T000000Z_run-aabbcc_worker.jsonl"
    old.write_text("{}\n", encoding="utf-8")
    os.utime(old, (1, 1))
    original_unlink = Path.unlink

    def refuse_old(path, *args, **kwargs):
        if path == old:
            raise PermissionError("retention denied")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", refuse_old)
    run = diagnostics.configure(
        "retention-failure", log_dir=tmp_path,
        retention_days=1, max_runs=1)

    assert run is not None and not run.enabled
    assert sorted(tmp_path.glob("*.jsonl")) == [old]


def test_atomic_pointer_does_not_follow_old_predictable_temp_symlink(tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("unchanged", encoding="utf-8")
    old_temp = tmp_path / (".latest.json.%d.tmp" % os.getpid())
    old_temp.symlink_to(victim)

    diagnostics.configure("pointer-safe", log_dir=tmp_path)
    diagnostics.info("pointer.update")
    diagnostics.shutdown()

    assert victim.read_text(encoding="utf-8") == "unchanged"


def test_reserved_fields_and_arbitrary_exception_fields_never_break_spans(tmp_path):
    diagnostics.configure("collisions", log_dir=tmp_path)
    path = diagnostics.get_log_path()
    with diagnostics.span(
            "collision", outcome="caller-value", duration_ms=999,
            exception=RuntimeError("private arbitrary exception phrase")):
        pass
    diagnostics.shutdown(
        outcome="ok", duration_ms=1, level="critical",
        exception=RuntimeError("private shutdown phrase"))

    started = one_event(path, "collision.start")
    ended = one_event(path, "collision.end")
    assert ended["fields"]["outcome"] == "ok"
    assert ended["fields"]["caller_outcome"] == "caller-value"
    assert ended["duration_ms"] < 999
    assert started["fields"]["caller_duration_ms"] == 999
    assert started["fields"]["caller_exception"]["type"] == "RuntimeError"
    summary = one_event(path, "run.summary")
    assert summary["fields"]["shutdown_duration_ms"] == 1
    assert summary["fields"]["shutdown_level"] == "critical"
    assert "private" not in json.dumps(records(path))


def test_invalid_configuration_falls_back_to_safe_bounded_defaults(tmp_path):
    run = diagnostics.configure(
        "invalid", log_dir=tmp_path, max_runs="not-an-integer")
    assert run is not None and run.max_runs == diagnostics.DEFAULT_MAX_RUNS
    assert diagnostics.info("still.works") is True
    with diagnostics.span("safe.span"):
        pass


def test_configuration_values_have_hard_upper_bounds(tmp_path):
    run = diagnostics.configure(
        "bounded-config", log_dir=tmp_path,
        retention_days=float("inf"), max_runs=10 ** 20,
        max_event_bytes=10 ** 20, max_run_bytes=10 ** 20,
        max_parts=10 ** 20, max_nodes=10 ** 20)

    assert run.retention_days == diagnostics.DEFAULT_RETENTION_DAYS
    assert run.max_runs == diagnostics.MAX_RUNS_LIMIT
    assert run.max_event_bytes == diagnostics.MAX_EVENT_BYTES_LIMIT
    assert run.max_run_bytes == diagnostics.MAX_RUN_BYTES_LIMIT
    assert run.max_parts == diagnostics.MAX_PARTS_LIMIT
    assert run.max_nodes == diagnostics.MAX_NODES_LIMIT


def test_rotation_collision_is_not_unlinked_and_keeps_active_part_bounded(
        tmp_path):
    run = diagnostics.configure(
        "rotation-collision", log_dir=tmp_path,
        max_run_bytes=64 * 1024, max_parts=2)
    base = run._base_path
    victim = tmp_path / "victim.txt"
    victim.write_text("untouched", encoding="utf-8")
    collision = base.with_name("%s.part-001.jsonl" % base.stem)
    collision.symlink_to(victim)

    results = [diagnostics.info(
        "rotation.sample", index=index, payload={"blob": "x" * 4000})
               for index in range(100)]

    assert any(result is False for result in results)
    assert collision.is_symlink()
    assert victim.read_text(encoding="utf-8") == "untouched"
    assert base.stat().st_size <= run.max_run_bytes
    assert run.path == base
    assert run._write_failures > 0 and run._dropped_events > 0


def test_rotation_stops_after_retention_unlink_failure(tmp_path, monkeypatch):
    run = diagnostics.configure(
        "rotation-retention", log_dir=tmp_path,
        max_run_bytes=64 * 1024, max_parts=1)
    base = run._base_path
    original_unlink = Path.unlink

    def refuse_base(path, *args, **kwargs):
        if path == base:
            raise PermissionError("retention denied")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", refuse_base)
    try:
        for index in range(300):
            diagnostics.info(
                "rotation.sample", index=index,
                payload={"blob": "x" * 4000})
    finally:
        monkeypatch.setattr(Path, "unlink", original_unlink)

    parts = sorted(tmp_path.glob("*_%s_*.jsonl" % run.run_id))
    assert run._rotation_retention_failed is True
    assert run._dropped_events > 0
    # Opening the replacement before retiring the current part permits one
    # bounded extra file; no later part is created after cleanup fails.
    assert len(parts) == 2
    assert run._part == 1
    assert all(path.stat().st_size <= run.max_run_bytes for path in parts)


def test_sanitizer_has_a_total_node_budget_for_shared_reference_dags():
    value = {"leaf": 1}
    for _ in range(8):
        value = {"branch_%02d" % index: value for index in range(50)}
    clean = diagnostics._sanitize(value, max_depth=20, max_nodes=128)
    encoded = json.dumps(clean)
    assert len(encoded) < 20000
    assert "[TRUNCATED]" in encoded


def test_run_summary_maps_have_bounded_name_cardinality(tmp_path):
    run = diagnostics.configure("cardinality", log_dir=tmp_path)
    for index in range(diagnostics.MAX_SUMMARY_KEYS + 50):
        diagnostics.info("plugin.event.%d" % index)
        diagnostics.metric("plugin.metric.%d" % index, index)
        diagnostics.counter("plugin.counter.%d" % index)

    assert len(run._event_counts) <= diagnostics.MAX_SUMMARY_KEYS
    assert len(run._metric_latest) <= diagnostics.MAX_SUMMARY_KEYS
    assert len(run._counters) <= diagnostics.MAX_SUMMARY_KEYS
    assert run._event_counts["__other_events__"] > 0
    assert "__other_metrics__" in run._metric_latest
    assert run._counters["__other_counters__"] > 0
