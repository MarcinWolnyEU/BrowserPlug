"""tools/list is the same in both protocol eras, and carries no x-mcp-header.

Tool descriptions and schemas are a measured budget (test_tools_list_golden.py pins them
byte for byte for the handshake era). A 2026-07-28 client lists the same registry through a
different path: the SDK's per-request entry, with cache hints stamped on the result. These
tests hold that path to the same golden file, in process through the SDK client and as raw
bytes over the production wrapper, and they pin the one property the relay's header
synthesis relies on: no tool schema asks for `Mcp-Param-*` headers.

The modern entry runs the server's own tools/list before every tools/call that has
arguments, to look up `x-mcp-header` in the tool's schema (the SDK validates the headers it
finds). BrowserPlug's schemas carry none, so that walk validates nothing, and a client, or
the stdio relay, never has to send an `Mcp-Param-*` header. A schema that gained one would
make every such call fail with -32020 until the sender added the header.
"""

import dataclasses
import json

import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

import test_imports  # noqa: F401  (path setup)
from browserplug_shared import mcp_era
from server.server import BrowserPlugServer
from server.tools.mcp_server import BrowserPlugMCPServer
from server.tools.mcp_tools import BrowserPlugTools
from server.tools.tool_registry import ToolRegistry, ToolSpec
from test_list_changed import raw_session
from test_tools_list_golden import GOLDEN, _browser, _Server

X_MCP_HEADER = "x-mcp-header"
SITUATIONS = ["default", "proxy_granted", "scripts_enabled", "two_browsers"]


@pytest.fixture(autouse=True)
def default_era(monkeypatch):
    monkeypatch.delenv(mcp_era.ERA_ENV, raising=False)


def build(monkeypatch, situation):
    """The BrowserPlugTools of one of the golden's four situations."""
    monkeypatch.delenv("BROWSERPLUG_EXT_SCRIPTS", raising=False)
    if situation == "default":
        return BrowserPlugTools(_Server())
    if situation == "proxy_granted":
        tools = BrowserPlugTools(_Server(proxy={"has_proxy_permission": True}))
        tools.refresh_proxy_tool_visibility()
        return tools
    if situation == "scripts_enabled":
        monkeypatch.setenv("BROWSERPLUG_EXT_SCRIPTS", "/some/scripts")
        return BrowserPlugTools(_Server())
    two = [_browser(1, "firefox-dev", "Firefox Developer Edition", "gecko"),
           _browser(2, "chrome", "Chrome", "blink")]
    return BrowserPlugTools(_Server(browsers=two))


def golden(situation):
    return json.loads(GOLDEN.read_text(encoding="utf-8"))[situation]


def dumped(listed):
    """The golden's form: each Tool as model_dump(mode="json", exclude_none=True), by name."""
    return [t.model_dump(mode="json", exclude_none=True)
            for t in sorted(listed.tools, key=lambda t: t.name)]


def has_key(node, key):
    """Whether `key` appears anywhere in a JSON value, as a mapping key at any depth."""
    if isinstance(node, dict):
        return key in node or any(has_key(v, key) for v in node.values())
    if isinstance(node, list):
        return any(has_key(v, key) for v in node)
    return False


# --- the golden, in both eras -----------------------------------------------------------

@pytest.mark.parametrize("situation", SITUATIONS)
async def test_both_eras_list_what_the_golden_pins(monkeypatch, situation):
    tools = build(monkeypatch, situation)
    want = golden(situation)
    async with Client(tools.mcp, mode="legacy") as legacy:
        handshake = await legacy.list_tools()
    async with Client(tools.mcp) as modern:
        assert modern.protocol_version == "2026-07-28"
        per_request = await modern.list_tools()
    for era, listed in (("handshake", handshake), ("2026-07-28", per_request)):
        assert [t.name for t in listed.tools] == want["order"], f"{era}: order"
        assert dumped(listed) == want["tools"], f"{era}: descriptions or schemas"


@pytest.mark.parametrize("situation", SITUATIONS)
async def test_the_signature_the_push_compares_names_the_golden_surface(monkeypatch, situation):
    """A changed surface is announced by its tools_signature, and the golden pins that
    value per situation: a push means "the surface the golden pins is not what you hold"."""
    assert build(monkeypatch, situation).tools_signature == golden(situation)["signature"]


def _server():
    return BrowserPlugServer(host="localhost", port=8772, start_mcp=False, ws_token="a" * 43)


