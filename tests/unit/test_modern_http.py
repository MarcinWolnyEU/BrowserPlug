"""2026-07-28 over HTTP /mcp, behind the real wrapper: who the server thinks the client is.

A 2026-07-28 request carries no Mcp-Session-Id, and the tool layer keys everything about a
client on one: its Connections row, the record its calls open and receive their verdict
on, its browser selection. HttpEndpoints files a modern request under a connection key
(modern_identity.py: the relay's X-BrowserPlug-Connection-Id, else a hash of the TCP peer)
and binds it into the request context as `mcp-session-id`. These tests run the real MCP
app and the real tools behind the production wrapper, with the SDK's own client and with
raw requests shaped like the stdio relay's, and read the server's state afterwards.

The ASGI tests fix the peer address, so the key is known. The TCP tests run uvicorn on its
own thread, which is the only way a listen stream or a GET stream stays open.
"""

import asyncio
import json
import os
import uuid

import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.server.subscriptions import ToolsListChanged
from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS

import test_imports  # noqa: F401  (path setup)
from browserplug_shared import mcp_era
from port_coordinator import allocate_port
from server.server import BrowserPlugServer
from server.tools.tool_outcome import EXTENSION_DISCONNECTED
from server.tools.tool_registry import ToolSpec
from server.web import http_endpoints, modern_identity, request_context
from test_list_changed import (NINE, GetStreams, Heard, Roster, http_client, names, reshape,
                               serving, wait_until)

TOKEN = "a" * 43
PEER_A = ("127.0.0.1", 41001)
PEER_B = ("127.0.0.1", 41002)
PEER_C = ("127.0.0.1", 41003)
POST_HEADERS = {"content-type": "application/json",
                "accept": "application/json, text/event-stream"}
CLIENT_INFO = {"name": "modern-http-test", "version": "1"}


@pytest.fixture(autouse=True)
def default_era(monkeypatch):
    """The setting in force is `all`, whatever a developer's shell exports."""
    monkeypatch.delenv(mcp_era.ERA_ENV, raising=False)


def _server():
    return BrowserPlugServer(host="localhost", port=allocate_port(), start_mcp=False,
                             ws_token=TOKEN)


def _key(peer):
    return modern_identity.peer_key(peer)


class Posts:
    """An httpx2 request hook recording the JSON-RPC id of each tools/call the client sends."""

    def __init__(self):
        self.calls = []

    async def __call__(self, request):
        if request.method == "POST":
            body = json.loads(request.content)
            if body.get("method") == "tools/call":
                self.calls.append(body["id"])


def asgi_client(app, peer, posts=None, **headers):
    """An httpx2 client driving `app` in process as the TCP peer `peer`."""
    hooks = {"request": [posts]} if posts is not None else {}
    return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app, client=peer),
                              base_url="http://127.0.0.1", headers=headers, event_hooks=hooks)


def modern_post(method, msg_id, name=None, **params):
    """(headers, body) of a 2026-07-28 request the way the SDK client, and the relay once
    it carries the era, send it: the routing headers plus the _meta envelope."""
    meta = {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientInfo": CLIENT_INFO,
            "io.modelcontextprotocol/clientCapabilities": {}}
    headers = {**POST_HEADERS, "mcp-protocol-version": "2026-07-28", "mcp-method": method}
    if name is not None:
        headers["mcp-name"] = name
        params["name"] = name
    return headers, {"jsonrpc": "2.0", "id": msg_id, "method": method,
                     "params": {**params, "_meta": meta}}


def reply_of(response):
    """The one JSON-RPC message a POST answered with, whether as JSON or as SSE."""
    assert response.status_code == 200, response.text
    if response.headers["content-type"].startswith("application/json"):
        return response.json()
    (message,) = [json.loads(line[len("data:"):]) for line in response.text.splitlines()
                  if line.startswith("data:")]
    return message


def mcp_clients(server):
    """The Connections report's MCP client rows."""
    report = server.get_connections_info(kilo_details=False)
    return [row for row in report["connections"] if row["kind"] == "mcp_client"]


# --- the SDK's own client ---------------------------------------------------------------

