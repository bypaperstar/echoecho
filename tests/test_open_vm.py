import asyncio
from types import SimpleNamespace

import pytest

from scripts import open_vm as opener


def test_open_attaches_to_worker_vm_after_preparing_its_workspace(monkeypatch, tmp_path):
    events = []
    monkeypatch.setenv("ECHOECHO_VM_NAME", "our-shared-mac")
    monkeypatch.setattr(opener.config, "WORKSPACE_DIR", tmp_path)
    monkeypatch.setattr(opener.config, "load_env_local", lambda: None)
    monkeypatch.setattr(opener.shutil, "which", lambda name: "/bin/lume")

    class VM:
        def __init__(self, workspace):
            assert workspace == tmp_path
            self.vm_name = opener.config.vm_name()

        async def prepare(self):
            events.append("prepared")

    def run(argv, **kwargs):
        events.append(argv)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(opener, "NativeDisplayVM", VM)
    monkeypatch.setattr(opener.subprocess, "run", run)
    asyncio.run(opener.open_vm())
    assert events == [
        ["/bin/lume", "attach", "--help"],
        "prepared",
        ["/bin/lume", "attach", "our-shared-mac", "--display", "native"],
    ]


def test_old_lume_cannot_start_a_guest_it_cannot_show(monkeypatch):
    monkeypatch.setattr(opener.config, "load_env_local", lambda: None)
    monkeypatch.setattr(opener.shutil, "which", lambda name: "/bin/lume")
    monkeypatch.setattr(opener.subprocess, "run",
                        lambda *args, **kwargs: SimpleNamespace(returncode=1))
    monkeypatch.setattr(opener, "NativeDisplayVM",
                        lambda **kwargs: pytest.fail("must not prepare a guest"))
    with pytest.raises(RuntimeError, match="Lume 0.6"):
        asyncio.run(opener.open_vm())


def test_missing_lume_reports_installation_requirement(monkeypatch):
    monkeypatch.setattr(opener.config, "load_env_local", lambda: None)
    monkeypatch.setattr(opener.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="not installed"):
        asyncio.run(opener.open_vm())


def test_viewer_helper_leaves_a_detached_vm_running(monkeypatch, tmp_path):
    vm = opener.NativeDisplayVM(vm_name="our-shared-mac", workspace=tmp_path)
    calls = []

    async def lume(*argv):
        calls.append(argv)
        return 0, "started"

    monkeypatch.setattr(vm, "_lume", lume)
    asyncio.run(vm._boot())
    assert calls == [(
        "run", "our-shared-mac", "--no-display", "--shared-dir", str(tmp_path) + ":rw",
        "--detach", "--log-file", str(vm._boot_log_path()),
    )]
    assert vm._runner is None


def test_failed_detached_boot_is_reported(monkeypatch):
    vm = opener.NativeDisplayVM()

    async def lume(*argv):
        return 1, "failed"

    monkeypatch.setattr(vm, "_lume", lume)
    with pytest.raises(opener.SandboxUnavailable, match="could not start"):
        asyncio.run(vm._boot())
