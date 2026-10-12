"""The route table of the MCP HTTP port, pinned as it behaves today.

http_endpoints.py owns one row per (method, path) with a policy column: LOCAL (only the
peer gate), TOKEN (a signature even from a local peer) and LOOPBACK (local peer only).
/shutdown, /restart, /timing and /diagnose-wan are CONTROL rows: a signature even from a
local peer, and no web page's request (review finding S2; test_control_endpoints.py).
"""

import asyncio
import json
import logging
import socket
import threading
import time
from unittest.mock import patch

import pytest

from secure_ext import signed_headers
from server.reporting import client_identity
from server.ws import http_auth
from server.web import host_guard, http_endpoints
from server import preflight
from server.web.http_endpoints import CONTROL, LOCAL, LOOPBACK, TOKEN, ROUTES, find_route
from server.server import BrowserPlugServer

TOKEN_VALUE = "a" * 43
LOCAL_PEER = ("127.0.0.1", 5555)
REMOTE_PEER = ("192.168.30.171", 44444)


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _server():
    return BrowserPlugServer(host="localhost", port=_free_port(),
                             start_mcp=False, ws_token=TOKEN_VALUE)


async def call(app, method, path, client=LOCAL_PEER, headers=None, body=b""):
    sent = []

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        sent.append(message)

    await app({"type": "http", "method": method, "path": path,
               "headers": headers or [], "client": client}, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    payload = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], dict(start["headers"]), payload


# --- the table itself ------------------------------------------------------------------

def test_the_route_table_is_pinned_row_by_row():
    """Every path and method with its policy. Adding, removing or re-classifying a row must
    show up here, in a diff a reviewer reads."""
    assert [(r.name, r.method, r.paths, r.policy) for r in ROUTES] == [
        ("health", "GET", ("/health", "/health/"), LOCAL),
        ("ready", "GET", ("/ready",), LOCAL),
        ("diagnose-wan", "GET", ("/diagnose-wan",), CONTROL),
        ("doctor", "GET", ("/doctor",), LOOPBACK),
        ("ws-token", "GET", ("/ws-token",), LOOPBACK),
        ("ws-bind", "POST", ("/ws-bind",), TOKEN),
        ("reset-config", "POST", ("/reset-config",), TOKEN),
        ("timing", "POST", ("/timing",), CONTROL),
        ("shutdown", "POST", ("/shutdown",), CONTROL),
        ("restart", "POST", ("/restart",), CONTROL),
    ]


def test_only_token_rows_carry_a_denial_message():
    for route in ROUTES:
        assert (route.deny_message is not None) == (route.policy in (TOKEN, CONTROL)), route.name


def test_the_mcp_fall_through_is_loopback_only():
    assert http_endpoints.MCP_POLICY == LOOPBACK
    for path in ("/mcp", "/mcp/", "/mcp/anything"):
        assert http_auth.is_loopback_only(path)


@pytest.mark.parametrize("method,path,name", [
    ("GET", "/health", "health"),
    ("GET", "/health/", "health"),
    ("GET", "/ready", "ready"),
    ("GET", "/ready/", "ready"),
    ("GET", "//ready", "ready"),
    ("GET", "/diagnose-wan/", "diagnose-wan"),
    ("GET", "/doctor/", "doctor"),
    ("GET", "/ws-token/", "ws-token"),
    ("POST", "/ws-bind/", "ws-bind"),
    ("POST", "/reset-config/", "reset-config"),
    ("POST", "/timing/", "timing"),
    ("POST", "/shutdown/", "shutdown"),
    ("POST", "/restart/", "restart"),
])
def test_paths_resolve_to_their_rows(method, path, name):
    assert find_route(method, path).name == name


@pytest.mark.parametrize("method,path", [
    ("POST", "/health"),           # wrong method
    ("GET", "/ws-bind"),
    ("GET", "/restart"),
    ("GET", "/shutdown"),
    ("GET", "/health//"),          # /health matches its two spellings exactly
    ("GET", "/mcp"),
    ("POST", "/mcp/"),
    ("GET", "/"),
    ("GET", ""),
    (None, "/health"),
])
def test_everything_else_falls_through_to_the_mcp_app(method, path):
    assert find_route(method, path) is None


