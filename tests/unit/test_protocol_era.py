"""server/rpc/protocol_era.py: the era policy behind /mcp and the duplex transports.

`mcp.era` (browserplug_shared/mcp_era.py) decides whether 2026-07-28 is served. Under
`direct` and `all` a request carrying that version header goes through to the SDK. Under
`handshake` it is refused with -32022 naming the handshake versions only, the answer
clients in auto mode fall back from. A request with no version header that carries the
2026-07-28 envelope and no session (what a stdio relay without era headers forwards for a
server/discover probe) is refused under every setting.

The pure tests pin the verdict, the ASGI tests the wire behaviour, and the last ones drive
the real MCP app through the real wrapper with the SDK's own auto-negotiating client, and
`run_stdio` over a pipe pair, because a gate that is correct but never called, or an
answer the client does not read as "fall back", passes every pure test above them.
"""

import asyncio
import json
import logging
import socket
from contextlib import asynccontextmanager

import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.server.lowlevel import Server
from mcp.shared.memory import create_client_server_memory_streams
from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS, MODERN_PROTOCOL_VERSIONS

import test_imports  # noqa: F401  (path setup)
from browserplug_shared import mcp_era
from server.rpc import protocol_era
from server.server import BrowserPlugServer
from server.tools.mcp_server import BrowserPlugMCPServer
from server.tools.tool_registry import ToolRegistry
from server.web import modern_identity, request_context
from test_list_changed import stdio_pipe

TOKEN = "a" * 43
ENVELOPE = {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientCapabilities": {}}
ERAS = list(mcp_era.ERA_VALUES)
SERVES_MODERN = ["direct", "all"]
HANDSHAKE_LIST = list(HANDSHAKE_PROTOCOL_VERSIONS)
EVERY_VERSION = list(HANDSHAKE_PROTOCOL_VERSIONS) + list(MODERN_PROTOCOL_VERSIONS)
PEER = ("127.0.0.1", 5555)


@pytest.fixture(autouse=True)
def no_era_in_the_environment(monkeypatch):
    """A developer's shell may set BROWSERPLUG_MCP_ERA; each test sets the era it means."""
    monkeypatch.delenv(mcp_era.ERA_ENV, raising=False)


def body(method, msg_id=1, envelope=False, **params):
    if envelope:
        params["_meta"] = ENVELOPE
    return json.dumps({"jsonrpc": "2.0", "id": msg_id, "method": method,
                       "params": params}).encode()


def hdrs(**kw):
    return {k.replace("_", "-").lower().encode(): v.encode() for k, v in kw.items()}


# --- the verdict ------------------------------------------------------------------------

def test_the_versions_served_depend_on_the_era():
    assert protocol_era.supported_versions("handshake") == tuple(HANDSHAKE_LIST)
    for era in SERVES_MODERN:
        assert protocol_era.supported_versions(era) == tuple(EVERY_VERSION)


@pytest.mark.parametrize("value,modern", [
    ("2026-07-28", True), (" 2026-07-28 ", True), ("2099-01-01", True),
    ("2025-11-25", False), ("2024-11-05", False), ("", False), (None, False), ("  ", False)])
def test_is_modern_version_is_the_sdks_routing_rule(value, modern):
    """The SDK sends a request to its stateless entry when the header holds anything
    outside the handshake versions. A missing or empty header is not a modern request."""
    assert protocol_era.is_modern_version(value) is modern


@pytest.mark.parametrize("era", ERAS)
@pytest.mark.parametrize("version", HANDSHAKE_LIST)
def test_a_handshake_header_passes_under_every_era(era, version):
    assert protocol_era.refusal(hdrs(MCP_Protocol_Version=version), None, era) is None


@pytest.mark.parametrize("era", SERVES_MODERN)
def test_a_2026_07_28_header_passes_when_the_era_serves_it(era):
    assert protocol_era.refusal(hdrs(MCP_Protocol_Version="2026-07-28"), None, era) is None


def test_a_2026_07_28_header_is_refused_under_handshake_naming_handshake_versions_only():
    refused = protocol_era.refusal(hdrs(MCP_Protocol_Version="2026-07-28"), None, "handshake")
    assert refused == protocol_era.Refusal("2026-07-28", "header", tuple(HANDSHAKE_LIST))


@pytest.mark.parametrize("era", ERAS)
@pytest.mark.parametrize("version", ["2099-01-01", "", "2026-07-29", "banana"])
def test_an_unknown_header_is_refused_naming_every_version_the_era_serves(era, version):
    refused = protocol_era.refusal(hdrs(MCP_Protocol_Version=version), None, era)
    assert refused == protocol_era.Refusal(version, "header",
                                           protocol_era.supported_versions(era))


@pytest.mark.parametrize("era", ERAS)
def test_initialize_passes_whatever_version_it_proposes(era):
    """The server counter-offers 2025-11-25 to an initialize proposing 2026-07-28."""
    b = body("initialize", protocolVersion="2026-07-28")
    assert protocol_era.refusal({}, b, era) is None
    assert protocol_era.refusal({}, body("initialize", envelope=True), era) is None


@pytest.mark.parametrize("era", ERAS)
def test_an_enveloped_request_without_a_header_or_session_is_refused_under_every_era(era):
    """What the stdio relay forwards for a stdio client probing server/discover. The list
    names handshake versions only, whatever the era: it is the answer clients read as
    "this server is handshake-only", and an old relay cannot carry the modern era."""
    refused = protocol_era.refusal({}, body("server/discover", envelope=True), era)
    assert refused == protocol_era.Refusal("2026-07-28", "envelope", tuple(HANDSHAKE_LIST))


@pytest.mark.parametrize("era", ERAS)
def test_an_envelope_naming_a_handshake_version_passes(era):
    """Refusing it would send -32022 listing the very version it asked for."""
    b = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {
        "_meta": {"io.modelcontextprotocol/protocolVersion": "2025-11-25"}}}).encode()
    assert protocol_era.refusal({}, b, era) is None


