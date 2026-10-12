"""
Unit tests for the per-Firefox-instance view exposed to the popup / /health.

Multiple Firefox instances used to fight over a single extension slot (flip-flop) and
the server warned about it. They no longer do: the server keeps a BROWSER REGISTRY and
several connected Firefoxes are a supported state. These tests cover what
get_connections_info() reports about them — one row per browser, and NO warning.

The registry is driven directly (no psutil / websockets needed) so the tests are
deterministic and Firefox-free. The registry mechanics themselves live in
test_browser_registry.py.
"""

from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest

from fake_process_table import fake_process_table  # noqa: F401  (autouse)
from server.reporting import call_telemetry
from server.server import BrowserPlugServer


def _connect(server, instance_id, *, pid=None, version="1.3.0",
             peer="127.0.0.1"):
    """Register a connected browser as a connect + extension_hello would."""
    sock = AsyncMock()
    sock.close_code = None
    sock.remote_address = (peer, 40000 + (pid or 0) % 1000)
    ident = {"pid": pid, "friendly": "Firefox",
             "exe_path": r"C:\Program Files\Firefox\firefox.exe"} if pid else {}
    with patch.object(server, "_probe_browser_identity", return_value=ident):
        browser = server.registry.register(sock)
    browser = server.registry.promote(browser, instance_id)
    browser.version = version
    return browser


class TestMultiInstanceView:

    @pytest.fixture
    def server(self):
        return BrowserPlugServer(host="localhost", port=8772, start_mcp=False)

    def test_no_instances(self, server):
        info = server.get_connections_info()
        assert info["extension_instances"] == []
        assert "multiple_extensions" not in info
        assert "extension_warning" not in info

    def test_single_instance(self, server):
        _connect(server, "ff-1", pid=111)
        info = server.get_connections_info()
        assert len(info["extension_instances"]) == 1
        assert info["extension_instances"][0]["pid"] == 111
        assert "multiple_extensions" not in info
        assert "extension_warning" not in info

    def test_two_instances_are_listed_and_never_warned_about(self, server):
        _connect(server, "ff-1", pid=111)
        _connect(server, "ff-2", pid=222)
        info = server.get_connections_info()

        # Two Firefoxes is a SUPPORTED state. The retired warning keys are not on the wire.
        assert "multiple_extensions" not in info
        assert "extension_warning" not in info

        by_pid = {i["pid"]: i for i in info["extension_instances"]}
        assert set(by_pid) == {111, 222}
        assert [i["slot"] for i in info["extension_instances"]] == [1, 2]
        assert all(i["currently_connected"] for i in info["extension_instances"])

    def test_a_disconnected_instance_drops_out(self, server):
        a = _connect(server, "ff-1", pid=111)
        _connect(server, "ff-2", pid=222)

        server.registry.remove(a)

        pids = [i["pid"] for i in server.get_connections_info()["extension_instances"]]
        assert pids == [222]

    def test_activity_is_attributed_to_the_browser_it_was_sent_to(self, server):
        a = _connect(server, "ff-1", pid=111)
        b = _connect(server, "ff-2", pid=222)

        call_telemetry.note_browser_activity(a, "tabs.list")

        info = {i["pid"]: i for i in server.get_connections_info()["extension_instances"]}
        assert info[111]["last_action"] == "tabs.list"
        assert info[111]["last_activity_hms"] is not None
        # The other browser is untouched: a real command reports only where it went.
        assert info[222]["last_action"] is None
        assert info[222]["last_activity_hms"] is None
        assert b.last_action is None

    def test_note_activity_without_a_browser_is_a_no_op(self, server):
        _connect(server, "ff-1", pid=111)
        call_telemetry.note_browser_activity(None, "tabs.list")  # must not raise
        assert server.get_connections_info()["extension_instances"][0]["last_action"] is None

    def test_connection_rows_one_per_browser(self, server):
        _connect(server, "ff-1", pid=111)
        _connect(server, "ff-2", pid=222)

        rows = [c for c in server.get_connections_info()["connections"]
                if c["kind"] == "extension"]

        assert len(rows) == 2
        assert [r["pid"] for r in rows] == [111, 222]
        # Row SHAPE is unchanged (the popup renders server-provided rows). The label is
        # the browser's NAME, so two browsers of one edition read alike; `slot` and `pid`
        # are the fields that tell them apart, and the popup paints those itself (on the
        # row icon, and under the activity time).
        assert [r["label"] for r in rows] == ["Firefox", "Firefox"]
        assert [r["slot"] for r in rows] == [1, 2]
        assert all(r["detail"] == f"WebSocket: {server.port}" for r in rows)

    def test_single_browser_row_label_is_unchanged(self, server):
        _connect(server, "ff-1", pid=111)
        row = [c for c in server.get_connections_info()["connections"]
               if c["kind"] == "extension"][0]
        assert row["label"] == "Firefox"  # no slot prefix in the single-browser case

    def test_rows_carry_slot_and_remoteness_for_the_connection_map(self, server):
        # The options page's connection map correlates connection rows with
        # extension_instances by SLOT (a remote browser has no pid to match on)
        # and draws remote browsers distinctly from is_remote/remote_addr.
        _connect(server, "ff-1", pid=111)
        _connect(server, "ff-2", peer="192.168.0.7")
        info = server.get_connections_info()

        rows = {c["slot"]: c for c in info["connections"] if c["kind"] == "extension"}
        assert rows[1]["is_remote"] is False
        assert rows[2]["is_remote"] is True
        assert rows[2]["remote_addr"] == "192.168.0.7"

        insts = {i["slot"]: i for i in info["extension_instances"]}
        assert insts[1]["is_remote"] is False
        assert insts[2]["is_remote"] is True

        # Server-clock epoch: lets the map age activity_epoch without mixing in
        # the client's own clock.
        assert isinstance(info["generated_epoch"], float)


