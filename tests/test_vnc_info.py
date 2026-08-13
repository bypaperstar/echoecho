"""Viewer /vnc-info (PR 15): env override, tier gating, and vm.vnc_url()'s
tolerant lume parse — no lume in CI, fake outputs only."""
import http.client
import json

import pytest

from echo_app.services import vm as vm_mod
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
        return resp.status, dict(resp.getheaders()), resp.read()
    finally:
        conn.close()


# real `lume get -f json` shape (0.5.3): a JSON ARRAY, log lines in front in
# practice — mirrors tests/test_vm_sandbox.py, plus the vncUrl field
LUME_GET_ARRAY = '''[
  {
    "status" : "running",
    "os" : "macOS",
    "ipAddress" : "192.168.64.3",
    "vncUrl" : "vnc://:s3cret@192.168.64.3:5900",
    "name" : "echo-vm",
    "sharedDirectories" : null
  }
]
'''


# -- the endpoint --------------------------------------------------------------

def test_env_override_returns_url(server, monkeypatch):
    monkeypatch.setenv("ECHO_VNC_URL", "vnc://:pw@10.0.0.9:5901")
    status, headers, body = get(server, "/vnc-info")
    assert status == 200
    assert json.loads(body) == {"url": "vnc://:pw@10.0.0.9:5901"}
    assert headers["Content-Type"].startswith("application/json")
    # same no-sniff discipline as every other JSON route
    assert headers["X-Content-Type-Options"] == "nosniff"


def test_503_when_vm_tier_not_configured(server, monkeypatch):
    monkeypatch.delenv("ECHO_VNC_URL", raising=False)
    monkeypatch.delenv("ECHO_SANDBOX", raising=False)  # default tier: shell
    status, headers, body = get(server, "/vnc-info")
    assert status == 503
    err = json.loads(body)["error"]
    assert "ECHO_SANDBOX" in err and "ECHO_VNC_URL" in err
    assert headers["X-Content-Type-Options"] == "nosniff"


def test_503_with_reason_when_lume_fails(server, monkeypatch):
    monkeypatch.delenv("ECHO_VNC_URL", raising=False)
    monkeypatch.setenv("ECHO_SANDBOX", "vm")
    monkeypatch.setattr(vm_mod, "_lume_get_sync",
                        lambda name: (1, "VM not found"))
    status, _, body = get(server, "/vnc-info")
    assert status == 503
    assert "VM not found" in json.loads(body)["error"]


def test_env_override_wins_even_on_vm_tier(server, monkeypatch):
    monkeypatch.setenv("ECHO_SANDBOX", "vm")
    monkeypatch.setenv("ECHO_VNC_URL", "vnc://:pw@127.0.0.1:5907")

    def boom(name):  # lume must not even be consulted
        raise AssertionError("lume queried despite ECHO_VNC_URL")
    monkeypatch.setattr(vm_mod, "_lume_get_sync", boom)
    status, _, body = get(server, "/vnc-info")
    assert status == 200
    assert json.loads(body)["url"] == "vnc://:pw@127.0.0.1:5907"


# -- vm.vnc_url(): fake-lume-output parsing ------------------------------------

def test_vnc_url_parses_array_output(monkeypatch):
    monkeypatch.setattr(vm_mod, "_lume_get_sync",
                        lambda name: (0, LUME_GET_ARRAY))
    assert vm_mod.vnc_url("echo-vm") == "vnc://:s3cret@192.168.64.3:5900"


def test_vnc_url_parses_log_prefixed_output(monkeypatch):
    out = "[2026-08-13T02:00:00Z] INFO: fetching\n" + LUME_GET_ARRAY
    monkeypatch.setattr(vm_mod, "_lume_get_sync", lambda name: (0, out))
    assert vm_mod.vnc_url("echo-vm") == "vnc://:s3cret@192.168.64.3:5900"


def test_vnc_url_defaults_to_configured_vm_name(monkeypatch):
    monkeypatch.setenv("ECHO_VM_NAME", "my-vm")
    seen = {}

    def fake(name):
        seen["name"] = name
        return 0, LUME_GET_ARRAY
    monkeypatch.setattr(vm_mod, "_lume_get_sync", fake)
    vm_mod.vnc_url()
    assert seen["name"] == "my-vm"


def test_vnc_url_failures_raise_human_readable(monkeypatch):
    # nonzero rc (unknown VM)
    monkeypatch.setattr(vm_mod, "_lume_get_sync",
                        lambda name: (1, "VM echo-vm not found"))
    with pytest.raises(vm_mod.SandboxUnavailable) as ei:
        vm_mod.vnc_url("echo-vm")
    assert "not found" in str(ei.value)

    # VM exists but is stopped: never hand out a dead endpoint
    stopped = LUME_GET_ARRAY.replace('"running"', '"stopped"')
    monkeypatch.setattr(vm_mod, "_lume_get_sync", lambda name: (0, stopped))
    with pytest.raises(vm_mod.SandboxUnavailable) as ei:
        vm_mod.vnc_url("echo-vm")
    assert "not running" in str(ei.value)

    # running but no vncUrl field
    no_url = LUME_GET_ARRAY.replace(
        '"vncUrl" : "vnc://:s3cret@192.168.64.3:5900",\n    ', "")
    monkeypatch.setattr(vm_mod, "_lume_get_sync", lambda name: (0, no_url))
    with pytest.raises(vm_mod.SandboxUnavailable) as ei:
        vm_mod.vnc_url("echo-vm")
    assert "vncUrl" in str(ei.value)

    # garbage output -> a clean error, never a crash
    monkeypatch.setattr(vm_mod, "_lume_get_sync",
                        lambda name: (0, "not json at all"))
    with pytest.raises(vm_mod.SandboxUnavailable):
        vm_mod.vnc_url("echo-vm")