async def test_a_modern_sdk_client_is_tracked_and_its_verdict_filed_under_its_connection():
    """The full set: a Connections row, last_command, the call's own record and the
    verdict on it. Without a key every one of them was empty for a modern HTTP client."""
    server = _server()
    app = server.mcp_app.http_app()
    posts, key = Posts(), _key(PEER_A)
    async with app.router.lifespan_context(app):
        async with asgi_client(server._wrap_client_tracking(app), PEER_A, posts) as http:
            transport = streamable_http_client("http://127.0.0.1/mcp", http_client=http)
            async with Client(transport) as client:
                assert client.protocol_version == "2026-07-28"
                result = await client.call_tool("tabs", {"action": "list"})
    assert result.is_error is True, "no browser is connected"
    assert list(server.http_clients) == [key]
    (request_id,) = posts.calls

    record = server.telemetry._calls[(key[:8], str(request_id))]
    assert record["tool"] == "tabs"
    assert record["outcome"]["code"] == EXTENSION_DISCONNECTED, (
        "the verdict is filed on the record the wrapper opened for this call; it used to "
        "go to the session manager's one lifespan token, which matches no record")
    assert server.telemetry.last_mcp["session"] == key[:8]
    assert server.telemetry.last_mcp["tool"] == "tabs"
    health = server.get_health_info()
    assert health["last_command"]["outcome"]["code"] == EXTENSION_DISCONNECTED

    (row,) = mcp_clients(server)
    assert [s["id"] for s in row["sessions"]] == [key[:8]]
    assert [c["tool"] for c in row["sessions"][0]["commands"]] == ["tabs"]


async def test_each_connection_is_its_own_client_and_selects_its_own_browser():
    """Before, every modern client without a pid header shared the "local" key, so one
    client's browsers(use) moved every other's calls."""
    server = _server()

    async def client_key() -> str:
        return server.mcp_tools.calls.client_key()

    server.mcp_tools.registry.add(ToolSpec.from_function(client_key))
    app = server.mcp_app.http_app()
    seen = {}
    async with app.router.lifespan_context(app):
        for name, peer, extra in (("a", PEER_A, {}), ("b", PEER_B, {}),
                                  ("c", PEER_C, {"x-browserplug-client-pid": str(os.getpid())})):
            async with asgi_client(server._wrap_client_tracking(app), peer, **extra) as http:
                transport = streamable_http_client("http://127.0.0.1/mcp", http_client=http)
                async with Client(transport) as client:
                    assert client.protocol_version == "2026-07-28"
                    result = await client.call_tool("client_key", {})
                    seen[name] = result.content[0].text
    assert seen["a"] == f"session:{_key(PEER_A)}"
    assert seen["b"] == f"session:{_key(PEER_B)}"
    assert seen["a"] != seen["b"], "two connections are two clients"
    assert "local" not in seen.values()
    assert seen["c"].startswith("pid:"), "a forwarded pid names the client, as for a session"
    assert server.http_clients[_key(PEER_A)]["client_key"] == seen["a"], "the key sticks"


async def test_the_tool_layer_reads_the_connection_key_where_it_reads_a_session_id():
    server = _server()

    async def whoami() -> str:
        return request_context.header("mcp-session-id") or "no session"

    server.mcp_tools.registry.add(ToolSpec.from_function(whoami))
    app = server.mcp_app.http_app()
    async with app.router.lifespan_context(app):
        async with asgi_client(server._wrap_client_tracking(app), PEER_A) as http:
            transport = streamable_http_client("http://127.0.0.1/mcp", http_client=http)
            async with Client(transport) as client:
                result = await client.call_tool("whoami", {})
    assert result.content[0].text == _key(PEER_A), (
        "the handler runs in a copy of the request's context, which carries the key")


# --- requests shaped like the relay's ---------------------------------------------------