# --- one definition of "this machine" ---------------------------------------------------

@pytest.mark.parametrize("peer", ["127.0.0.1", "::1", "::ffff:127.0.0.1", "localhost", ""])
def test_the_doctor_agrees_with_the_gate_about_who_is_local(peer):
    """preflight kept its own copy of the list and lacked the IPv4-mapped form."""
    assert http_auth.is_local_peer(peer)


def test_the_local_peer_list_has_a_single_definition():
    import inspect
    from server import server as server_module
    assert not hasattr(server_module, "LOCAL_PEER_HOSTS")
    assert not hasattr(preflight, "_LOCAL_PEER_HOSTS")
    import ast
    # Names and imports only: a comment that explains where the list went stays legal.
    tree = ast.parse(inspect.getsource(http_endpoints))
    used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    used |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    used |= {a.name for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))
             for a in n.names}
    assert "LOCAL_PEER_HOSTS" not in used


@pytest.mark.asyncio
async def test_the_doctor_serves_an_ipv4_mapped_loopback_peer():
    server = _server()
    app = server._wrap_client_tracking(lambda *a: None)
    status, _, _ = await call(app, "GET", "/doctor", client=("::ffff:127.0.0.1", 1))
    assert status == 200


# --- the policies, through the real wrapper ---------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("method,path", [("POST", "/ws-bind"), ("POST", "/reset-config")])
async def test_token_rows_refuse_a_local_peer_without_a_signature(method, path):
    server = _server()
    server.restarted_onto = []
    server._restart_onto = server.restarted_onto.append
    app = server._wrap_client_tracking(lambda *a: None)
    status, headers, body = await call(app, method, path, body=b"{}")
    assert status == 403
    assert "connection token is required" in json.loads(body)["error"]
    assert headers[b"cache-control"] == b"no-store"
    assert server.restarted_onto == []


@pytest.mark.asyncio
async def test_ws_token_refuses_a_remote_peer_even_with_a_signature():
    server = _server()
    app = server._wrap_client_tracking(lambda *a: None)
    status, _, _ = await call(app, "GET", "/ws-token", client=REMOTE_PEER,
                              headers=signed_headers(server, "GET", "/ws-token"))
    assert status == 403


@pytest.mark.asyncio
async def test_ws_token_answers_a_local_peer_without_cors_headers():
    server = _server()
    app = server._wrap_client_tracking(lambda *a: None)
    status, headers, body = await call(app, "GET", "/ws-token")
    assert status == 200
    assert set(json.loads(body)) == {"token", "ws_host", "ws_port"}
    assert headers[b"cache-control"] == b"no-store"
    assert not any(k.lower().startswith(b"access-control") for k in headers)


@pytest.mark.asyncio
async def test_a_local_restart_needs_a_signature():
    """Review finding S2: unsigned, the handler is never reached; signed, it restarts."""
    server = _server()
    began = []
    server._begin_restart = began.append
    app = server._wrap_client_tracking(lambda *a: None)
    status, _, _ = await call(app, "POST", "/restart")
    assert status == 403 and began == []
    status, headers, body = await call(app, "POST", "/restart",
                                       headers=signed_headers(server, "POST", "/restart"))
    assert status == 200
    assert json.loads(body)["status"] == "restarting"
    assert b"cache-control" not in headers
    assert began == ["POST /restart"]


@pytest.mark.asyncio
async def test_timing_takes_a_signed_local_report():
    server = _server()
    app = server._wrap_client_tracking(lambda *a: None)
    status, _, body = await call(app, "POST", "/timing", body=b"not json",
                                 headers=signed_headers(server, "POST", "/timing",
                                                        body=b"not json"))
    assert status == 200
    assert json.loads(body) == {"matched": False}