@pytest.mark.parametrize("era", ERAS)
def test_an_enveloped_request_inside_a_session_passes(era):
    assert protocol_era.refusal(hdrs(Mcp_Session_Id="abc"),
                                body("tools/list", envelope=True), era) is None


@pytest.mark.parametrize("era", ERAS)
@pytest.mark.parametrize("raw", [b"", b"not json", b"[1, 2]", body("tools/list")])
def test_anything_else_without_a_header_passes(era, raw):
    assert protocol_era.refusal({}, raw, era) is None


@pytest.mark.parametrize("era,passes", [("handshake", False), ("direct", True), ("all", True)])
def test_a_session_header_beside_a_2026_07_28_header_does_not_change_the_verdict(era, passes):
    refused = protocol_era.refusal(
        hdrs(MCP_Protocol_Version="2026-07-28", Mcp_Session_Id="abc"), None, era)
    assert (refused is None) is passes


# --- the ASGI gate ----------------------------------------------------------------------

async def drive(gate_or_app, method, path, headers, payload, chunks=1, parts=None):
    sent = []
    if parts is None:
        size = -(-len(payload) // chunks) or 1
        parts = [payload[i * size:(i + 1) * size] for i in range(chunks)]
    queue = [{"type": "http.request", "body": p, "more_body": i < len(parts) - 1}
             for i, p in enumerate(parts)]

    async def receive():
        return queue.pop(0) if queue else {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    pairs = headers.items() if hasattr(headers, "items") else headers
    scope = {"type": "http", "method": method, "path": path, "client": PEER,
             "headers": list(pairs)}
    result = await gate_or_app(scope, receive, send)
    return result, sent


async def drive_gate(era, method, path, headers, payload, **kw):
    async def gate(scope, receive, send):
        return await protocol_era.gate(scope, receive, send, era)
    return await drive(gate, method, path, headers, payload, **kw)


def response(sent):
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    raw = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, json.loads(raw)


@pytest.mark.asyncio
async def test_a_refusal_under_handshake_is_400_with_the_request_id_and_handshake_versions_only():
    (handled, _), sent = await drive_gate("handshake", "POST", "/mcp",
                                          hdrs(MCP_Protocol_Version="2026-07-28"),
                                          body("server/discover", msg_id=42, envelope=True))
    assert handled is True
    status, reply = response(sent)
    assert status == 400
    assert reply["id"] == 42
    assert reply["error"]["code"] == -32022
    assert reply["error"]["message"] == "Unsupported protocol version"
    assert reply["error"]["data"] == {"supported": HANDSHAKE_LIST, "requested": "2026-07-28"}
    assert not any(v.startswith("2026") for v in reply["error"]["data"]["supported"])


@pytest.mark.asyncio
@pytest.mark.parametrize("era", SERVES_MODERN)
async def test_an_unknown_version_over_http_gets_every_served_version(era):
    (handled, _), sent = await drive_gate(era, "POST", "/mcp",
                                          hdrs(MCP_Protocol_Version="2099-01-01"),
                                          body("tools/list", msg_id="x", envelope=True))
    assert handled is True
    status, reply = response(sent)
    assert (status, reply["id"], reply["error"]["code"]) == (400, "x", -32022)
    assert reply["error"]["data"] == {"supported": EVERY_VERSION, "requested": "2099-01-01"}


@pytest.mark.asyncio
@pytest.mark.parametrize("era,version", [("all", "2026-07-28"), ("direct", "2026-07-28"),
                                         ("all", "2025-11-25"), ("handshake", "2025-11-25")])
async def test_a_request_whose_header_is_admitted_is_not_read_by_the_gate(era, version):
    """The verdict depends on the header alone, so the body streams to the SDK untouched
    and an id is owed only to a refusal."""
    taken = []

    async def receive():
        taken.append(1)
        return {"type": "http.request", "body": b"{}", "more_body": False}

    async def send(message):
        raise AssertionError("an admitted request gets no answer from the gate")

    scope = {"type": "http", "method": "POST", "path": "/mcp", "client": PEER,
             "headers": list(hdrs(MCP_Protocol_Version=version).items())}
    handled, passed_on = await protocol_era.gate(scope, receive, send, era)
    assert handled is False
    assert passed_on is receive and taken == []


@pytest.mark.asyncio
@pytest.mark.parametrize("versions,refused", [(("2026-07-28", "2025-11-25"), True),
                                              (("2025-11-25", "2026-07-28"), False)])
async def test_the_first_of_two_version_headers_decides_as_the_sdk_routes_it(versions, refused):
    """The SDK takes the first MCP-Protocol-Version. A gate that took the last would let
    `2026-07-28, 2025-11-25` through under `handshake`, and the SDK would then serve it
    modern."""
    pairs = [(b"mcp-protocol-version", v.encode()) for v in versions]
    (handled, _), sent = await drive_gate("handshake", "POST", "/mcp", pairs,
                                          body("tools/list", envelope=True))
    assert handled is refused
    if refused:
        assert response(sent)[1]["error"]["data"]["requested"] == "2026-07-28"


@pytest.mark.asyncio
async def test_a_passed_request_replays_every_byte_it_read():
    payload = body("initialize", protocolVersion="2025-11-25")
    (handled, replay), sent = await drive_gate("all", "POST", "/mcp", {}, payload, chunks=3)
    assert handled is False and sent == []
    got = b""
    while True:
        m = await replay()
        got += m.get("body", b"")
        if not m.get("more_body"):
            break
    assert got == payload


async def read_all(replay):
    got = b""
    while True:
        m = await replay()
        got += m.get("body", b"")
        if not m.get("more_body"):
            return got


@pytest.mark.asyncio
@pytest.mark.parametrize("era", ERAS)
async def test_a_headerless_envelope_split_across_messages_is_refused(era):
    """The stdio relay's server/discover probe: no header, no session, the era only in the
    body. Chunked delivery with an empty terminator is how ASGI servers hand it over."""
    payload = body("server/discover", msg_id="probe-7", envelope=True)
    half = len(payload) // 2
    (handled, _), sent = await drive_gate(era, "POST", "/mcp", {}, payload,
                                          parts=[payload[:half], payload[half:], b""])
    assert handled is True
    status, reply = response(sent)
    assert status == 400
    assert reply["id"] == "probe-7"
    assert reply["error"]["code"] == -32022
    assert reply["error"]["data"]["supported"] == HANDSHAKE_LIST


@pytest.mark.asyncio
async def test_a_body_too_large_to_be_a_probe_streams_through_whole():
    big = body("tools/call", name="page_eval", code="x" * (protocol_era.MAX_PROBE_BODY * 2))
    (handled, replay), sent = await drive_gate("all", "POST", "/mcp", {}, big, chunks=8)
    assert handled is False and sent == []
    assert await read_all(replay) == big


@pytest.mark.asyncio
async def test_a_declared_oversize_body_is_not_read_at_all():
    big = body("tools/list", envelope=True)
    headers = hdrs(Content_Length=str(protocol_era.MAX_PROBE_BODY + 1))
    (handled, replay), sent = await drive_gate("all", "POST", "/mcp", headers, big)
    assert handled is False and sent == []
    assert await read_all(replay) == big  # the untouched receive, nothing consumed


@pytest.mark.asyncio
async def test_a_refusal_is_logged(caplog):
    with caplog.at_level(logging.INFO, logger=protocol_era.__name__):
        await drive_gate("all", "POST", "/mcp", {}, body("server/discover", envelope=True))
        await drive_gate("handshake", "POST", "/mcp", hdrs(MCP_Protocol_Version="2026-07-28"),
                         body("tools/list", envelope=True))
    messages = [r.getMessage() for r in caplog.records]
    assert any("Refused MCP protocol 2026-07-28 (envelope)" in m for m in messages), messages
    assert any("Refused MCP protocol 2026-07-28 (header)" in m for m in messages), messages


@pytest.mark.asyncio
async def test_non_mcp_paths_are_not_touched():
    (handled, _), sent = await drive_gate("handshake", "POST", "/timing",
                                          hdrs(MCP_Protocol_Version="2026-07-28"), b"{}")
    assert handled is False and sent == []


# --- wiring: the real wrapper, then the real app ----------------------------------------

def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _server(**kw):
    return BrowserPlugServer(host="localhost", port=_free_port(), start_mcp=False,
                             ws_token=TOKEN, **kw)


@pytest.mark.asyncio
@pytest.mark.parametrize("era", ERAS)
async def test_the_wrapper_applies_the_era_before_the_mcp_app(monkeypatch, era):
    monkeypatch.setenv(mcp_era.ERA_ENV, era)
    server = _server()
    reached = []

    async def inner(scope, receive, send):
        reached.append((scope["path"], request_context.header("mcp-session-id"),
                        dict(scope["headers"]).get(b"mcp-session-id")))

    app = server._wrap_client_tracking(inner)

    _, sent = await drive(app, "POST", "/mcp", hdrs(MCP_Protocol_Version="2026-07-28"),
                          body("tools/list", envelope=True))
    if era == "handshake":
        assert response(sent)[0] == 400
        assert reached == [], "under handshake a 2026-07-28 request never reaches the SDK"
    else:
        key = modern_identity.peer_key(PEER)
        assert reached == [("/mcp", key, None)], (
            "the connection key is bound for the tool layer and is not added to the scope")
    reached.clear()

    _, sent = await drive(app, "POST", "/mcp", {}, body("server/discover", envelope=True))
    assert response(sent)[0] == 400
    assert reached == [], "a headerless 2026-07-28 envelope never reaches the SDK"

    await drive(app, "POST", "/mcp", {}, body("initialize", protocolVersion="2025-11-25"))
    assert reached == [("/mcp", None, None)], "a handshake request is not keyed"


@pytest.mark.asyncio
async def test_the_era_is_read_once_when_the_wrapper_is_built(monkeypatch):
    """A change to the setting takes a restart, as documented: the wrapper a running
    uvicorn serves was built under the old one."""
    monkeypatch.setenv(mcp_era.ERA_ENV, "all")
    server = _server()
    reached = []

    async def inner(scope, receive, send):
        reached.append(scope["path"])

    before = server._wrap_client_tracking(inner)
    monkeypatch.setenv(mcp_era.ERA_ENV, "handshake")
    after = server._wrap_client_tracking(inner)
    modern = hdrs(MCP_Protocol_Version="2026-07-28")
    await drive(before, "POST", "/mcp", modern, body("tools/list", envelope=True))
    assert reached == ["/mcp"]
    _, sent = await drive(after, "POST", "/mcp", modern, body("tools/list", envelope=True))
    assert response(sent)[0] == 400 and reached == ["/mcp"]


@pytest.mark.asyncio
async def test_the_wrapper_logs_the_era_it_serves_and_a_value_it_skipped(monkeypatch, caplog):
    monkeypatch.setenv(mcp_era.ERA_ENV, "modern")
    with caplog.at_level(logging.INFO, logger=protocol_era.__name__):
        _server()._wrap_client_tracking(lambda *a: None)
    messages = [r.getMessage() for r in caplog.records]
    assert "MCP protocol era: all (default)" in messages, messages
    assert ("Ignored BROWSERPLUG_MCP_ERA=modern: not one of handshake, direct, all"
            in messages), messages


def _wrapped_app(server, seen_versions):
    """The real MCP app behind the real wrapper, for the SDK's own auto-negotiating client
    to drive in process. Returns (the Starlette app, the wrapper recording each request's
    MCP-Protocol-Version in `seen_versions`)."""
    starlette_app = server.mcp_app.http_app()
    wrapped = server._wrap_client_tracking(starlette_app)

    async def app(scope, receive, send):
        if scope["type"] == "http":
            seen_versions.append(dict(scope.get("headers") or []).get(b"mcp-protocol-version"))
        await wrapped(scope, receive, send)

    return starlette_app, app


@pytest.mark.asyncio
@pytest.mark.parametrize("era", SERVES_MODERN)
async def test_the_sdk_auto_client_ends_on_2026_07_28_with_a_keyed_connection(monkeypatch, era):
    """The era `direct` and `all` serve: the probe is answered, no session is ever
    opened, and the connection is still tracked, under its key."""
    monkeypatch.setenv(mcp_era.ERA_ENV, era)
    server = _server()
    seen_versions = []
    starlette_app, app = _wrapped_app(server, seen_versions)
    async with starlette_app.router.lifespan_context(starlette_app):
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app, client=PEER),
                                      base_url="http://127.0.0.1") as http:
            transport = streamable_http_client("http://127.0.0.1/mcp", http_client=http)
            async with Client(transport, mode="auto") as client:
                assert client.protocol_version == "2026-07-28"
                tools = await client.list_tools()
                assert "server_info" in {t.name for t in tools.tools}
    assert set(seen_versions) == {b"2026-07-28"}, "no handshake-era request was made"
    assert list(server.http_clients) == [modern_identity.peer_key(PEER)], (
        "the connection reached track_http_client under its key")