def _post(method, msg_id, **headers):
    meta = {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientInfo": {"name": "cross-era", "version": "1"},
            "io.modelcontextprotocol/clientCapabilities": {}}
    return ({"content-type": "application/json",
             "accept": "application/json, text/event-stream",
             "mcp-protocol-version": "2026-07-28", "mcp-method": method, **headers},
            {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": {"_meta": meta}})


def _wire_tools(response):
    assert response.status_code == 200, response.text
    if response.headers["content-type"].startswith("application/json"):
        message = response.json()
    else:
        (message,) = [json.loads(line[len("data:"):]) for line in response.text.splitlines()
                      if line.startswith("data:")]
    return message["result"]


async def test_the_raw_wire_carries_the_same_tools_in_both_eras():
    """Over the production wrapper: the `tools` array a handshake session receives and the
    one a 2026-07-28 request receives are the same bytes, and the registry's own."""
    server = _server()
    app = server.mcp_app.http_app()
    url = "http://127.0.0.1/mcp"
    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app=server._wrap_client_tracking(app),
                                               client=("127.0.0.1", 41101)),
                base_url="http://127.0.0.1") as http:
            session = await raw_session(http, url)
            legacy = _wire_tools(await http.post(
                url, headers=session,
                json={"jsonrpc": "2.0", "id": 5, "method": "tools/list"}))
            headers, body = _post("tools/list", 6)
            modern = _wire_tools(await http.post(url, headers=headers, json=body))
    assert modern["tools"] == legacy["tools"] == server.mcp_tools.registry.wire_tools()
    # What the 2026 era adds around the array, and the SDK fills in itself.
    assert modern["resultType"] == "complete"
    assert {"ttlMs", "cacheScope"} <= set(modern)


# --- no x-mcp-header ---------------------------------------------------------------------

@pytest.mark.parametrize("situation", SITUATIONS)
async def test_no_tool_schema_carries_x_mcp_header(monkeypatch, situation):
    tools = build(monkeypatch, situation)
    wire = tools.mcp.registry.wire_tools()
    assert wire, "an empty list proves nothing"
    offenders = [tool["name"] for tool in wire if has_key(tool, X_MCP_HEADER)]
    assert offenders == [], (
        f"{offenders} ask clients for Mcp-Param-* headers; the stdio relay sends none, and "
        f"every modern call to such a tool with arguments would be refused -32020")
    assert not has_key(golden(situation), X_MCP_HEADER)


def _registry_with_x_mcp_header():
    async def probe(region: str = "") -> str:
        return region

    spec = ToolSpec.from_function(probe)
    schema = spec.parameters
    schema["properties"]["region"][X_MCP_HEADER] = "Region"
    return ToolRegistry([dataclasses.replace(spec, schema_json=json.dumps(schema))])


def test_the_walk_finds_x_mcp_header_wherever_a_schema_nests_it():
    """So that a clean result above means something."""
    assert has_key(_registry_with_x_mcp_header().wire_tools(), X_MCP_HEADER)
    assert has_key({"a": [{"b": {X_MCP_HEADER: 1}}]}, X_MCP_HEADER)
    assert not has_key({"a": [{"b": {"x-other": 1}}], "c": X_MCP_HEADER}, X_MCP_HEADER)


async def test_a_schema_with_x_mcp_header_makes_the_sdk_demand_the_header():
    """Why the property matters: the SDK reads `x-mcp-header` from the schema and refuses
    a modern tools/call with arguments that lacks the matching Mcp-Param-* header, which
    the stdio relay does not send."""
    mcp = BrowserPlugMCPServer(_registry_with_x_mcp_header())
    app = mcp.http_app()
    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),
                                      base_url="http://127.0.0.1") as http:
            headers, body = _post("tools/call", 3, **{"mcp-name": "probe"})
            body["params"].update({"name": "probe", "arguments": {"region": "eu"}})
            refused = await http.post("/mcp", headers=headers, json=body)
            granted = await http.post(
                "/mcp", headers={**headers, "mcp-param-region": "eu"}, json=body)
    assert refused.status_code == 400, refused.text
    assert granted.status_code == 200, granted.text


async def test_a_modern_call_with_arguments_needs_no_param_headers():
    """The SDK's hidden listing before a tools/call with arguments finds nothing to
    validate, so a request carrying only the routing headers is served."""
    server = _server()
    app = server.mcp_app.http_app()
    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app=server._wrap_client_tracking(app),
                                               client=("127.0.0.1", 41102)),
                base_url="http://127.0.0.1") as http:
            transport = streamable_http_client("http://127.0.0.1/mcp", http_client=http)
            async with Client(transport) as client:
                assert client.protocol_version == "2026-07-28"
                result = await client.call_tool("tabs", {"action": "list"})
    assert result.is_error is True
    assert "No extension connection available" in result.content[0].text
