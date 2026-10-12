"""Byte-level pins on the two reports: GET /health and get_connections_info().

The reports are assembled from a lot of server state (the browser registry, the MCP client
table, the call-timing records, the launcher versions) and consumed by the popup, the
options page's connection map, the launcher and preflight. test_connections_contract checks
their SHAPE; this file checks their VALUES, from a fully faked world with a frozen clock,
against JSON files under tests/unit/golden/.

It exists so a refactor that moves the builders (or the state they read) cannot change a
single byte of either report without a test saying so. If a report changes ON PURPOSE,
regenerate the files with `BROWSERPLUG_UPDATE_GOLDEN=1` and review the diff by eye.

Time is frozen by swapping the `datetime` class in every loaded `server.*` module, so the
test does not care which module reads the clock.
"""

import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from server import server as server_module
from browserplug_shared.version import __version__ as BROWSERPLUG_VERSION
from server.browsers import browser_identity, routing
from server.reporting import client_identity
from server.server import BrowserPlugServer

GOLDEN_DIR = Path(__file__).resolve().parent / "golden"
UPDATE = bool(os.environ.get("BROWSERPLUG_UPDATE_GOLDEN"))

NOW = datetime(2026, 9, 29, 12, 0, 0)
FAKE_PID = 4242


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW


class FakeWS:
    """A live socket: ExtBrowser.connected is `close_code is None`."""

    def __init__(self, peer=("127.0.0.1", 5555)):
        self.remote_address = peer
        self.close_code = None


def _browser(slot, instance_id, *, remote=False, **fields):
    b = browser_identity.ExtBrowser(
        FakeWS(), instance_id, slot,
        remote_addr="192.168.0.7" if remote else "127.0.0.1", is_remote=remote)
    b.provisional = False
    for k, v in fields.items():
        setattr(b, k, v)
    return b


def _set_calls(server, last_command, mcp_call):
    server.telemetry.last_command = last_command
    server.telemetry.mcp_call = mcp_call


def _browser_call(server):
    """State after a tools/call that DID drive a browser."""
    t0 = NOW - timedelta(seconds=5)
    _set_calls(
        server,
        {"tool": "tabs", "action": "tabs.list", "params": {"action": "list"},
         "at": t0, "client_pid": 777, "session": "abc12345",
         "acknowledged": True, "acknowledged_at": t0 + timedelta(milliseconds=85)},
        {"tool": "tabs", "params": {"action": "list"}, "session": "abc12345",
         "client_pid": 777, "launcher_pid": 888,
         "recv_at": t0, "recv_epoch": t0.timestamp(),
         "ws_sent_at": t0 + timedelta(milliseconds=3),
         "ws_ack_at": t0 + timedelta(milliseconds=88),
         "http_done_at": t0 + timedelta(milliseconds=90),
         "browser_slot": 1,
         "launcher": {"launcher_pid": 888, "total_ms": 132.4},
         "outcome": {"code": "ok", "retry_safe": True}})


def _server_side_call(server):
    """A tools/call that never touched a browser (browsers, server_info)."""
    t0 = NOW - timedelta(seconds=2)
    _set_calls(
        server, None,
        {"tool": "browsers", "params": {"action": "list"}, "session": "abc12345",
         "client_pid": 777, "launcher_pid": 888,
         "recv_at": t0, "recv_epoch": t0.timestamp(),
         "ws_sent_at": None, "ws_ack_at": None,
         "http_done_at": t0 + timedelta(milliseconds=2, microseconds=500),
         "browser_slot": None,
         "launcher": {"launcher_pid": 888, "total_ms": 9.0}})


def _command_only(server):
    """A WS command whose MCP call record is gone: last_command stands alone."""
    t0 = NOW - timedelta(seconds=9)
    _set_calls(
        server,
        {"tool": "page_read", "action": "page.read", "params": {"tab_id": 3},
         "at": t0, "client_pid": 777, "session": "zzzzzzzz",
         "acknowledged": False, "acknowledged_at": None},
        None)


