"""Headless checks for the Mac-only VM note proof's diagnostic boundaries."""
import importlib.util
from pathlib import Path
import types

import pytest


REPO = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "echoecho_vm_note_demo", REPO / "scripts" / "vm_note_demo.py")
demo = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(demo)


class FakeVM:
    vm_name = "test-vm"

    def __init__(self):
        self.commands = []

    def ssh_argv(self, command):
        self.commands.append(command)
        return ["ssh", "test-vm", command]


def test_ssh_diagnostics_record_only_operation_and_sizes(monkeypatch):
    records = []
    monkeypatch.setattr(
        demo.diagnostics, "info",
        lambda event, **fields: records.append((event, fields)))
    monkeypatch.setattr(
        demo.subprocess, "run",
        lambda *_args, **_kwargs: types.SimpleNamespace(
            returncode=0, stdout="PRIVATE-OUTPUT-CANARY", stderr=""))
    vm = FakeVM()
    guest = demo.Guest(vm)
    private_command = "printf PRIVATE-COMMAND-CANARY"

    result = guest.ssh(private_command, operation="note_write")

    assert result.returncode == 0
    assert vm.commands == [private_command]
    rendered = repr(records)
    assert "PRIVATE-COMMAND-CANARY" not in rendered
    assert "PRIVATE-OUTPUT-CANARY" not in rendered
    event, fields = records[-1]
    assert event == "demo.ssh.finished"
    assert fields["operation"] == "note_write"
    assert fields["stdout_chars"] == len("PRIVATE-OUTPUT-CANARY")


def test_require_ssh_turns_unchecked_failure_into_bounded_error(monkeypatch):
    monkeypatch.setattr(
        demo.subprocess, "run",
        lambda *_args, **_kwargs: types.SimpleNamespace(
            returncode=9, stdout="", stderr="PRIVATE-ERROR-CANARY"))
    guest = demo.Guest(FakeVM())

    with pytest.raises(RuntimeError, match="note_open") as caught:
        guest.require_ssh("open private-file", "note_open")

    assert "PRIVATE-ERROR-CANARY" not in str(caught.value)


def test_ssh_exception_never_echoes_remote_command(monkeypatch):
    private_command = "printf PRIVATE-COMMAND-CANARY"

    def fail(argv, **_kwargs):
        raise demo.subprocess.TimeoutExpired(argv, 1)

    monkeypatch.setattr(demo.subprocess, "run", fail)
    guest = demo.Guest(FakeVM())

    with pytest.raises(RuntimeError, match="TimeoutExpired") as caught:
        guest.ssh(private_command, operation="note_write")

    assert "PRIVATE-COMMAND-CANARY" not in str(caught.value)


def test_screenshot_console_never_echoes_guest_stderr(
        monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(demo, "WS", tmp_path)
    guest = demo.Guest(FakeVM())
    monkeypatch.setattr(
        guest, "ssh",
        lambda *_args, **_kwargs: types.SimpleNamespace(
            returncode=7, stdout="", stderr="PRIVATE-STDERR-CANARY"))

    assert guest.shot("failed.png") is False

    output = capsys.readouterr()
    assert "PRIVATE-STDERR-CANARY" not in output.out + output.err


def test_entrypoint_replaces_raw_traceback_with_safe_pointer(
        monkeypatch, capsys):
    monkeypatch.setattr(demo.diagnostics, "configure", lambda *_a, **_k: None)
    monkeypatch.setattr(demo.diagnostics, "exception", lambda *_a, **_k: None)
    monkeypatch.setattr(demo.diagnostics, "shutdown", lambda *_a, **_k: None)
    monkeypatch.setattr(demo, "main", lambda: object())

    def fail(_awaitable):
        raise RuntimeError("PRIVATE-TRACEBACK-CANARY")

    monkeypatch.setattr(demo.asyncio, "run", fail)

    with pytest.raises(SystemExit) as caught:
        demo._entrypoint()

    assert caught.value.code == 1
    output = capsys.readouterr()
    assert "PRIVATE-TRACEBACK-CANARY" not in output.out + output.err
    assert "inspect structured diagnostics" in output.out


def test_vnc_setup_does_not_emit_endpoint_or_password(monkeypatch):
    records = []
    secret = "DEMO-VNC-PASSWORD-CANARY"
    endpoint = "vnc://:%s@127.0.0.1:5900" % secret
    monkeypatch.setattr(demo.config, "vnc_url_override", lambda: endpoint)
    monkeypatch.setattr(
        demo.diagnostics, "info",
        lambda event, **fields: records.append((event, fields)))

    class FakeSpan:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(demo.diagnostics, "span", lambda *_a, **_k: FakeSpan())

    class FakeClient:
        width = 800
        height = 600

        def __init__(self, host, port, password, timeout):
            assert (host, port, password, timeout) == (
                "127.0.0.1", 5900, secret, 25)

        def connect(self):
            return self

    monkeypatch.setattr(demo.vnc_mod, "VncClient", FakeClient)

    client = demo.Guest(FakeVM()).vnc()

    assert (client.width, client.height) == (800, 600)
    rendered = repr(records)
    assert endpoint not in rendered
    assert secret not in rendered
    assert records[-1][1]["password_present"] is True
