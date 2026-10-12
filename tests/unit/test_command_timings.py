"""
Unit tests for the per-hop command timing lifecycle behind last_command.timings.

The cycle (see .tmp/connection-map.md):
  tools/call POST arrives      -> record_command() opens mcp_call (recv stamp)
  first WS command dispatched  -> note_command_sent() stamps ws_sent_at (ONCE)
  last WS answer arrives       -> send_request_and_wait() stamps ws_ack_at
  HTTP response fully sent     -> note_mcp_http_done() stamps http_done_at
  launcher's POST /timing      -> record_launcher_timing() merges the stdio side

last_command_info() turns those stamps into the timings dict served at /health
and inside get_connections_info() (which the options page's connection map
replays commands from). Driven directly — no Firefox, no websockets, no psutil.
"""

import asyncio
import json
from datetime import datetime

from unittest.mock import AsyncMock, patch

import pytest

from server.server import BrowserPlugServer

SID = "session-abcdef1234"     # mcp_call keeps str(sid)[:8]


def _connect_browser(server, instance_id="ff-1", pid=111):
    """Register a connected browser exactly as test_multi_instance does."""
    sock = AsyncMock()
    sock.close_code = None
    sock.remote_address = ("127.0.0.1", 40001)
    ident = {"pid": pid, "friendly": "Firefox",
             "exe_path": r"C:\Program Files\Firefox\firefox.exe"}
    with patch.object(server, "_probe_browser_identity", return_value=ident):
        browser = server.registry.register(sock)
    return server.registry.promote(browser, instance_id)


