"""Keep the live desktop proof honest: no unlock, no shell-written marker."""
import asyncio
import importlib.util
import json
import plistlib
from pathlib import Path
from types import SimpleNamespace

import pytest


SPEC = importlib.util.spec_from_file_location(
    "vm_unattended_proof", Path(__file__).parents[1] / "scripts/vm_unattended_proof.py")
proof = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(proof)


@pytest.mark.parametrize("as_list", [False, True])
def test_console_probe_handles_both_registry_shapes(monkeypatch, as_list):
    registry = {"IOConsoleUsers": [{"kCGSSessionOnConsoleKey": True,
                                   "CGSSessionScreenIsLocked": True}]}
    if as_list:
        registry = [registry]
    responses = iter([SimpleNamespace(returncode=0, stdout=b"lume\n"),
                      SimpleNamespace(returncode=0, stdout=plistlib.dumps(registry))])
    monkeypatch.setattr(proof, "guest_command", lambda *_a: next(responses))
    assert proof.console_state(object()) == {
        "user": "lume", "logged_in": True, "locked": True}


@pytest.mark.parametrize("session_flag", [None, False])
def test_console_probe_reads_root_lock_flag(monkeypatch, session_flag):
    session = {"kCGSSessionOnConsoleKey": True}
    if session_flag is not None:
        session["CGSSessionScreenIsLocked"] = session_flag
    registry = {"IOConsoleLocked": True, "IOConsoleUsers": [session]}
    responses = iter([SimpleNamespace(returncode=0, stdout=b"lume\n"),
                      SimpleNamespace(returncode=0, stdout=plistlib.dumps(registry))])
    monkeypatch.setattr(proof, "guest_command", lambda *_a: next(responses))
    assert proof.console_state(object())["locked"] is True


def rig(monkeypatch, tmp_path, console, screenshot_failure=False):
    actions = []
    data = {"typed": "", "saved": b""}

    class VM:
        def __init__(self, **kwargs):
            self.vm_name = kwargs["vm_name"]

        async def _get(self):
            return {"status": "stopped"}

        def _is_running(self, info):
            return info["status"] == "running"

        async def _lume(self, *args):
            actions.append(args)
            return 0, ""

        async def prepare(self):
            pass

    class Client:
        width, height = 1920, 1440
        def __init__(self, *_a, **_k):
            pass

        def connect(self):
            return self

        def capture_png(self, path, **_kwargs):
            if screenshot_failure and path.name == "02-textedit-saved.png":
                raise TimeoutError("no final screenshot")
            path.write_bytes(b"screenshot")

        def type_text(self, text):
            actions.append(("type", text))
            data["typed"] = text

        def click(self, x, y):
            actions.append(("click", x, y))

        def close(self):
            pass

    def command(_vm, command):
        actions.append(("ssh", command))
        return SimpleNamespace(returncode=0,
                               stdout=data["saved"] if command.startswith("cat ") else b"")

    def key(_client, combo):
        actions.append(("key", combo))
        if combo == "cmd+s":
            data["saved"] = data["typed"].encode()

    async def no_sleep(_seconds):
        pass

    monkeypatch.setattr(proof, "LumeVM", VM)
    monkeypatch.setattr(proof, "console_state", lambda _vm: console)
    monkeypatch.setattr(proof, "guest_command", command)
    monkeypatch.setattr(proof, "vnc_url", lambda _name: "vnc://localhost:5900")
    monkeypatch.setattr(proof.vnc_mod, "VncClient", Client)
    monkeypatch.setattr(proof, "send_key", key)
    monkeypatch.setattr(proof.asyncio, "sleep", no_sleep)
    args = SimpleNamespace(vm="isolated-proof-vm", golden="golden",
                           workspace=tmp_path / "workspace", output=tmp_path / "results",
                           phase="before", stop_first=True, desktop_timeout=0,
                           screensaver=False)
    return args, actions


@pytest.mark.parametrize("console", [
    {"user": "root", "logged_in": False, "locked": None},
    {"user": "lume", "logged_in": True, "locked": True},
])
def test_login_or_lock_screen_fails_without_typing(monkeypatch, tmp_path, console):
    args, actions = rig(monkeypatch, tmp_path, console)
    assert asyncio.run(proof.run(args)) == 1
    result = json.loads((args.output / "result.json").read_text())
    assert result["passed"] is False
    assert result["login_password_entered"] is False
    assert result["error"] == "desktop requires login or unlock"
    assert not any(action[0] in ("type", "stop") for action in actions)
    assert all(action == ("key", "return")
               for action in actions if action[0] == "key")


def test_success_requires_gui_typing_save_and_file_readback(monkeypatch, tmp_path):
    args, actions = rig(monkeypatch, tmp_path,
                        {"user": "lume", "logged_in": True, "locked": False})
    assert asyncio.run(proof.run(args)) == 0
    result = json.loads((args.output / "result.json").read_text())
    assert result["saved_text"] == result["marker"] + "\n"
    assert ("type", result["saved_text"]) in actions
    assert ("key", "cmd+s") in actions
    shell_commands = [action[1] for action in actions if action[0] == "ssh"]
    assert all(result["marker"] not in command for command in shell_commands)
    assert any(command.startswith("cat ") for command in shell_commands)
    assert Path(result["saved_screenshot"]).exists()


def test_missing_final_screenshot_cannot_report_a_pass(monkeypatch, tmp_path):
    args, _ = rig(monkeypatch, tmp_path,
                  {"user": "lume", "logged_in": True, "locked": False},
                  screenshot_failure=True)
    assert asyncio.run(proof.run(args)) == 1
    assert json.loads((args.output / "result.json").read_text())["passed"] is False