def _client(server, pid):
    """An MCP client session as the ASGI wrapper records it."""
    server.http_clients[f"sess-{pid}"] = {
        "pid": pid, "friendly": "Claude Code", "cwd": r"C:\dr\browserplug",
        "exe_path": None, "start_time": None, "launcher_pid": pid + 1,
        "launcher_start_time": None, "last_activity": datetime.now(), "history": [],
    }


def _client_row(server, pid):
    return [c for c in server.get_connections_info()["connections"]
            if c["kind"] == "mcp_client" and c["pid"] == pid][0]


class TestClientRoutingForTheMap:
    """`target_browser_slot` on an mcp_client row: which browser that client's calls
    actually reach. The connection map's routing line is drawn from it, so it has to
    agree with resolve_target_browser — including where that one refuses to route."""

    @pytest.fixture
    def server(self):
        return BrowserPlugServer(host="localhost", port=8772, start_mcp=False)

    def test_no_selection_routes_to_the_first_connected_browser(self, server):
        _connect(server, "ff-1", pid=111)
        _connect(server, "ff-2", pid=222)
        _client(server, 777)

        row = _client_row(server, 777)
        assert row["target_browser_slot"] == 1
        assert row["target_browser_explicit"] is False

    def test_an_explicit_selection_is_reported_as_such(self, server):
        _connect(server, "ff-1", pid=111)
        _connect(server, "ff-2", pid=222)
        _client(server, 777)
        server.browser_selection["pid:777"] = "ff-2"

        row = _client_row(server, 777)
        assert row["target_browser_slot"] == 2
        assert row["target_browser_explicit"] is True

    def test_a_selection_pointing_at_a_departed_browser_routes_nowhere(self, server):
        # resolve_target_browser ERRORS here rather than falling back (acting in the
        # wrong logged-in browser is worse), so the map must draw no line at all —
        # never one to whichever browser happens to be first.
        gone = _connect(server, "ff-1", pid=111)
        _connect(server, "ff-2", pid=222)
        _client(server, 777)
        server.browser_selection["pid:777"] = "ff-1"
        server.registry.remove(gone)

        row = _client_row(server, 777)
        assert row["target_browser_slot"] is None
        assert row["target_browser_explicit"] is True

    def test_reading_the_route_never_mutates_the_selection(self, server):
        """It is a diagnostic: unlike resolve_target_browser it must not prune."""
        gone = _connect(server, "ff-1", pid=111)
        _client(server, 777)
        server.browser_selection["pid:777"] = "ff-1"
        server.registry.remove(gone)

        assert _client_row(server, 777)["target_browser_slot"] is None
        assert server.browser_selection["pid:777"] == "ff-1", "still the client's to change"

    def test_nothing_connected_means_no_route(self, server):
        _client(server, 777)
        assert _client_row(server, 777)["target_browser_slot"] is None