class TestCommandTimings:

    @pytest.fixture
    def server(self):
        s = BrowserPlugServer(host="localhost", port=8772, start_mcp=False)
        s.http_clients[SID] = {
            "pid": 4242, "friendly": "Claude Code", "exe_path": None,
            "cwd": r"C:\proj", "start_time": None, "launcher_pid": 999,
            "launcher_start_time": datetime.now(),
            "peer_port": 55555, "first_seen": datetime.now(),
            "last_activity": datetime.now(), "history": [],
        }
        return s

    def test_record_command_opens_the_call_record(self, server):
        server.telemetry.record_command(SID, "tabs", {"action": "list"}, req_id=1)
        mc = server.telemetry.mcp_call
        assert mc["tool"] == "tabs"
        assert mc["session"] == SID[:8]
        assert mc["client_pid"] == 4242
        assert mc["launcher_pid"] == 999
        assert mc["recv_at"] is not None
        assert mc["ws_sent_at"] is None and mc["http_done_at"] is None

    def test_only_the_first_ws_dispatch_stamps_ws_sent(self, server):
        server.telemetry.record_command(SID, "page_snapshot", {}, req_id=2)
        server.telemetry.note_command_sent({"action": "content.snapshot", "data": {}})
        first = server.telemetry.mcp_call["ws_sent_at"]
        assert first is not None
        # A multi-command tool sends again; dispatch must keep the FIRST stamp.
        server.telemetry.note_command_sent({"action": "content.snapshot", "data": {"frame": 1}})
        assert server.telemetry.mcp_call["ws_sent_at"] is first

    @pytest.mark.asyncio
    async def test_ws_roundtrip_stamps_ack_and_browser_slot(self, server):
        _connect_browser(server)
        server.telemetry.record_command(SID, "tabs", {}, req_id=3)
        request = {"id": "req-1", "type": "request", "action": "tabs.list", "data": {}}

        async def answer():
            for _ in range(100):
                await asyncio.sleep(0.005)
                fut = server.rpc.pending_requests.get("req-1")
                if fut is not None:
                    fut.set_result({"id": "req-1", "type": "response",
                                    "action": "tabs.list", "data": {"tabs": []}})
                    return

        task = asyncio.create_task(answer())
        response = await server.send_request_and_wait(request, timeout=2.0)
        await task
        assert response.get("type") == "response"

        mc = server.telemetry.mcp_call
        assert mc["ws_sent_at"] is not None
        assert mc["ws_ack_at"] is not None
        assert mc["browser_slot"] == 1

    def test_http_done_stamps_only_the_matching_session(self, server):
        server.telemetry.record_command(SID, "tabs", {}, req_id=4)
        server._note_mcp_http_done("some-other-session")
        assert server.telemetry.mcp_call["http_done_at"] is None
        server._note_mcp_http_done(SID)
        assert server.telemetry.mcp_call["http_done_at"] is not None

    def test_launcher_report_merges_and_mismatches_are_refused(self, server):
        server.telemetry.record_command(SID, "tabs", {}, req_id=5)
        recv = server.telemetry.mcp_call["recv_epoch"]

        # Wrong tool: refused.
        assert server.record_launcher_timing(
            {"tool": "navigate", "started_epoch": recv - 0.01,
             "total_ms": 500, "launcher_pid": 999}) is False
        # Outside the time window: refused.
        assert server.record_launcher_timing(
            {"tool": "tabs", "started_epoch": recv + 600,
             "total_ms": 500, "launcher_pid": 999}) is False
        # Wrong client pid: refused.
        assert server.record_launcher_timing(
            {"tool": "tabs", "started_epoch": recv - 0.01,
             "total_ms": 500, "launcher_pid": 999, "client_pid": 1}) is False
        # The real one: merged.
        assert server.record_launcher_timing(
            {"tool": "tabs", "started_epoch": recv - 0.01,
             "total_ms": 500.0, "launcher_pid": 999, "client_pid": 4242}) is True
        assert server.telemetry.mcp_call["launcher"] == {"launcher_pid": 999, "total_ms": 500.0}

    @pytest.mark.asyncio
    async def test_full_cycle_yields_the_timings_dict(self, server):
        _connect_browser(server)
        server.telemetry.record_command(SID, "tabs", {}, req_id=6)
        request = {"id": "req-2", "type": "request", "action": "tabs.list", "data": {}}

        async def answer():
            for _ in range(100):
                await asyncio.sleep(0.005)
                fut = server.rpc.pending_requests.get("req-2")
                if fut is not None:
                    fut.set_result({"id": "req-2", "type": "response",
                                    "action": "tabs.list", "data": {"tabs": []}})
                    return

        task = asyncio.create_task(answer())
        await server.send_request_and_wait(request, timeout=2.0)
        await task
        server._note_mcp_http_done(SID)
        assert server.record_launcher_timing(
            {"tool": "tabs", "started_epoch": server.telemetry.mcp_call["recv_epoch"] - 0.01,
             "total_ms": 5000.0, "launcher_pid": 999, "client_pid": 4242}) is True

        info = server.telemetry.last_command_info()
        assert info["browser_slot"] == 1
        assert info["launcher_pid"] == 999
        t = info["timings"]
        assert t["complete"] is True
        assert t["started_epoch"] == server.telemetry.mcp_call["recv_epoch"]
        for key in ("server_dispatch_ms", "browser_ms", "server_finalize_ms", "server_ms"):
            assert t[key] is not None and t[key] >= 0, key
        assert t["launcher_ms"] == 5000.0
        # proxy overhead = the launcher's total minus the server-side stay
        assert t["proxy_overhead_ms"] == round(5000.0 - t["server_ms"], 1)

        # The same object rides the connections payload for the options-page map.
        conn = server.get_connections_info()
        assert conn["last_command"]["timings"]["complete"] is True
        # ... and the client row names its launcher for the map's launcher hop,
        # with the launcher's own start time (its node reports "started" too).
        row = [c for c in conn["connections"] if c["kind"] == "mcp_client"][0]
        assert row["launcher_pid"] == 999
        assert row["launcher_since_hms"] is not None

    @pytest.mark.asyncio
    async def test_http_done_ignores_the_mcp_redirect_response(self, server):
        """The launcher POSTs tools/call to /mcp/, which 307-redirects to /mcp;
        the SAME body is then re-POSTed. The redirect's (instant) response must
        NOT stamp http_done_at — only the real 2xx completion may. This is the
        exact bug seen live: server_ms 0.0 with a negative finalize."""
        body = json.dumps({"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                           "params": {"name": "tabs", "arguments": {}}}).encode()

        def scope():
            return {"type": "http", "method": "POST", "path": "/mcp/",
                    "headers": [(b"mcp-session-id", SID.encode())],
                    "client": ("127.0.0.1", 55555)}

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(message):
            pass

        status = {"code": 307}

        async def app(_scope, _receive, _send):
            await _send({"type": "http.response.start",
                         "status": status["code"], "headers": []})
            await _send({"type": "http.response.body", "body": b"", "more_body": False})

        wrapped = server._wrap_client_tracking(app)

        await wrapped(scope(), receive, send)          # the 307 bounce
        assert server.telemetry.mcp_call["tool"] == "tabs"
        assert server.telemetry.mcp_call["http_done_at"] is None, \
            "a redirect must not end the call's server leg"

        status["code"] = 200
        await wrapped(scope(), receive, send)          # the re-POST (same req id)
        assert server.telemetry.mcp_call["http_done_at"] is not None

    def test_a_browserless_call_becomes_last_command_itself(self, server):
        """`browsers` / `server_info` never send a WS command, so the WS-level
        record can't represent them — but they DO show in the session history,
        and last_command must agree (the inconsistency reported 2026-07-15).
        The old call's timings must not stick around either."""
        server.telemetry.record_command(SID, "tabs", {}, req_id=7)
        server.telemetry.note_command_sent({"action": "tabs.list", "data": {}})
        server._note_mcp_http_done(SID)
        assert server.telemetry.last_command_info()["timings"]["complete"] is True

        server.telemetry.record_command(SID, "browsers", {"action": "list"}, req_id=8)
        server._note_mcp_http_done(SID)
        info = server.telemetry.last_command_info()
        assert info["tool"] == "browsers"
        assert info["action"] is None
        assert info["acknowledged_by_extension"] is None
        assert info["browser_slot"] is None
        t = info["timings"]
        assert t["complete"] is True
        assert t["server_dispatch_ms"] is None and t["browser_ms"] is None
        assert t["server_ms"] is not None and t["server_ms"] >= 0

        # The launcher's stdio-side report still merges for such calls.
        assert server.record_launcher_timing(
            {"tool": "browsers", "started_epoch": server.telemetry.mcp_call["recv_epoch"] - 0.01,
             "total_ms": 25.0, "launcher_pid": 999, "client_pid": 4242}) is True
        t = server.telemetry.last_command_info()["timings"]
        assert t["launcher_ms"] == 25.0
        assert t["proxy_overhead_ms"] == round(25.0 - t["server_ms"], 1)