async def test_a_connection_id_names_the_client_across_tcp_connections():
    """The relay sends its connection id, one per process, on every request. The key is
    that id and not the peer, so the row, the call records and the browser selection
    follow the stdio connection, and they survive a server restart."""
    server = _server()
    app = server.mcp_app.http_app()
    wrapped = server._wrap_client_tracking(app)
    connection = uuid.uuid4().hex
    extra = {mcp_era.CONNECTION_ID_HEADER: connection, "x-browserplug-client-pid": str(os.getpid())}
    async with app.router.lifespan_context(app):
        for peer, msg_id in ((PEER_A, 7), (PEER_B, 8)):
            async with asgi_client(wrapped, peer) as http:
                headers, body = modern_post("tools/call", msg_id, name="tabs",
                                            arguments={"action": "list"})
                reply = reply_of(await http.post("/mcp", headers={**headers, **extra}, json=body))
                assert reply["id"] == msg_id and reply["result"]["isError"] is True, reply
    assert list(server.http_clients) == [connection], "both peers, one connection"
    assert server.http_clients[connection]["pid"] == os.getpid(), "the forwarded pid is used"
    assert server.telemetry._calls[(connection[:8], "7")]["outcome"]["code"] \
        == EXTENSION_DISCONNECTED
    assert server.telemetry._calls[(connection[:8], "8")]["outcome"]["code"] \
        == EXTENSION_DISCONNECTED
    (row,) = mcp_clients(server)
    assert [s["id"] for s in row["sessions"]] == [connection[:8]]
    assert len(row["sessions"][0]["commands"]) == 2


async def test_a_malformed_connection_id_is_ignored():
    server = _server()
    app = server.mcp_app.http_app()
    async with app.router.lifespan_context(app):
        async with asgi_client(server._wrap_client_tracking(app), PEER_A) as http:
            headers, body = modern_post("tools/list", 1)
            reply = reply_of(await http.post(
                "/mcp", headers={**headers, mcp_era.CONNECTION_ID_HEADER: "not-an-id"},
                json=body))
    assert names_of(reply) == NINE
    assert list(server.http_clients) == [_key(PEER_A)]


def names_of(reply):
    return [tool["name"] for tool in reply["result"]["tools"]]


async def test_a_request_with_no_peer_and_no_connection_id_is_served_untracked():
    """The peer gate stops a peerless request before it gets here in production; the step
    after it must still do no harm when handed one."""
    server = _server()
    reached = []

    async def inner(scope, receive, send):
        reached.append(request_context.header("mcp-session-id"))

    endpoints = http_endpoints.HttpEndpoints(server, inner, is_localhost_bind=lambda host: True,
                                             era="all")
    headers, body = modern_post("tools/list", 1)

    async def receive():
        return {"type": "http.request", "body": json.dumps(body).encode(), "more_body": False}

    async def send(message):
        raise AssertionError("the inner app answers; the wrapper adds nothing")

    scope = {"type": "http", "method": "POST", "path": "/mcp", "client": None,
             "headers": [(k.encode(), v.encode()) for k, v in headers.items()]}
    await endpoints._forward_to_mcp(scope, receive, send)
    assert reached == [None]
    assert server.http_clients == {}


async def test_a_refused_modern_request_leaves_no_trace(monkeypatch):
    """Under `handshake` the gate answers before any tracking: -32022 with the handshake
    versions, and no row."""
    monkeypatch.setenv(mcp_era.ERA_ENV, "handshake")
    server = _server()
    app = server.mcp_app.http_app()
    async with app.router.lifespan_context(app):
        async with asgi_client(server._wrap_client_tracking(app), PEER_A) as http:
            headers, body = modern_post("tools/list", 1)
            response = await http.post("/mcp", headers=headers, json=body)
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == -32022
    assert error["data"] == {"supported": list(HANDSHAKE_PROTOCOL_VERSIONS),
                             "requested": "2026-07-28"}
    assert server.http_clients == {}


# --- a handshake session is untouched ---------------------------------------------------

async def test_a_handshake_session_is_keyed_by_its_session_id_whatever_else_it_sends():
    """The relay's legacy POSTs: the connection id header beside a real session id changes
    nothing. One row, under the session id the server issued."""
    server = _server()
    app = server.mcp_app.http_app()
    sessions = []

    async def note(response):
        if response.headers.get("mcp-session-id"):
            sessions.append(response.headers["mcp-session-id"])

    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app=server._wrap_client_tracking(app),
                                               client=PEER_A),
                base_url="http://127.0.0.1", event_hooks={"response": [note]},
                headers={mcp_era.CONNECTION_ID_HEADER: uuid.uuid4().hex}) as http:
            transport = streamable_http_client("http://127.0.0.1/mcp", http_client=http)
            async with Client(transport, mode="legacy") as client:
                result = await client.call_tool("tabs", {"action": "list"})
    assert result.is_error is True
    assert set(sessions) and len(set(sessions)) == 1
    assert list(server.http_clients) == [sessions[0]]
    assert _key(PEER_A) not in server.http_clients


