"""Guest setup verifies policy and never puts passwords in SSH commands."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


SPEC = importlib.util.spec_from_file_location(
    "vm_unattended", Path(__file__).parents[1] / "scripts/vm_unattended.py")
setup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(setup)


def configure_responses(monkeypatch, lock="off", sleep="0", boot_lock="0"):
    calls = []
    encoded = []

    def guest(_vm, command, operation, input_text=None):
        calls.append((command, operation, input_text))
        stdout, stderr = "", ""
        if operation == "set unlocked automatic login":
            encoded.append(input_text.splitlines()[1])
        if operation == "verify automatic login credential":
            stdout = encoded[0] + "\n"
        if operation == "verify automatic login":
            stdout = "lume\n"
        if operation == "verify unlocked automatic login":
            stdout = boot_lock + "\n"
        if "-screenLock status" in command:
            stderr = "screenLock delay is " + lock
        if "pmset -g custom" in command:
            stdout = "AC Power:\n sleep %s\n displaysleep 0\n disksleep 0\n" % sleep
        return SimpleNamespace(returncode=0, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(setup, "guest", guest)
    return calls


def test_setup_verifies_native_lock_and_sleep_settings(monkeypatch):
    calls = configure_responses(monkeypatch)
    result = setup.configure(object(), "lume", "PRIVATE-PASSWORD-CANARY")
    assert result["screen_lock"] == "off"
    assert result["automatic_login_user"] == "lume"
    assert all("PRIVATE-PASSWORD-CANARY" not in command for command, _, _ in calls)
    assert any("-screenLock off -password -" in command and
               input_text == "PRIVATE-PASSWORD-CANARY\n"
               for command, _, input_text in calls)
    assert any("sudo -k -S" in command for command, _, _ in calls)


@pytest.mark.parametrize("password", ["lume", "abcdefghijkl", "café"])
def test_autologin_credential_terminates_and_pads_before_xor(monkeypatch, password):
    calls = configure_responses(monkeypatch)
    setup.configure(object(), "lume", password)
    command, _, credentials = next(
        call for call in calls if call[1] == "set unlocked automatic login")
    assert "sudo -k -S" in command
    assert "autoLoginUserScreenLocked -bool false" in command
    encoded = bytes.fromhex(credentials.splitlines()[1])
    key = bytes.fromhex("7d895223d2bcddeaa3b91f")
    decoded = bytes(value ^ key[index % len(key)]
                    for index, value in enumerate(encoded))
    assert len(encoded) % 12 == 0
    assert decoded.startswith(password.encode() + b"\0")
    assert decoded[len(password.encode()):].strip(b"\0") == b""
    if password == "lume":
        assert encoded.hex() == "11fc3f46d2bcddeaa3b91f7d"


def test_setup_rejects_automatic_login_with_locked_desktop(monkeypatch):
    configure_responses(monkeypatch, boot_lock="1")
    with pytest.raises(RuntimeError, match="desktop locked"):
        setup.configure(object(), "lume", "PRIVATE-PASSWORD-CANARY")


@pytest.mark.parametrize("lock,sleep,message", [
    ("immediate", "0", "still requires a password"),
    ("off", "1", "sleep timer was not disabled"),
])
def test_setup_rejects_settings_that_did_not_change(monkeypatch, lock, sleep, message):
    configure_responses(monkeypatch, lock=lock, sleep=sleep)
    with pytest.raises(RuntimeError, match=message):
        setup.configure(object(), "lume", "PRIVATE-PASSWORD-CANARY")