@pytest.mark.asyncio
async def test_a_remote_peer_needs_a_signature_on_a_local_row():
    server = _server()
    app = server._wrap_client_tracking(lambda *a: None)
    status, _, _ = await call(app, "POST", "/timing", client=REMOTE_PEER)
    assert status == 403
    status, _, _ = await call(app, "POST", "/timing", client=REMOTE_PEER,
                              headers=signed_headers(server, "POST", "/timing"))
    assert status == 200


@pytest.mark.asyncio
async def test_unrouted_requests_reach_the_mcp_app():
    server = _server()
    reached = []

    async def inner(scope, receive, send):
        reached.append(scope["path"])

    app = server._wrap_client_tracking(inner)
    await call_raw(app, "GET", "/nothing-here")
    assert reached == ["/nothing-here"]


async def call_raw(app, method, path):
    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        pass

    await app({"type": "http", "method": method, "path": path, "headers": [],
               "client": LOCAL_PEER}, receive, send)


@pytest.mark.asyncio
async def test_non_http_scopes_go_straight_to_the_mcp_app():
    server = _server()
    seen = []

    async def inner(scope, receive, send):
        seen.append(scope["type"])

    app = server._wrap_client_tracking(inner)
    await app({"type": "lifespan"}, None, None)
    assert seen == ["lifespan"]


# --- the Host-check hook ----------------------------------------------------------------

def test_the_default_host_check_admits_everything():
    assert http_endpoints.permissive_host_check({"headers": [(b"host", b"evil.example")]}) is None


@pytest.mark.asyncio
async def test_a_refusing_host_check_runs_before_the_gate_and_the_routes():
    server = _server()
    ep = http_endpoints.HttpEndpoints(
        server, lambda *a: None, is_localhost_bind=lambda h: True,
        host_check=lambda scope: (421, "unexpected Host"))
    sent = []

    async def send(message):
        sent.append(message)

    await ep.handle({"type": "http", "method": "GET", "path": "/health", "headers": [],
                     "client": LOCAL_PEER}, None, send)
    assert sent[0]["status"] == 421
    assert json.loads(sent[1]["body"]) == {"error": "unexpected Host"}


# --- the shared helpers -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_read_body_joins_chunks_and_stops_at_a_disconnect():
    messages = iter([
        {"type": "http.request", "body": b"ab", "more_body": True},
        {"type": "http.request", "body": b"cd", "more_body": True},
        {"type": "http.disconnect"},
    ])

    async def receive():
        return next(messages)

    assert await http_endpoints.read_body(receive) == b"abcd"


@pytest.mark.asyncio
async def test_json_reply_headers():
    sent = []

    async def send(message):
        sent.append(message)

    await http_endpoints.json_reply(send, {"a": 1}, status=201)
    assert sent[0]["status"] == 201
    assert sent[0]["headers"] == [(b"content-type", b"application/json"),
                                  (b"cache-control", b"no-store")]
    assert json.loads(sent[1]["body"]) == {"a": 1}
    sent.clear()
    await http_endpoints.json_reply(send, {}, no_store=False)
    assert sent[0]["headers"] == [(b"content-type", b"application/json")]


@pytest.mark.asyncio
async def test_require_token_answers_403_only_when_unsigned():
    sent = []

    async def send(message):
        sent.append(message)

    assert await http_endpoints.require_token({"browserplug.signed": True}, send, "no") is True
    assert sent == []
    assert await http_endpoints.require_token({}, send, "no") is False
    assert sent[0]["status"] == 403
    assert json.loads(sent[1]["body"]) == {"error": "no"}


# --- 2026-10-02: client identity is looked up off the loop ---------------------------------
# Finding server-era-1 and the 14 minute freeze: track_http_client walked the process table
# and masked the peer's argv on the uvicorn loop, once per session (and, for a 2026-07-28
# client with no connection id, once per TCP connection).

@pytest.fixture(autouse=True)
def _own_identity_pool(monkeypatch):
    """A pool of the test's own: lookups a previous test left sleeping must not queue ahead
    of this one's."""
    from concurrent.futures import ThreadPoolExecutor
    pool = ThreadPoolExecutor(max_workers=http_endpoints.IDENTITY_WORKERS)
    monkeypatch.setattr(http_endpoints, "_identity_pool", pool)
    yield
    pool.shutdown(wait=True, cancel_futures=True)