@pytest.fixture
def world(monkeypatch):
    """A server whose whole connected world is faked, with the clock and every psutil
    read pinned. Yields the server."""
    for mod in [m for name, m in list(sys.modules.items())
                if name.startswith("server") and m is not None]:
        if getattr(mod, "datetime", None) is datetime:
            monkeypatch.setattr(mod, "datetime", _FrozenDatetime, raising=False)
    monkeypatch.setattr(os, "getpid", lambda: FAKE_PID)

    srv = BrowserPlugServer(host="localhost", port=8772, start_mcp=False)
    srv.port = 8772
    srv.mcp_port = 2830
    srv.ws_host_source = "default"
    srv.ws_token_source = "file"
    srv.server_start_time = NOW - timedelta(hours=1, minutes=2, seconds=3)
    srv.extension_last_activity = NOW - timedelta(seconds=30)
    srv.mcp_tools.tools_signature = "sig-1"

    srv.browsers = {
        "ff-local": _browser(
            1, "ff-local",
            version="7.7.4", browser_pid=111, engine="gecko",
            exe_path=r"C:\Program Files\Firefox Developer Edition\firefox.exe",
            proc_started=NOW - timedelta(hours=3),
            brand="Firefox", friendly="Firefox Developer Edition",
            connected_at=NOW - timedelta(minutes=50),
            last_action="tabs.list", last_activity=NOW - timedelta(seconds=4),
            last_seen=NOW - timedelta(seconds=1),
            file_access={"can_read_files": True, "browser_major": 152},
            proxy_access={"has_proxy_permission": True},
            api_support={"documentId": True},
            platform={"os": "win", "arch": "x86-64"},
            browser_info={"name": "Firefox", "version": "152.0", "vendor": "Mozilla"},
            browser_state={"windows": 2, "tabs": 14, "active_tabs": 2, "loaded_tabs": 9,
                           "active_tab_list": [{"id": 3, "title": "t", "last_hms": "11:59:00"}]},
            browser_state_at=NOW - timedelta(seconds=20),
            connect_count=2, encrypted=True, hello_latency_ms=12.5,
            last_disconnect_at=NOW - timedelta(minutes=55),
            last_close_code=1006, last_close_reason="abnormal",
            reported_journal=["connected", "hello"]),
        "ff-remote": _browser(
            2, "ff-remote", remote=True, connect_count=3, version="7.7.3",
            last_seen=NOW - timedelta(seconds=7),
            platform={"os": "android", "arch": "arm64"},
            browser_info={"name": "Firefox Nightly", "version": "153.0"}),
    }

    srv.http_clients = {
        "abc12345-session": {
            "pid": 777, "friendly": "Claude Code",
            "cwd": r"C:\dr\browserplug", "exe_path": r"C:\claude\claude.exe",
            "cmdline": "claude --api-key=*** --resume",
            "start_time": NOW - timedelta(minutes=30),
            "launcher_pid": 888,
            "launcher_start_time": NOW - timedelta(minutes=30, seconds=2),
            "last_activity": NOW - timedelta(seconds=5),
            "history": [
                {"time": NOW - timedelta(seconds=60), "tool": "navigate",
                 "args": {"url": "https://example.org/" + "x" * 250, "tab_id": 3,
                          "background": True, "note": None}},
                {"time": NOW - timedelta(seconds=5), "tool": "tabs",
                 "args": {"action": "list"}},
            ]},
        # Same process, second session: collapsed into ONE row.
        "def67890-session": {
            "pid": 777, "friendly": None, "cwd": None, "exe_path": None,
            "start_time": None, "launcher_pid": None, "launcher_start_time": None,
            "last_activity": NOW - timedelta(seconds=1), "history": []},
        # A direct HTTP client (no pid): keyed by session id, no launcher.
        "ghi11111-session": {
            "pid": None, "friendly": "GitLab Duo CLI",
            "cwd": "/work", "exe_path": "/x/glab-cli/bin/duo.exe",
            "start_time": NOW - timedelta(days=1),
            "last_activity": NOW - timedelta(seconds=100),
            "history": [{"time": NOW - timedelta(seconds=100), "tool": "server_info",
                         "args": None}]},
        # Idle past five minutes: dropped from the report (and from the table).
        "old22222-session": {
            "pid": 999, "friendly": "Node", "last_activity": NOW - timedelta(seconds=301),
            "history": []},
    }
    srv.launcher_versions = {888: {"version": "7.7.4", "seen": NOW - timedelta(seconds=10)}}
    # A pid key carries the process start time (routing.pid_client_key), so a reused pid
    # cannot inherit this client's selection.
    srv.browser_selection = {routing.pid_client_key(777, NOW - timedelta(minutes=30)):
                             "ff-remote"}

    describe = {"pid": FAKE_PID, "friendly": "python", "exe_name": "python.exe",
                "exe_path": r"C:\dr\browserplug\.venv\Scripts\python.exe",
                "cwd": r"C:\dr\browserplug", "start_time": NOW - timedelta(hours=1)}
    with patch.object(client_identity, "describe_pid", return_value=describe), \
            patch.object(client_identity, "connected_browser_pids", return_value=[111]), \
            patch.object(server_module, "lan_addresses", return_value=["192.168.0.5"]), \
            patch.object(server_module, "ws_lan_addresses", return_value=[
                {"ip": "192.168.0.5", "interface": "Ethernet", "recommended": True}]):
        yield srv


def _canon(obj):
    """Stable text for a report. The server's own version is masked so a routine version
    bump does not rewrite the golden files (the fake parties use 7.7.x on purpose)."""
    text = json.dumps(obj, indent=2, sort_keys=False, default=repr, ensure_ascii=False) + "\n"
    return text.replace(BROWSERPLUG_VERSION, "<SERVER_VERSION>")


def _check(name, obj):
    path = GOLDEN_DIR / f"{name}.json"
    text = _canon(obj)
    if UPDATE:
        GOLDEN_DIR.mkdir(exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    assert path.exists(), f"missing golden {path}; run with BROWSERPLUG_UPDATE_GOLDEN=1"
    assert text == path.read_text(encoding="utf-8"), f"{name} drifted from its golden file"


def test_health_after_a_browser_call(world):
    _browser_call(world)
    _check("health_browser_call", world.get_health_info())


def test_health_after_a_server_side_call(world):
    _server_side_call(world)
    _check("health_server_side_call", world.get_health_info())


def test_connections_after_a_browser_call(world):
    _browser_call(world)
    _check("connections_browser_call",
           world.get_connections_info(kilo_details=False, viewer_host="192.168.0.114"))
    assert "old22222-session" not in world.http_clients, "an idle client is pruned"


def test_connections_after_a_server_side_call(world):
    _server_side_call(world)
    _check("connections_server_side_call", world.get_connections_info())


def test_connections_with_a_stranded_command(world):
    _command_only(world)
    _check("connections_command_only", world.get_connections_info(kilo_details=False))


def test_connections_with_no_calls_and_a_hostile_viewer_host(world):
    _set_calls(world, None, None)
    _check("connections_no_calls",
           world.get_connections_info(viewer_host="evil.example/<script>"))