@pytest.mark.asyncio
async def test_under_handshake_the_sdk_auto_client_falls_back_to_a_session(monkeypatch):
    """mcp's Client in its default auto mode probes server/discover at 2026-07-28 first.
    Through the gate set to `handshake` it must end up on a handshake version, holding a
    session id."""
    monkeypatch.setenv(mcp_era.ERA_ENV, "handshake")
    server = _server()
    seen_versions = []
    starlette_app, app = _wrapped_app(server, seen_versions)
    async with starlette_app.router.lifespan_context(starlette_app):
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app, client=PEER),
                                      base_url="http://127.0.0.1") as http:
            transport = streamable_http_client("http://127.0.0.1/mcp", http_client=http)
            async with Client(transport, mode="auto") as client:
                assert client.protocol_version in HANDSHAKE_PROTOCOL_VERSIONS
                tools = await client.list_tools()
                assert "server_info" in {t.name for t in tools.tools}
    assert b"2026-07-28" in seen_versions, "the client never probed; the test proves nothing"
    assert server.http_clients, "the session never reached track_http_client"
    assert modern_identity.peer_key(PEER) not in server.http_clients, (
        "a handshake session is keyed by its session id, never by its connection")


# --- the duplex valve: serve_duplex through run_stdio -----------------------------------