class SlowIdentity:
    """resolve_client_chain / describe_pid stand-ins that take `seconds` and record the
    thread they ran on."""

    def __init__(self, seconds):
        self.seconds = seconds
        self.threads = []

    def chain(self, port):
        self.threads.append(threading.current_thread())
        time.sleep(self.seconds)
        return 4321, None

    def describe(self, pid):
        return {"pid": pid, "friendly": "Slow Client", "exe_path": "C:/slow.exe",
                "cmdline": "slow", "cwd": "C:/w", "start_time": None}

    def __enter__(self):
        self._patches = [patch.object(client_identity, "resolve_client_chain", self.chain),
                         patch.object(client_identity, "describe_pid", self.describe)]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()


def _mcp_post(session, body=b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}', peer=LOCAL_PEER,
              extra=()):
    sent = []
    chunks = [{"type": "http.request", "body": body, "more_body": False}]

    async def receive():
        return chunks.pop(0) if chunks else {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": "/mcp",
             "headers": [(b"mcp-session-id", session.encode()), *extra], "client": peer}
    return scope, receive, send, sent


async def _timed(coro):
    start = time.perf_counter()
    result = await coro
    return result, time.perf_counter() - start


def _echo_app(arrived):
    async def inner(scope, receive, send):
        arrived.append(time.perf_counter())
        await receive()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})
    return inner


@pytest.mark.asyncio
async def test_the_identity_lookup_runs_on_a_worker_thread_and_fills_the_row_when_done():
    server = _server()
    arrived = []
    app = server._wrap_client_tracking(_echo_app(arrived))
    with SlowIdentity(0.3) as slow:
        scope, receive, send, sent = _mcp_post("s1")
        (_, elapsed) = await _timed(app(scope, receive, send))
        assert elapsed < 0.25, "the request waited for the lookup"
        assert server.http_clients["s1"]["identified"] is False
        assert server.http_clients["s1"]["friendly"] is None
        for _ in range(100):
            await asyncio.sleep(0.05)
            if server.http_clients["s1"]["identified"]:
                break
    assert server.http_clients["s1"]["friendly"] == "Slow Client"
    assert server.http_clients["s1"]["pid"] == 4321
    assert slow.threads and slow.threads[0] is not threading.current_thread()


@pytest.mark.asyncio
async def test_a_slow_identity_lookup_delays_neither_health_nor_another_clients_request():
    server = _server()
    arrived = []
    app = server._wrap_client_tracking(_echo_app(arrived))
    with SlowIdentity(1.0):
        scope, receive, send, _ = _mcp_post("hostile", peer=("127.0.0.1", 6001))
        first = asyncio.ensure_future(app(scope, receive, send))
        await asyncio.sleep(0.05)       # the lookup is under way on its worker

        async def health():
            sent = []

            async def s(message):
                sent.append(message)

            async def r():
                return {"type": "http.request", "body": b"", "more_body": False}

            await app({"type": "http", "method": "GET", "path": "/health",
                       "query_string": b"identity=1", "headers": [],
                       "client": LOCAL_PEER}, r, s)
            return next(m["status"] for m in sent if m["type"] == "http.response.start")

        status, health_seconds = await _timed(health())
        other = _mcp_post("other", peer=("127.0.0.1", 6002))
        _, other_seconds = await _timed(app(*other[:3]))
        await first
    assert status == 200
    assert health_seconds < 0.4, f"/health waited {health_seconds:.2f}s behind a lookup"
    assert other_seconds < 0.4, f"another client's request waited {other_seconds:.2f}s"


