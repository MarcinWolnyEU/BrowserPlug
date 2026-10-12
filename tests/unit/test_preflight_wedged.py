"""Preflight on a wedged server, and the client registries it reads (review L15).

A wedged server holds its ports and never answers /health. The doctor used to call that
"no BrowserPlug server is running" and recommend a start that dies on the instance lock.
The fake listener below accepts connections and never writes a byte, which is what a
server stuck in its event loop looks like from outside.
"""
import json
import os
import socket

import pytest

import test_imports  # noqa: F401  (path setup)
from server import preflight
from server.reporting import reports
from browserplug_shared import mcp_port


@pytest.fixture
def silent_listener():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(8)
    yield sock
    sock.close()


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(autouse=True)
def _private_ws_port(monkeypatch):
    """The live server on this box holds 8772; keep the doctor's WS check off it."""
    monkeypatch.setattr(preflight, "WS_PORT", _free_port(), raising=False)


def _by_name(checks):
    return {c["name"]: c for c in checks}


def test_a_pinned_port_held_by_a_silent_listener_is_a_wedged_server(silent_listener, tmp_path):
    port = silent_listener.getsockname()[1]
    checks = _by_name(preflight.run_checks(
        str(tmp_path), env={mcp_port.MCP_PORT_ENV: str(port)}))
    for name in ("mcp_port", "server_process"):
        check = checks[name]
        assert check["ok"] is False
        assert "no BrowserPlug server is running" not in check["detail"]
        assert "not answering /health" in check["detail"]
        assert f"pid {os.getpid()}" in check["detail"]


def test_the_websocket_port_held_by_someone_else_is_named(silent_listener, tmp_path, monkeypatch):
    ws_port = silent_listener.getsockname()[1]
    monkeypatch.setattr(preflight, "WS_PORT", ws_port, raising=False)
    checks = _by_name(preflight.run_checks(
        str(tmp_path), env={mcp_port.MCP_PORT_ENV: str(_free_port())}))
    detail = checks["server_process"]["detail"]
    assert "not answering /health" in detail and f"pid {os.getpid()}" in detail
    assert str(ws_port) in detail


def test_a_wedged_server_is_not_told_to_start_another(silent_listener, tmp_path):
    port = silent_listener.getsockname()[1]
    check = _by_name(preflight.run_checks(
        str(tmp_path), env={mcp_port.MCP_PORT_ENV: str(port)}))["mcp_port"]
    assert "server/server.py" not in check["remedy"]
    assert str(os.getpid()) in check["remedy"]


def test_a_genuinely_empty_machine_still_says_nothing_is_running(tmp_path):
    check = preflight.check_mcp_port(
        {"probed": True, "pinned": None, "port": None, "health": None, "error": None,
         "holders": []})
    assert "no BrowserPlug server is running" in check["detail"]


def test_the_start_remedy_is_detached_never_a_foreground_run():
    remedy = preflight._START_SERVER
    assert "detached" in remedy
    assert "foreground" in remedy  # named only to say not to
    assert "python server/server.py" not in remedy.replace("{python} server/server.py", "")


# --- client registries: project scope and .mcp.json ---------------------------------

def _entry(tmp_path):
    return {"command": str(tmp_path / "python.exe"),
            "args": [str(tmp_path / "server" / "launcher.py")]}


def test_claude_code_project_scoped_servers_are_read(tmp_path):
    (tmp_path / "python.exe").write_text("")
    (tmp_path / "server").mkdir()
    (tmp_path / "server" / "launcher.py").write_text("")
    (tmp_path / ".claude.json").write_text(json.dumps({
        "mcpServers": {},
        "projects": {"C:/some/project": {"mcpServers": {"browserplug": _entry(tmp_path)}}},
    }))
    records = preflight.collect_client_configs(
        str(tmp_path), env={"USERPROFILE": str(tmp_path), "APPDATA": str(tmp_path / "x")})
    claude = [r for r in records if r["client"].startswith("Claude Code")]
    assert claude and any(r["declares_browserplug"] for r in claude)


def test_a_project_mcp_json_is_read(tmp_path):
    (tmp_path / ".mcp.json").write_text(json.dumps(
        {"mcpServers": {"browserplug": _entry(tmp_path)}}))
    records = preflight.collect_client_configs(
        str(tmp_path), env={"USERPROFILE": str(tmp_path / "h"), "APPDATA": str(tmp_path / "x")})
    assert any(r["path"].endswith(".mcp.json") and r["declares_browserplug"] for r in records)


# --- summarize_connections lives with server_info's reporting -----------------------

def test_summarize_connections_moved_to_reports():
    assert callable(reports.summarize_connections)
    assert not hasattr(preflight, "summarize_connections")
    assert "error" in reports.summarize_connections(None)