def _tools(**kw):
    return _server(**kw).mcp_tools


@pytest.mark.asyncio
@pytest.mark.parametrize("era,negotiated_modern", [("handshake", False), ("direct", True),
                                                   ("all", True)])
async def test_run_stdio_serves_the_era_the_setting_allows(monkeypatch, era, negotiated_modern):
    """--stdio under `handshake` runs the handshake loop only: the auto client's
    server/discover probe gets -32601 and it falls back to initialize. Under `direct` and
    `all` the SDK's dual-era loop runs and the same client ends on 2026-07-28."""
    monkeypatch.setenv(mcp_era.ERA_ENV, era)
    tools = _tools()
    async with stdio_pipe(monkeypatch, tools.mcp) as transport:
        async with Client(transport, mode="auto") as client:
            if negotiated_modern:
                assert client.protocol_version == "2026-07-28"
            else:
                assert client.protocol_version in HANDSHAKE_PROTOCOL_VERSIONS
            assert "server_info" in {t.name for t in (await client.list_tools()).tools}


@pytest.mark.asyncio
async def test_run_stdio_under_handshake_still_serves_a_legacy_client(monkeypatch):
    monkeypatch.setenv(mcp_era.ERA_ENV, "handshake")
    tools = _tools()
    async with stdio_pipe(monkeypatch, tools.mcp) as transport:
        async with Client(transport, mode="legacy") as client:
            assert client.protocol_version in HANDSHAKE_PROTOCOL_VERSIONS
            assert client.server_info.name == "BrowserPlug"
            assert "server_info" in {t.name for t in (await client.list_tools()).tools}