@pytest.mark.asyncio
async def test_a_tools_call_waits_for_its_sessions_lookup_but_only_so_long(monkeypatch):
    """The call is filed under the client's real process when the lookup is quick; a slow
    one is not waited out."""
    monkeypatch.setattr(http_endpoints, "IDENTITY_WAIT_SECONDS", 0.3)
    call = b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"server_info"}}'
    server = _server()
    app = server._wrap_client_tracking(_echo_app([]))
    with SlowIdentity(0.15):
        quick = _mcp_post("quick", body=call, peer=("127.0.0.1", 6003))
        await app(*quick[:3])
    assert server.http_clients["quick"]["identified"] is True, (
        "a quick lookup must be finished before the call is recorded")
    slow_server = _server()
    slow_app = slow_server._wrap_client_tracking(_echo_app([]))
    with SlowIdentity(1.5):
        slow = _mcp_post("slow", body=call, peer=("127.0.0.1", 6004))
        _, seconds = await _timed(slow_app(*slow[:3]))
    assert 0.25 < seconds < 0.9, f"waited {seconds:.2f}s"
    assert slow_server.http_clients["slow"]["identified"] is False


@pytest.mark.asyncio
async def test_a_flood_of_new_connections_cannot_queue_unbounded_lookups(monkeypatch):
    monkeypatch.setattr(http_endpoints, "MAX_IDENTITY_PENDING", 2)
    server = _server()
    app = server._wrap_client_tracking(_echo_app([]))
    with SlowIdentity(0.4) as slow:
        for i in range(8):
            scope, receive, send, _ = _mcp_post(f"flood-{i}", peer=("127.0.0.1", 7000 + i))
            await app(scope, receive, send)
        assert len(server.http_clients) == 8, "every session still gets its row"
        await asyncio.sleep(1.5)
    assert len(slow.threads) <= 4, f"{len(slow.threads)} lookups ran for 8 sessions"


@pytest.mark.asyncio
async def test_the_same_session_on_one_connection_is_looked_up_once():
    server = _server()
    app = server._wrap_client_tracking(_echo_app([]))
    with SlowIdentity(0.2) as slow:
        for _ in range(5):
            scope, receive, send, _ = _mcp_post("one")
            await app(scope, receive, send)
        await asyncio.sleep(0.6)
    assert len(slow.threads) == 1


# --- relay-2: a cancel that lands before the body is read -----------------------------------

@pytest.mark.asyncio
async def test_a_client_that_hangs_up_before_its_body_arrives_is_dropped_quietly(caplog):
    """The relay cancels by shutting the socket down. Replaying that disconnect to the SDK
    app made starlette raise ClientDisconnect out of it, which uvicorn logs as an ERROR
    traceback that looks like a server bug."""
    server = _server()
    reached = []

    async def inner(scope, receive, send):
        reached.append(scope["path"])
        await receive()                 # would be the disconnect, replayed

    app = server._wrap_client_tracking(inner)
    sent = []

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    with caplog.at_level(logging.DEBUG), SlowIdentity(0):
        await app({"type": "http", "method": "POST", "path": "/mcp",
                   "headers": [(b"mcp-session-id", b"s1")], "client": LOCAL_PEER},
                  receive, send)
    assert reached == [], "the SDK app must not be called for a request nobody can answer"
    assert sent == []
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


@pytest.mark.asyncio
async def test_a_disconnect_after_the_body_was_read_still_reaches_the_app():
    """Only a hang-up BEFORE the body is complete is dropped: the SDK needs to see a later
    disconnect (a client that leaves mid-response) to cancel its own work."""
    server = _server()
    seen = []

    async def inner(scope, receive, send):
        seen.append((await receive())["type"])
        seen.append((await receive())["type"])

    app = server._wrap_client_tracking(inner)
    scope, _, send, _ = _mcp_post("s1")
    queue = [{"type": "http.request", "body": b"{}", "more_body": False},
             {"type": "http.disconnect"}]

    async def receive():
        return queue.pop(0)

    await app(scope, receive, send)
    assert seen == ["http.request", "http.disconnect"]


# --- the Host check's interface lookup stays off the loop -----------------------------------

