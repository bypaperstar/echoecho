import importlib.util
import io
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest


REPO = Path(__file__).resolve().parents[1]
CAPTURE = REPO / "scripts" / "console_capture.py"
SPEC = importlib.util.spec_from_file_location("echoecho_console_capture", CAPTURE)
console_capture = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(console_capture)


def _run_capture(log, payload, *, max_bytes=96, max_parts=3):
    env = os.environ.copy()
    env["ECHOECHO_CONSOLE_MAX_BYTES"] = str(max_bytes)
    env["ECHOECHO_CONSOLE_MAX_PARTS"] = str(max_parts)
    return subprocess.run(
        [sys.executable, str(CAPTURE), "--log", str(log)],
        input=payload,
        env=env,
        capture_output=True,
        timeout=10,
    )


def _create(directory, component="daemon", *, max_runs=10):
    env = os.environ.copy()
    env["ECHOECHO_CONSOLE_MAX_RUNS"] = str(max_runs)
    result = subprocess.run(
        [sys.executable, str(CAPTURE), "--create-dir", str(directory),
         "--component", component],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    return Path(result.stdout.strip())


def _parts(log):
    return sorted(log.parent.glob(f"{log.stem}.part-*{log.suffix}"))


def _chronological(log):
    parts = sorted(
        _parts(log),
        key=lambda path: int(path.stem.rsplit(".part-", 1)[1]),
        reverse=True,
    )
    return b"".join(path.read_bytes() for path in [*parts, log])


def test_console_limits_clamp_environment_and_direct_construction(
        tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_CONSOLE_LIMIT", str(10 ** 30))
    assert console_capture._positive_env(
        "TEST_CONSOLE_LIMIT", 7, console_capture.MAX_PARTS_LIMIT
    ) == console_capture.MAX_PARTS_LIMIT

    log = tmp_path / "bounded-daemon.log"
    log.touch()
    sink = console_capture.RotatingCapture(
        log, max_bytes=10 ** 30, max_parts=10 ** 30)
    try:
        assert sink.max_bytes == console_capture.MAX_BYTES_LIMIT
        assert sink.max_parts == console_capture.MAX_PARTS_LIMIT
    finally:
        sink.close()


def test_capture_rotates_to_a_private_bounded_ring(tmp_path):
    log = tmp_path / "20260818T120000Z-123-daemon.log"
    log.touch(mode=0o644)
    payload = b"".join(f"record-{i:03d}:".encode() + b"x" * 19 + b"\n"
                       for i in range(40))

    result = _run_capture(log, payload, max_bytes=96, max_parts=3)

    assert result.returncode == 0, result.stderr.decode()
    retained = [*_parts(log), log]
    assert [path.name for path in _parts(log)] == [
        f"{log.stem}.part-001.log",
        f"{log.stem}.part-002.log",
    ]
    assert all(path.stat().st_size <= 96 for path in retained)
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in retained)
    combined = _chronological(log)
    assert payload.endswith(combined)
    assert combined


def test_tail_reassembles_lines_split_across_rotated_parts(tmp_path):
    log = tmp_path / (
        "20260818T120000Z-123-%s-orb.log" % ("a" * 32))
    log.touch()
    payload = b"".join(f"line-{i:02d}-abcdefghijklmno\n".encode()
                       for i in range(20))
    assert _run_capture(log, payload, max_bytes=41, max_parts=20).returncode == 0
    current = tmp_path / "orb-current.log"
    current.symlink_to(log.name)

    result = subprocess.run(
        [sys.executable, str(CAPTURE), "--log", str(current), "--tail", "5"],
        capture_output=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr.decode()
    assert result.stdout == b"".join(payload.splitlines(keepends=True)[-5:])


def test_tail_bounds_a_single_unterminated_line_across_many_parts(tmp_path):
    log = tmp_path / "20260818T120000Z-123-daemon.log"
    log.touch()
    payload = b"prefix-canary-" + b"x" * (
        console_capture.MAX_TAIL_LINE_BYTES * 3) + b"-recent-suffix"
    assert _run_capture(
        log, payload, max_bytes=4096, max_parts=100).returncode == 0
    output = io.BytesIO()

    console_capture.tail(log, 10, output)

    rendered = output.getvalue()
    assert rendered.startswith(b"[... console line prefix truncated ...]")
    assert rendered.endswith(b"-recent-suffix")
    assert b"prefix-canary" not in rendered
    assert len(rendered) <= (console_capture.MAX_TAIL_LINE_BYTES + 64)


def test_tail_seeks_to_recent_data_under_a_global_input_budget(
        tmp_path, monkeypatch):
    log = tmp_path / "20260818T120000Z-123-daemon.log"
    recent = b"recent-one\nrecent-two\n"
    with log.open("wb") as stream:
        stream.truncate(1024 * 1024)
        stream.seek(-len(recent), os.SEEK_END)
        stream.write(recent)
    monkeypatch.setattr(console_capture, "MAX_TAIL_INPUT_BYTES", 128)
    output = io.BytesIO()

    console_capture.tail(log, 2, output)

    assert output.getvalue().endswith(recent)
    assert len(output.getvalue()) <= (
        console_capture.MAX_TAIL_INPUT_BYTES * 4 + 64)


def test_tail_escapes_terminal_controls_by_default_with_explicit_raw_opt_in(
        tmp_path):
    log = tmp_path / "20260818T120000Z-123-daemon.log"
    hostile = (b"safe\x1b]8;;https://example.test\x07link\x1b]8;;\x07"
               b"\rFAKE\xc2\x9b31m\xe2\x80\xae\n")
    log.write_bytes(hostile)

    safe = subprocess.run(
        [sys.executable, str(CAPTURE), "--log", str(log), "--tail", "5"],
        capture_output=True, timeout=10)
    raw = subprocess.run(
        [sys.executable, str(CAPTURE), "--log", str(log), "--tail", "5",
         "--raw-tail"], capture_output=True, timeout=10)

    assert safe.returncode == 0
    assert b"\x1b" not in safe.stdout
    assert b"\rFAKE" not in safe.stdout
    assert "‮" not in safe.stdout.decode("utf-8")
    assert b"\\x1b" in safe.stdout
    assert b"\\x0dFAKE" in safe.stdout
    assert b"\\u202e" in safe.stdout
    assert raw.stdout == hostile


def test_tail_caps_total_directory_entries_not_only_matching_parts(
        tmp_path, monkeypatch):
    log = tmp_path / "20260818T120000Z-123-daemon.log"
    log.write_text("recent\n")
    for index in range(5):
        (tmp_path / ("noise-%d.txt" % index)).write_text("x")
    monkeypatch.setattr(console_capture, "MAX_TAIL_DIRECTORY_ENTRIES", 2)

    with pytest.raises(OSError, match="entry limit"):
        console_capture.tail(log, 2, io.BytesIO())


def test_unwritable_sink_is_drained_without_failing_the_producer(tmp_path):
    # A missing parent makes the sink fail before its first write. The helper
    # must nevertheless consume all stdin and exit successfully, avoiding a
    # producer-visible broken pipe.
    log = tmp_path / "missing" / "daemon.log"
    result = _run_capture(log, b"x" * (1024 * 1024), max_bytes=64,
                          max_parts=2)
    assert result.returncode == 0
    assert result.stderr == b""
    assert not log.exists()


def test_one_part_configuration_keeps_only_active_suffix(tmp_path):
    log = tmp_path / "20260818T120000Z-123-vm.log"
    log.touch()
    payload = bytes(range(256)) * 3

    assert _run_capture(log, payload, max_bytes=64, max_parts=1).returncode == 0

    assert _parts(log) == []
    assert 0 < log.stat().st_size <= 64
    assert payload.endswith(log.read_bytes())


def test_tail_rejects_current_symlink_outside_console_directory(tmp_path):
    outside = tmp_path / "private.txt"
    outside.write_text("do-not-print\n")
    console = tmp_path / "console"
    console.mkdir()
    current = console / "daemon-current.log"
    current.symlink_to(outside)

    result = subprocess.run(
        [sys.executable, str(CAPTURE), "--log", str(current), "--tail", "5"],
        capture_output=True,
        timeout=10,
    )

    assert result.returncode == 1
    assert b"do-not-print" not in result.stdout


def test_tail_rejects_current_symlink_to_unrelated_sibling(tmp_path):
    console = tmp_path / "console"
    console.mkdir()
    unrelated = console / "private.txt"
    unrelated.write_text("same-directory-private-canary\n")
    current = console / "daemon-current.log"
    current.symlink_to(unrelated.name)

    result = subprocess.run(
        [sys.executable, str(CAPTURE), "--log", str(current), "--tail", "5"],
        capture_output=True,
        timeout=10,
    )

    assert result.returncode == 1
    assert b"same-directory-private-canary" not in result.stdout


def test_create_is_exclusive_private_and_replaces_pointer_not_its_target(tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("untouched")
    current = tmp_path / "daemon-current.log"
    current.symlink_to(victim)

    first = _create(tmp_path)
    second = _create(tmp_path)

    assert first != second
    assert first.parent == tmp_path == second.parent
    assert first.name.endswith("-daemon.log")
    assert second.name.endswith("-daemon.log")
    assert stat.S_ISREG(first.lstat().st_mode)
    assert stat.S_ISREG(second.lstat().st_mode)
    assert stat.S_IMODE(first.stat().st_mode) == 0o600
    assert stat.S_IMODE(second.stat().st_mode) == 0o600
    assert victim.read_text() == "untouched"
    assert current.is_symlink()
    assert current.readlink() == Path(second.name)


def test_create_rejects_a_symlink_console_directory(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    console = tmp_path / "console"
    console.symlink_to(outside, target_is_directory=True)

    result = subprocess.run(
        [sys.executable, str(CAPTURE), "--create-dir", str(console),
         "--component", "daemon"],
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 1
    assert list(outside.iterdir()) == []


def test_launch_count_and_part_ring_bound_total_component_storage(tmp_path):
    payload = b"diagnostic-output\n" * 100
    orphan = tmp_path / (
        "20260818T000000Z-1-%s-daemon.part-001.log" % ("a" * 32))
    orphan.write_bytes(b"orphan")
    for _ in range(4):
        log = _create(tmp_path, max_runs=2)
        assert _run_capture(log, payload, max_bytes=64,
                            max_parts=3).returncode == 0

    roots = sorted(tmp_path.glob("*-daemon.log"))
    retained = []
    for root in roots:
        retained.extend([root, *_parts(root)])

    assert len(roots) == 2
    assert len(retained) <= 2 * 3
    assert sum(path.stat().st_size for path in retained) <= 2 * 3 * 64
    assert all(path.stat().st_size <= 64 for path in retained)
    assert not orphan.exists()


def test_retention_only_deletes_exact_generated_component_names(tmp_path):
    unrelated = [
        tmp_path / "notes-daemon.log",
        tmp_path / "orb-run-personal-not-owned-daemon.log",
        tmp_path / "20260818T000000Z-1-not-a-uuid-daemon.log",
        tmp_path / ("20260818T000000Z-1-%s-other-daemon.log" % ("b" * 32)),
    ]
    for path in unrelated:
        path.write_text("must survive")

    for _ in range(3):
        _create(tmp_path, component="daemon", max_runs=1)

    assert all(path.read_text() == "must survive" for path in unrelated)
    generated = [path for path in tmp_path.glob("*-daemon.log")
                 if console_capture._generated_root(path.name, "daemon")]
    assert len(generated) == 1


def test_create_does_not_replace_a_hostile_current_directory(tmp_path):
    current = tmp_path / "orb-current.log"
    current.mkdir()

    log = _create(tmp_path, component="orb")

    assert log.is_file()
    assert current.is_dir()


def test_echoechoctl_creation_uses_secure_helper_and_preserves_victim(tmp_path):
    console = tmp_path / "console"
    console.mkdir()
    victim = tmp_path / "victim.txt"
    victim.write_text("still-safe")
    (console / "daemon-current.log").symlink_to(victim)
    env = os.environ.copy()
    env["ECHOECHO_DIAGNOSTICS_DIR"] = str(tmp_path)

    result = subprocess.run(
        ["bash", "-c",
         'source "$1" version >/dev/null; new_console_log daemon',
         "echoechoctl-test", str(REPO / "scripts" / "echoechoctl.sh")],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    created = Path(result.stdout.strip())
    assert created.is_file() and not created.is_symlink()
    assert stat.S_IMODE(created.stat().st_mode) == 0o600
    assert victim.read_text() == "still-safe"
    assert (console / "daemon-current.log").resolve() == created


def test_echoechoctl_rejects_a_symlink_console_directory(tmp_path):
    diagnostics_dir = tmp_path / "diagnostics"
    diagnostics_dir.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "20200101T000000Z-1-daemon.log"
    victim.write_text("must-survive")
    (diagnostics_dir / "console").symlink_to(
        outside, target_is_directory=True)
    env = os.environ.copy()
    env["ECHOECHO_DIAGNOSTICS_DIR"] = str(diagnostics_dir)

    result = subprocess.run(
        ["bash", "-c",
         'source "$1" version >/dev/null; new_console_log daemon',
         "echoechoctl-test", str(REPO / "scripts" / "echoechoctl.sh")],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "/dev/null"
    assert "console logging disabled" in result.stderr
    assert victim.read_text() == "must-survive"
    assert list(outside.iterdir()) == [victim]


def test_echoechoctl_logs_does_not_follow_hostile_current_pointer(tmp_path):
    console = tmp_path / "console"
    console.mkdir()
    victim = tmp_path / "victim.txt"
    victim.write_text("PRIVATE-CANARY\n")
    (console / "daemon-current.log").symlink_to(victim)
    env = os.environ.copy()
    env["ECHOECHO_DIAGNOSTICS_DIR"] = str(tmp_path)

    result = subprocess.run(
        ["bash", str(REPO / "scripts" / "echoechoctl.sh"),
         "logs", "daemon", "10"],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0
    assert "PRIVATE-CANARY" not in result.stdout


def test_echoechoctl_logs_does_not_follow_unrelated_sibling_pointer(tmp_path):
    console = tmp_path / "console"
    console.mkdir()
    victim = console / "private.txt"
    victim.write_text("SIBLING-PRIVATE-CANARY\n")
    (console / "daemon-current.log").symlink_to(victim.name)
    env = os.environ.copy()
    env["ECHOECHO_DIAGNOSTICS_DIR"] = str(tmp_path)

    result = subprocess.run(
        ["bash", str(REPO / "scripts" / "echoechoctl.sh"),
         "logs", "daemon", "10"],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0
    assert "SIBLING-PRIVATE-CANARY" not in result.stdout