# --- real TCP ---------------------------------------------------------------------------

async def test_one_broadcast_reaches_a_get_stream_client_and_a_listen_client(monkeypatch):
    """A change to the tool surface is announced once to a handshake client holding its GET
    stream and once to a 2026-07-28 client holding a subscriptions/listen stream, through
    the production wrapper and uvicorn on its own thread."""
    server = _server()
    tools, roster = server.mcp_tools, Roster(monkeypatch)
    app = GetStreams(server._wrap_client_tracking(tools.mcp.http_app()))
    legacy_heard, listened = Heard(), []
    async with serving(app) as live, http_client() as http_legacy, http_client() as http_modern:
        async with Client(streamable_http_client(live.url, http_client=http_legacy),
                          mode="legacy", message_handler=legacy_heard) as legacy, \
                Client(streamable_http_client(live.url, http_client=http_modern)) as modern:
            assert legacy.protocol_version in HANDSHAKE_PROTOCOL_VERSIONS
            assert modern.protocol_version == "2026-07-28"
            assert names(await legacy.list_tools()) == NINE
            assert names(await modern.list_tools()) == NINE
            await wait_until(lambda: app.open == 1, "the legacy client's GET stream")
            async with modern.listen(tools_list_changed=True) as subscription:
                async def consume():
                    async for event in subscription:
                        listened.append(event)

                reader = asyncio.create_task(consume())
                await asyncio.to_thread(reshape, tools, roster, "two browsers")
                await wait_until(lambda: len(legacy_heard) and listened,
                                 "both clients told", timeout=3)
                await asyncio.sleep(1.0)
                assert len(legacy_heard) == 1, "the GET-stream client hears it once"
                assert listened == [ToolsListChanged()], "the listen client hears it once"
                assert names(await legacy.list_tools()) == NINE + ["browsers"]
                assert names(await modern.list_tools()) == NINE + ["browsers"]

                await asyncio.to_thread(reshape, tools, roster, None)
                await wait_until(lambda: len(legacy_heard) == 2 and len(listened) == 2,
                                 "both clients told of the second change", timeout=3)
                await asyncio.sleep(0.5)
                assert (len(legacy_heard), len(listened)) == (2, 2)
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
    by_connection = {key for key, entry in server.http_clients.items()
                     if modern_identity.peer_key(("127.0.0.1", entry["peer_port"])) == key}
    assert by_connection, "the 2026-07-28 client was tracked under connection keys"
    assert len(set(server.http_clients) - by_connection) == 1, (
        "the handshake client is one session, keyed by the id the server issued")


async def test_a_modern_call_over_tcp_is_filed_under_its_connection():
    """Real sockets, the real peer address: the key is the hash of the peer the server
    saw, and the call's record and verdict are filed under it."""
    server = _server()
    posts = Posts()
    app = server._wrap_client_tracking(server.mcp_app.http_app())
    async with serving(app) as live:
        async with httpx2.AsyncClient(trust_env=False, event_hooks={"request": [posts]},
                                      timeout=httpx2.Timeout(10, read=60)) as http:
            transport = streamable_http_client(live.url, http_client=http)
            async with Client(transport) as client:
                assert client.protocol_version == "2026-07-28"
                result = await client.call_tool("tabs", {"action": "list"})
    assert result.is_error is True
    (request_id,) = posts.calls
    owners = [key for key in server.http_clients
              if (key[:8], str(request_id)) in server.telemetry._calls]
    assert len(owners) == 1, list(server.http_clients)
    (key,) = owners
    assert modern_identity.peer_key(("127.0.0.1", server.http_clients[key]["peer_port"])) == key
    assert server.telemetry._calls[(key[:8], str(request_id))]["outcome"]["code"] \
        == EXTENSION_DISCONNECTED