@pytest.mark.asyncio
async def test_the_host_guards_interface_lookup_runs_on_a_worker_not_the_loop():
    threads = []

    def addresses():
        threads.append(threading.current_thread())
        time.sleep(0.4)
        return {"192.168.9.9"}

    guard = host_guard.HostGuard(addresses=addresses)
    server = _server()
    reached = []
    endpoints = http_endpoints.HttpEndpoints(
        server, _echo_app(reached), is_localhost_bind=lambda host: True,
        host_check=guard.check, host_check_async=guard.check_async)

    ticks = []

    async def ticker():
        while True:
            ticks.append(time.perf_counter())
            await asyncio.sleep(0.01)

    task = asyncio.ensure_future(ticker())
    scope, receive, send, sent = _mcp_post("s1")
    scope["headers"].append((b"host", b"192.168.9.9:2830"))
    await endpoints.handle(scope, receive, send)
    task.cancel()
    assert threads and threads[0] is not threading.current_thread()
    gaps = [b - a for a, b in zip(ticks, ticks[1:])]
    assert max(gaps) < 0.2, f"the loop stalled {max(gaps):.2f}s on the address lookup"
    assert reached, "the request itself was admitted"


@pytest.mark.asyncio
async def test_a_known_host_is_answered_on_the_loop_from_the_cache():
    calls = []
    guard = host_guard.HostGuard(addresses=lambda: calls.append(threading.current_thread()) or set())
    guard.host_allowed("127.0.0.1")
    scope = {"type": "http", "method": "GET", "path": "/health",
             "headers": [(b"host", b"127.0.0.1:2830")]}
    before = len(calls)
    assert await guard.check_async(scope) is None
    assert len(calls) == before, "a loopback Host needs no interface lookup"
    refused = await guard.check_async({"type": "http", "method": "GET", "path": "/health",
                                       "headers": [(b"host", b"evil.example:2830")]})
    assert refused == (403, "unexpected Host header")


@pytest.mark.asyncio
async def test_ws_bind_scans_the_interfaces_off_the_loop():
    server = _server()
    server._restart_onto = lambda host: None
    threads = []

    def scan():
        threads.append(threading.current_thread())
        return [{"ip": "192.168.1.5"}]

    server.ws_lan_addresses = scan
    app = server._wrap_client_tracking(lambda *a: None)
    body = json.dumps({"host": "localhost"}).encode()
    status, _, payload = await call(app, "POST", "/ws-bind", body=body,
                                    headers=signed_headers(server, "POST", "/ws-bind", body=body))
    assert status == 200, payload
    assert threads and threads[0] is not threading.current_thread()


# --- a slow identity lookup must not decide a client's routing key ----------------------------
# A tools/call waits IDENTITY_WAIT_SECONDS for its session's lookup and then goes ahead. The
# tool layer asks routing.client_key for the session's key during that call, and the first key
# it gets used to stick for the session's life. A lookup that took 1.1 s pinned the client to
# "session:<id>", one that took 0.9 s to "pid:<n>": the browser selection depended on a race.

CALL = b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"server_info"}}'