@pytest.mark.asyncio
async def test_run_stdio_logs_the_era_it_serves(monkeypatch, caplog):
    monkeypatch.setenv(mcp_era.ERA_ENV, "handshake")
    tools = _tools()
    with caplog.at_level(logging.INFO, logger=protocol_era.__name__):
        async with stdio_pipe(monkeypatch, tools.mcp) as transport:
            async with Client(transport, mode="legacy"):
                pass
    assert "MCP protocol era: handshake (env)" in [r.getMessage() for r in caplog.records]


def test_the_valve_is_not_an_override_of_server_run():
    """The era is applied in run_stdio, through serve_duplex. An override of run() would
    put every caller of it, the in-process legacy transport included, behind the setting."""
    assert BrowserPlugMCPServer.run is Server.run


@pytest.mark.asyncio
async def test_the_in_process_client_is_outside_the_valve(monkeypatch):
    """`Client(tools.mcp)` in auto mode drives the SDK's per-request path directly: the
    unit suite's in-process sites are modern whatever the setting says."""
    monkeypatch.setenv(mcp_era.ERA_ENV, "handshake")
    tools = _tools()
    async with Client(tools.mcp) as client:
        assert client.protocol_version == "2026-07-28"


@asynccontextmanager
async def run_pipe(server):
    """`server.run(...)` serving one end of an in-memory pipe, the call the in-process
    legacy transport and ProxyServer make; yields a client transport for the other end."""
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        serving = asyncio.create_task(server.run(
            server_streams[0], server_streams[1], server.create_initialization_options()))

        @asynccontextmanager
        async def transport():
            try:
                yield client_streams
            finally:
                await client_streams[1].aclose()

        try:
            yield transport()
            await asyncio.wait_for(serving, 10)
        finally:
            if not serving.done():
                serving.cancel()
                await asyncio.gather(serving, return_exceptions=True)


