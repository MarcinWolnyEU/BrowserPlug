"""The serving loops must not run psutil scans, and one report costs one scan.

Findings C7 (server/launcher review, 2026-09-29): every launcher polls /health, and each
poll used to run the full report inline on the MCP loop, including one TCP-table scan plus
one more per connected browser. A WebSocket connect ran a scan and a process walk on the
WebSocket loop, and get_connections built its report there too. The fakes below sleep
inside psutil.net_connections, so a scan that runs on the loop shows up as a stalled
ticker, and one that runs too often shows up in the scan counter.
"""

import asyncio
import collections
import dataclasses
import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from server.web import http_endpoints
from server.reporting import probes, reports
from server.server import BrowserPlugServer
from browserplug_shared.version import HEALTH_IDENTITY_HEADER

SCAN_SECONDS = 0.3
Conn = collections.namedtuple("Conn", "laddr raddr pid status")
Addr = collections.namedtuple("Addr", "ip port")


class FakeTable:
    """psutil.net_connections stand-in: sleeps like a big table, counts its calls, and
    lists two ESTABLISHED browser sockets on the WebSocket port (B = 2)."""

    def __init__(self, ws_port, delay=SCAN_SECONDS):
        self.calls = 0
        self.delay = delay
        self.threads = set()
        rows = []
        for i, client_port in enumerate((50001, 50002)):
            rows.append(Conn(Addr("127.0.0.1", ws_port), Addr("127.0.0.1", client_port),
                             1000, "ESTABLISHED"))
            rows.append(Conn(Addr("127.0.0.1", client_port), Addr("127.0.0.1", ws_port),
                             2000 + i, "ESTABLISHED"))
        self.rows = rows

    def __call__(self, kind="inet"):
        self.calls += 1
        self.threads.add(threading.current_thread())
        time.sleep(self.delay)
        return list(self.rows)


@pytest.fixture
def server():
    return BrowserPlugServer(host="localhost", port=8772, start_mcp=False)


@pytest.fixture
def table(server):
    fake = FakeTable(server.port)
    with patch("psutil.net_connections", fake):
        yield fake


class Ticker:
    """Wakes every 10 ms and records the longest gap: what a starved loop looks like."""

    def __init__(self):
        self.max_gap = 0.0
        self._stop = False

    async def run(self):
        last = time.perf_counter()
        while not self._stop:
            await asyncio.sleep(0.01)
            now = time.perf_counter()
            self.max_gap = max(self.max_gap, now - last)
            last = now

    def start(self):
        self._task = asyncio.ensure_future(self.run())

    async def stop(self):
        self._stop = True
        await self._task


async def get_health(server, identity=False):
    sent = []

    async def send(message):
        sent.append(message)

    if identity:
        await probes.handle_health_request(send, server, identity=True)
    else:
        await probes.handle_health_request(send, server)
    return json.loads(b"".join(m.get("body", b"") for m in sent
                               if m["type"] == "http.response.body"))


def test_one_health_report_runs_one_table_scan(server, table):
    """Was 1 + B: connected_browser_pids re-scanned the table for each browser socket."""
    server.get_health_info()
    assert table.calls == 1


@pytest.mark.asyncio
async def test_health_request_does_not_stall_the_loop(server, table):
    ticker = Ticker()
    ticker.start()
    await asyncio.sleep(0.05)
    body = await get_health(server)
    await ticker.stop()
    assert body["service"] == "browserplug"
    assert table.calls >= 1
    assert ticker.max_gap < SCAN_SECONDS / 2, (
        f"the event loop was blocked for {ticker.max_gap:.2f}s while /health scanned")


@pytest.mark.asyncio
async def test_health_snapshot_is_cached_and_single_flight(server, table):
    """Five launchers polling at once, then a sixth a moment later: one build."""
    await asyncio.gather(*(get_health(server) for _ in range(5)))
    await get_health(server)
    assert table.calls == 1


@pytest.mark.asyncio
async def test_health_snapshot_expires(server, table):
    server._health_snapshots = reports.SnapshotCache(ttl=0.05)
    await get_health(server)
    await asyncio.sleep(0.1)
    await get_health(server)
    assert table.calls == 2


@pytest.mark.asyncio
async def test_identity_health_scans_nothing_and_keeps_the_launcher_keys(server, table):
    full = json.loads(probes.health_body(server))
    table.calls = 0
    body = await get_health(server, identity=True)
    assert table.calls == 0
    for key in ("status", "service", "server_pid", "server_version", "started",
                "ws_port", "mcp_port", "tools_signature"):
        assert body[key] == full[key], key
    assert "connections" not in body


@pytest.mark.asyncio
async def test_health_endpoint_honours_the_identity_header_and_query(server, table):
    seen = []

    async def send(message):
        seen.append(message)

    for scope in ({"headers": [(HEALTH_IDENTITY_HEADER.lower().encode(), b"identity")]},
                  {"headers": [], "query_string": b"identity=1"}):
        seen.clear()
        req = SimpleNamespace(scope=scope, send=send, server=server)
        await http_endpoints._health(None, req)
        body = json.loads(next(m["body"] for m in seen if m["type"] == "http.response.body"))
        assert "connections" not in body and body["service"] == "browserplug"
    assert table.calls == 0


@pytest.mark.asyncio
async def test_get_connections_does_not_stall_the_ws_loop(server):
    ticker = Ticker()

    def slow_connections(kilo_details=True, viewer_host=None):
        time.sleep(SCAN_SECONDS)
        return {"connections": [], "count": 0}

    server.ws_rpc._h = dataclasses.replace(server.ws_rpc._h, connections_info=slow_connections,
                                           send=AsyncMock())
    ticker.start()
    await asyncio.sleep(0.05)
    await server.ws_rpc._action_get_connections({"data": {}}, None, "m1")
    await ticker.stop()
    assert ticker.max_gap < SCAN_SECONDS / 2, (
        f"the WebSocket loop was blocked for {ticker.max_gap:.2f}s building the report")


@pytest.mark.asyncio
async def test_websocket_connect_does_not_stall_the_loop(server):
    sock = AsyncMock()
    sock.remote_address = ("127.0.0.1", 1111)
    sock.close_code = None

    async def empty():
        return
        yield

    sock.__aiter__ = lambda *a: empty()

    def slow_probe(websocket):
        time.sleep(SCAN_SECONDS)
        return {}

    ticker = Ticker()
    ticker.start()
    await asyncio.sleep(0.05)
    with patch.object(server, "_probe_browser_identity", side_effect=slow_probe), \
         patch.object(server, "_notify_connection_waiters"):
        await server.handle_extension_connection(sock)
    await ticker.stop()
    assert ticker.max_gap < SCAN_SECONDS / 2, (
        f"the WebSocket loop was blocked for {ticker.max_gap:.2f}s at connect")


def test_launcher_polls_ask_for_the_identity_reply():
    """Both poll paths (the proxy transport's watch and the stdlib relay) carry the header."""
    from launcher import launcher
    headers = launcher.launcher_identity_headers()
    assert headers[HEALTH_IDENTITY_HEADER] == "identity"

    from launcher import stdio_relay
    seen = {}

    class Opener:
        def open(self, req, timeout=None):
            seen.update(dict(req.header_items()))
            raise OSError("stop here")

    relay = stdio_relay.StdioRelay(2830, log=lambda *a: None, identity_headers=headers)
    relay._opener = Opener()
    with pytest.raises(OSError):
        relay.health()
    assert seen[HEALTH_IDENTITY_HEADER.capitalize()] == "identity"