async def _land(server, sid, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if server.http_clients[sid]["identified"]:
            await asyncio.sleep(0.05)       # the re-key is queued onto the loop
            return
        await asyncio.sleep(0.02)
    raise AssertionError("the identity lookup never landed")


@pytest.mark.asyncio
async def test_a_call_that_outran_a_slow_lookup_does_not_pin_the_client_to_its_session_key(
        monkeypatch):
    from server.browsers import routing
    monkeypatch.setattr(routing, "process_started", lambda pid: None)
    monkeypatch.setattr(http_endpoints, "IDENTITY_WAIT_SECONDS", 0.15)
    server = _server()
    app = server._wrap_client_tracking(_echo_app([]))
    with SlowIdentity(0.6):
        scope, receive, send, _ = _mcp_post("slow", body=CALL, peer=("127.0.0.1", 6101))
        await app(scope, receive, send)
        entry = server.http_clients["slow"]
        assert entry["identified"] is False, "the call went ahead before the lookup landed"
        # what the tool layer does during that call:
        assert routing.client_key(None, "slow", server.http_clients) == "session:slow"
        assert "client_key" not in entry, "a placeholder key must not be stored as final"
        await _land(server, "slow")
    assert routing.client_key(None, "slow", server.http_clients) == "pid:4321"
    assert entry["client_key"] == "pid:4321"


@pytest.mark.asyncio
async def test_a_quick_lookup_and_a_slow_one_end_in_the_same_key(monkeypatch):
    from server.browsers import routing
    monkeypatch.setattr(routing, "process_started", lambda pid: None)
    monkeypatch.setattr(http_endpoints, "IDENTITY_WAIT_SECONDS", 0.3)
    keys = {}
    for name, seconds in (("quick", 0.05), ("slow", 0.8)):
        server = _server()
        app = server._wrap_client_tracking(_echo_app([]))
        with SlowIdentity(seconds):
            scope, receive, send, _ = _mcp_post(name, body=CALL, peer=("127.0.0.1", 6102))
            await app(scope, receive, send)
            routing.client_key(None, name, server.http_clients)     # the call's own lookup
            await _land(server, name)
        keys[name] = routing.client_key(None, name, server.http_clients)
    assert keys["quick"] == keys["slow"] == "pid:4321"


@pytest.mark.asyncio
async def test_a_browser_chosen_while_the_client_was_anonymous_follows_it_to_its_real_key(
        monkeypatch):
    from server.browsers import routing
    monkeypatch.setattr(routing, "process_started", lambda pid: None)
    monkeypatch.setattr(http_endpoints, "IDENTITY_WAIT_SECONDS", 0.1)
    server = _server()
    app = server._wrap_client_tracking(_echo_app([]))
    with SlowIdentity(0.5):
        scope, receive, send, _ = _mcp_post("slow", body=CALL, peer=("127.0.0.1", 6103))
        await app(scope, receive, send)
        key = routing.client_key(None, "slow", server.http_clients)
        server.browser_selection[key] = "firefox-1"      # `browsers use`, mid-lookup
        await _land(server, "slow")
    assert dict(server.browser_selection) == {"pid:4321": "firefox-1"}


def test_a_selection_under_the_real_key_wins_over_the_placeholders():
    from server.browsers import routing
    clients = {"s": {"pid": 77, "start_time": 5, "identified": True}}
    selection = {"session:s": "old", "pid:77@5": "kept"}
    assert routing.rekey_selection(selection, "s", clients) is True
    assert selection == {"pid:77@5": "kept"}
    assert routing.rekey_selection(selection, "s", clients) is False


def test_nothing_moves_for_a_client_that_is_still_anonymous():
    from server.browsers import routing
    clients = {"s": {"pid": None, "identified": False}}
    selection = {"session:s": "firefox-1"}
    assert routing.rekey_selection(selection, "s", clients) is False
    assert selection == {"session:s": "firefox-1"}


def test_a_session_key_is_final_unless_a_lookup_is_still_in_flight():
    from server.browsers import routing
    clients = {"done": {"pid": None, "identity_pending": False},
               "none": {}, "busy": {"pid": None, "identity_pending": True}}
    for name in ("done", "none"):
        assert routing.client_key(None, name, clients) == f"session:{name}"
        assert clients[name]["client_key"] == f"session:{name}"
    assert routing.client_key(None, "busy", clients) == "session:busy"
    assert "client_key" not in clients["busy"]


@pytest.mark.asyncio
async def test_a_lookup_that_finds_no_process_leaves_the_session_key_final(monkeypatch):
    from server.browsers import routing
    monkeypatch.setattr(http_endpoints, "IDENTITY_WAIT_SECONDS", 0.05)
    server = _server()
    app = server._wrap_client_tracking(_echo_app([]))
    with patch.object(client_identity, "resolve_client_chain", lambda port: (None, None)), \
            patch.object(client_identity, "describe_pid", lambda pid: {"pid": None}):
        scope, receive, send, _ = _mcp_post("ghost", body=CALL, peer=("127.0.0.1", 6104))
        await app(scope, receive, send)
        for _ in range(100):
            if not server.http_clients["ghost"].get("identity_pending"):
                break
            await asyncio.sleep(0.02)
    assert routing.client_key(None, "ghost", server.http_clients) == "session:ghost"
    assert server.http_clients["ghost"]["client_key"] == "session:ghost"