class RunsThroughServeDuplex(BrowserPlugMCPServer):
    """What ProxyServer.run does: its own run() hands the connection to serve_duplex."""

    era = None

    async def run(self, read_stream, write_stream, initialization_options,
                  raise_exceptions=False):
        await protocol_era.serve_duplex(self, read_stream, write_stream,
                                        initialization_options, raise_exceptions,
                                        era=self.era)


@pytest.mark.asyncio
@pytest.mark.parametrize("era,negotiated_modern", [("handshake", False), ("direct", True),
                                                   ("all", True)])
async def test_a_server_whose_run_calls_serve_duplex_does_not_recurse(monkeypatch, era,
                                                                      negotiated_modern):
    """serve_duplex calls Server.run itself. Through `self.run` it would re-enter the
    override that called it, and recurse until the stack ran out. The server task dies
    then and the client waits for a reply that never comes, hence the bound."""
    monkeypatch.setenv(mcp_era.ERA_ENV, era)
    async with asyncio.timeout(15), run_pipe(RunsThroughServeDuplex(ToolRegistry())) as transport:
        async with Client(transport, mode="auto") as client:
            assert (client.protocol_version == "2026-07-28") is negotiated_modern
            assert client.protocol_version in EVERY_VERSION
            assert (await client.list_tools()).tools == []


@pytest.mark.asyncio
async def test_an_era_passed_to_serve_duplex_outranks_the_setting(monkeypatch):
    monkeypatch.setenv(mcp_era.ERA_ENV, "all")
    server = RunsThroughServeDuplex(ToolRegistry())
    server.era = "handshake"
    async with asyncio.timeout(15), run_pipe(server) as transport:
        async with Client(transport, mode="auto") as client:
            assert client.protocol_version in HANDSHAKE_PROTOCOL_VERSIONS
