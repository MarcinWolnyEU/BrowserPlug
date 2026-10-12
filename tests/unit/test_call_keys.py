"""T9 and T13 of the server/launcher review: which call a verdict belongs to, and which
client a browser selection belongs to.

T9: the outcome sink used to write the server's single newest-call slot, so with several
clients in flight one call's verdict landed on another client's record.
T13: pid keys carried no process start time (a reused pid inherited a dead client's
selection), and a session whose pid resolved late changed key mid-life.
"""

from types import SimpleNamespace

import pytest

from server.browsers import routing
from server.reporting.call_telemetry import CallTelemetry
from server.tools.browser_calls import BrowserCalls

FAILED = {"failed": True, "code": "timeout", "retryable": False, "degraded": []}


def _telemetry():
    clients = {"sess-aaaa1111": {"pid": 10}, "sess-bbbb2222": {"pid": 20}}
    return CallTelemetry(lambda: clients), clients


def _server_stub(telemetry):
    class S:
        _mcp_call = property(lambda self: telemetry.mcp_call)

        def record_call_outcome(self, *a, **k):
            return telemetry.record_call_outcome(*a, **k)
    return S()


def test_a_verdict_lands_on_the_call_it_belongs_to_not_the_newest_one():
    t, _ = _telemetry()
    t.record_command("sess-aaaa1111", "navigate", {"url": "https://example.org"}, req_id=1)
    first = t.mcp_call
    t.record_command("sess-bbbb2222", "tabs", {}, req_id=7)      # a second client, newer
    second = t.mcp_call
    assert first is not second

    # The navigate call finishes (failed) while the tabs call is still the newest.
    BrowserCalls(_server_stub(t)).record_outcome("navigate", FAILED)

    assert second.get("outcome") is None, "tabs must not inherit navigate's failure"


def test_the_call_id_routes_the_verdict_when_two_clients_run_the_same_tool():
    t, _ = _telemetry()
    t.record_command("sess-aaaa1111", "navigate", {}, req_id=1)
    a = t.mcp_call
    t.record_command("sess-bbbb2222", "navigate", {}, req_id=1)   # same JSON-RPC id, other session
    b = t.mcp_call
    calls = BrowserCalls(_server_stub(t))

    calls.record_outcome("navigate", FAILED, call=("sess-aaaa1111", 1))
    calls.record_outcome("navigate", None, call=("sess-bbbb2222", 1))

    assert a["outcome"] == FAILED
    assert b["outcome"] is None


def test_request_ids_compare_as_text_whatever_type_the_client_sent():
    t, _ = _telemetry()
    t.record_command("sess-aaaa1111", "tabs", {}, req_id=5)
    rec = t.mcp_call
    BrowserCalls(_server_stub(t)).record_outcome("tabs", FAILED, call=("sess-aaaa1111", "5"))
    assert rec["outcome"] == FAILED


def test_an_unknown_call_writes_nowhere():
    t, _ = _telemetry()
    t.record_command("sess-aaaa1111", "tabs", {}, req_id=5)
    rec = t.mcp_call
    BrowserCalls(_server_stub(t)).record_outcome("tabs", FAILED, call=("sess-zzzz9999", 99))
    assert rec.get("outcome") is None


def test_only_a_bounded_number_of_call_records_is_kept():
    t, clients = _telemetry()
    for i in range(500):
        t.record_command("sess-aaaa1111", "tabs", {}, req_id=i)
    assert len(t._calls) <= 64


# ---- T13 ---------------------------------------------------------------------------

@pytest.fixture
def procs(monkeypatch):
    """Fake process table: pid -> start epoch."""
    table = {}
    monkeypatch.setattr(routing, "process_started", lambda pid: table.get(int(pid)))
    return table


def test_a_reused_pid_does_not_inherit_the_dead_clients_selection(procs):
    procs[4100] = 1000
    old_key = routing.client_key("4100")
    selection = {old_key: "ff-1"}
    # The client dies; the OS hands 4100 to a new process started later.
    procs[4100] = 2000
    new_key = routing.client_key("4100")
    assert new_key != old_key
    assert new_key not in selection


def test_the_map_and_the_tool_side_agree_on_a_pid_with_a_start_time(procs):
    procs[4100] = 1000
    assert routing.pid_client_key(4100) == routing.client_key("4100")
    assert routing.pid_client_key(4100, 1000.4) == routing.client_key("4100")


def test_a_session_keeps_its_first_key_when_its_pid_resolves_late(procs):
    procs[300] = 50
    clients = {"sid": {}}
    first = routing.client_key(None, "sid", clients)
    assert first == "session:sid"
    clients["sid"]["pid"] = 300              # the ASGI wrapper resolves the pid afterwards
    assert routing.client_key(None, "sid", clients) == first


def test_a_session_that_starts_on_a_pid_keeps_it_when_the_header_goes_away(procs):
    procs[300] = 50
    clients = {"sid": {"pid": 300}}
    first = routing.client_key("300", "sid", clients)
    assert routing.client_key(None, "sid", clients) == first


def test_the_entrys_own_start_time_is_used_without_asking_the_os(procs):
    from datetime import datetime
    clients = {"sid": {"pid": 9, "start_time": datetime.fromtimestamp(1_800_000_000)}}
    assert routing.client_key(None, "sid", clients) == routing.pid_client_key(9, 1_800_000_000)


async def test_the_dispatcher_hands_the_sink_the_calls_identity():
    """Over HTTP the identity is (the Mcp-Session-Id header, the JSON-RPC request id): the
    same pair the ASGI wrapper filed the call's record under."""
    from server.tools.mcp_server import BrowserPlugMCPServer
    from server.tools.tool_registry import ToolRegistry, ToolSpec
    from server.web import request_context

    async def tabs() -> str:
        return "ok"

    seen = []
    registry = ToolRegistry([ToolSpec.from_function(tabs)])
    mcp = BrowserPlugMCPServer(registry, record=lambda *a: seen.append(a))
    ctx = SimpleNamespace(request_id=9, lifespan_context={})
    params = SimpleNamespace(name="tabs", arguments=None)

    with request_context.bound({"Mcp-Session-Id": "sess-1"}):
        result = await mcp._on_call_tool(ctx, params)
    assert result.is_error is False
    assert seen == [("tabs", None, ("sess-1", 9))]
