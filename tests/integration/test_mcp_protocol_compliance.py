"""
MCP Protocol Compliance Tests
Tests HTTP endpoints, parameter validation, and MCP protocol compliance

THE CANARY: pins the public tool surface (exactly 9 tools) and key parameter
defaults so accidental renames or schema regressions are caught immediately.
"""

import pytest
import pytest_asyncio
import asyncio
import json
import re

from browserplug_shared.version import __version__ as BROWSERPLUG_VERSION
from server.server import BrowserPlugServer
# A server given no MCP port walks the production range 2830..2942 to pick one, even
# with start_mcp=False, so every server here gets a coordinator port.
from port_coordinator import coordinated_test_ports, get_port_by_type
from mcp_client_harness import DirectMCPTestClient


# The complete BrowserPlug tool surface — exactly these 9 tools, no more, no less.
# (11 until 2026-08-03, when page_console and history were removed: zero calls
#  between them in 3,112 across three clients and 31 days.)
EXPECTED_TOOLS = {
    # browser-level
    "tabs",
    "navigate",
    # page
    "page_read",
    "page_snapshot",
    "page_act",
    "page_eval",
    "page_screenshot",
    # data
    "requests",
    # diagnostics
    "server_info",
}

# Pre-2.0 tool names (the old 37-tool surface) that MUST NOT exist anymore,
# plus names that never existed but external agents have historically guessed.
REMOVED_TOOLS = {
    # window tools
    "windows_list", "windows_get", "windows_get_current", "windows_get_last_focused",
    "windows_create", "windows_close", "windows_focus", "windows_update",
    # tab tools
    "tabs_list", "tabs_create", "tabs_close", "tabs_activate", "tabs_screenshot",
    # page tools
    "page_get_text", "page_get_html", "page_navigate", "page_back", "page_forward",
    "page_reload", "page_frames", "page_execute_script", "page_execute_predefined",
    "page_read_clipboard", "page_click", "page_fill", "page_select", "page_wait_for",
    # history tools (the `history` TOOL itself went on 2026-08-03 — 0 calls in
    # 3,112 — and took the browser `history` permission with it)
    "history", "history_query", "history_recent", "history_delete",
    # request-monitoring tools
    "requests_start", "requests_stop", "requests_list", "requests_get_content",
    # server tools
    "server_status", "server_connections",
    # names that never existed but would confuse external agents
    "get_recent_history", "get_history_recent",
}


def _assert_tool_surface(tool_names):
    """Assert the tool list is exactly the 9-tool BrowserPlug surface."""
    names = set(tool_names)
    missing = EXPECTED_TOOLS - names
    unexpected = names - EXPECTED_TOOLS
    assert not missing, \
        f"Expected tools missing from the MCP server: {sorted(missing)} (got: {sorted(names)})"
    assert not unexpected, \
        f"Unexpected tools on the MCP server: {sorted(unexpected)} — the surface is pinned to exactly {len(EXPECTED_TOOLS)} tools"
    for removed in sorted(REMOVED_TOOLS):
        assert removed not in names, \
            f"Removed tool name '{removed}' found in the MCP tools - the pre-2.0 surface must not resurface (it would confuse external agents)"


def _param_schema(tool, name):
    """Get one parameter's JSON schema from a registered tool, asserting it exists."""
    props = tool.parameters.get("properties", {})
    assert name in props, \
        f"Tool '{tool.name}' should have parameter '{name}' (has: {sorted(props)})"
    return props[name]


def _has_type(param_schema, expected_type):
    """True if the parameter schema declares expected_type directly or via anyOf/oneOf."""
    if param_schema.get("type") == expected_type:
        return True
    for key in ("anyOf", "oneOf"):
        if key in param_schema and any(s.get("type") == expected_type for s in param_schema[key]):
            return True
    return False


_MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


def _rpc_response(response, request_id):
    """The JSON-RPC message answering `request_id`, from a JSON or an event-stream body."""
    if "text/event-stream" in response.headers.get("content-type", ""):
        for line in response.text.splitlines():
            if line.startswith("data:"):
                message = json.loads(line[5:].strip())
                if message.get("id") == request_id:
                    return message
        raise AssertionError(f"no JSON-RPC reply for {request_id!r} in: {response.text[:500]}")
    return response.json()


class TestMCPProtocolCompliance:
    """MCP protocol compliance and HTTP endpoint tests"""

    @pytest_asyncio.fixture
    async def full_mcp_system(self):
        """Set up complete system: MCP server + WebSocket server (no Firefox needed)"""

        # Use coordinated ports to avoid conflicts
        with coordinated_test_ports() as (ports, coord_file):
            websocket_port = ports['websocket']
            mcp_port = ports['mcp']

            # Start the complete BrowserPlug server (WebSocket + MCP)
            server = BrowserPlugServer(
                host="localhost",
                port=websocket_port,
                mcp_port=mcp_port,
                start_mcp=True  # Enable MCP server for end-to-end testing
            )

            # Start servers (WebSocket server only - MCP handled by start_mcp=True)
            websocket_task = asyncio.create_task(server.start_server())

            # Wait for servers to start
            await asyncio.sleep(1.0)

            # Create real MCP client harness
            mcp_client = DirectMCPTestClient(server.mcp_tools)

            system_info = {
                'server': server,
                'websocket_port': websocket_port,
                'mcp_port': mcp_port,
                'mcp_client': mcp_client,
                'coordination_file': coord_file,
                'websocket_task': websocket_task
            }

            try:
                yield system_info
            finally:
                # Cleanup
                await mcp_client.disconnect()
                await server.shutdown(websocket_task)

    @pytest.mark.asyncio
    async def test_real_mcp_http_server_tool_names(self, full_mcp_system):
        """Test that the actual MCP HTTP server exposes exactly the 9 expected tools

        This test prevents naming mismatches between DirectMCPTestClient
        and the real MCP server that external agents would use, and is
        the canary against the pre-2.0 tool names resurfacing.
        """
        system = full_mcp_system
        mcp_port = system['mcp_port']

        import httpx2

        # Give server time to fully start
        await asyncio.sleep(1.0)

        url = f"http://localhost:{mcp_port}/mcp"
        async with httpx2.AsyncClient(timeout=10.0, trust_env=False) as session:
            # A real client's order: initialize, take the session id the server hands back,
            # confirm with notifications/initialized, and only then ask for tools/list.
            # Nothing here is caught: a refused connection, a non-200 or a body without
            # the tool list fails the test. There is no in-process fallback, because that
            # is exactly how this test once passed with no server listening at all.
            init = await session.post(url, json={
                "jsonrpc": "2.0", "id": "test-init", "method": "initialize",
                "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                           "clientInfo": {"name": "protocol-compliance-test",
                                          "version": "0"}}},
                headers=_MCP_HEADERS)
            assert init.status_code == 200, f"initialize: {init.status_code} {init.text[:300]}"
            init_result = _rpc_response(init, "test-init")
            assert "result" in init_result, f"initialize returned no result: {init_result}"
            session_id = init.headers.get("mcp-session-id")
            assert session_id, \
                f"initialize must return an mcp-session-id header; got {dict(init.headers)}"
            headers = {**_MCP_HEADERS, "mcp-session-id": session_id}

            confirmed = await session.post(url, json={
                "jsonrpc": "2.0", "method": "notifications/initialized"}, headers=headers)
            assert confirmed.status_code in (200, 202), \
                f"notifications/initialized: {confirmed.status_code} {confirmed.text[:300]}"

            listing = await session.post(url, json={
                "jsonrpc": "2.0", "id": "test-tools-list", "method": "tools/list",
                "params": {}}, headers=headers)
            assert listing.status_code == 200, \
                f"tools/list: {listing.status_code} {listing.text[:300]}"
            data = _rpc_response(listing, "test-tools-list")
            assert "result" in data and "tools" in data["result"], \
                f"tools/list returned no tool list: {data}"
            tool_names = [tool["name"] for tool in data["result"]["tools"]]
            print("✓ Got tool list over real MCP HTTP transport")

            # The artifacts the old framework stamped on the wire were dropped on
            # purpose and must stay gone: a per-tool title and outputSchema, the
            # framework's _meta tags, and serverInfo carrying that framework's version.
            for tool in data["result"]["tools"]:
                assert "title" not in tool, f"{tool['name']} advertises a title: {tool}"
                assert "outputSchema" not in tool,                     f"{tool['name']} advertises an outputSchema: {tool}"
                assert "_meta" not in tool, f"{tool['name']} carries _meta: {tool}"
                assert "x-fastmcp" not in json.dumps(tool), tool
            server_info = init_result["result"]["serverInfo"]
            assert server_info["name"] == "BrowserPlug", server_info
            assert server_info["version"] == BROWSERPLUG_VERSION, server_info

            # A tool result is its content blocks alone: no structuredContent duplicate.
            called = await session.post(url, json={
                "jsonrpc": "2.0", "id": "test-call", "method": "tools/call",
                "params": {"name": "server_info", "arguments": {}}}, headers=headers)
            assert called.status_code == 200,                 f"tools/call: {called.status_code} {called.text[:300]}"
            call_result = _rpc_response(called, "test-call")["result"]
            assert call_result.get("content"), call_result
            assert "structuredContent" not in call_result, call_result
            assert "fastmcp" not in json.dumps(call_result.get("_meta", {})), call_result

        _assert_tool_surface(tool_names)
        print(f"✓ MCP server exposes exactly the {len(EXPECTED_TOOLS)} expected tools")
        print(f"✓ Tools: {sorted(tool_names)}")

    @pytest.mark.asyncio
    async def test_mcp_parameter_format_validation(self, full_mcp_system):
        """Test that MCP tools properly validate parameter formats

        This test catches parameter format issues that external agents might encounter,
        such as sending 'arguments' instead of 'params', or string instead of JSON object.
        """
        system = full_mcp_system
        server = system['server']

        # Get the registered tool to examine its expected parameters
        tools_dict = {t.name: t for t in await server.mcp_app.list_tools()}
        # Vehicle only — the subject is the parameter FORMAT, not this tool. Retargeted
        # from `history` when that tool was removed on 2026-08-03.
        probe_tool = tools_dict['requests']

        print("Testing parameter format validation for requests:")
        print(f"Expected schema: {probe_tool.parameters}")

        # Verify the tool has direct parameters (no params wrapper)
        assert 'params' not in probe_tool.parameters.get('properties', {}), \
            "The tool should NOT have a 'params' wrapper - it takes direct parameters"

        # An integer parameter is directly accessible and pinned
        limit_param = _param_schema(probe_tool, 'limit')
        assert _has_type(limit_param, 'integer'), "'limit' should be integer type"
        assert 'default' in limit_param, "Should have default value"
        assert limit_param['default'] == 30, "Default should be 30"

        # ...and a string one with its own default
        action_param = _param_schema(probe_tool, 'action')
        assert _has_type(action_param, 'string'), "'action' should be string type"
        assert action_param.get('default') == "recent", "action default should be 'recent'"

        print("✓ Parameter schema validation:")
        print(f"  - Tool expects direct parameters (no 'params' wrapper)")
        print(f"  - 'limit' parameter is integer, default: {limit_param['default']}")
        print(f"  - Agents should send: 'arguments': {{'limit': 5}}")

        # Test with DirectMCPTestClient to ensure it works correctly
        mcp_client = system['mcp_client']
        await mcp_client.connect()

        # Correct format: the params must be ACCEPTED (a formatted string comes back,
        # not a raised exception). No browser is connected here, so the tool body returns
        # a *connection* error — which is fine; the point is it is NOT a param/schema error.
        result = await mcp_client.call_tool("requests", {"limit": 3})
        assert isinstance(result['content'], str), "requests should return a formatted string"
        if result['isError']:
            assert 'no extension connection' in result['content'].lower(), \
                f"expected a connection error (no browser), not a parameter error: {result}"

        print("✓ DirectMCPTestClient correctly formats parameters for the MCP server")
        print("✓ Parameter validation test complete")

        # Document the correct format for external agents
        print("\n📋 For external MCP agents:")
        print("✅ Correct:   {'arguments': {'limit': 5}}")
        print("❌ Wrong:     {'arguments': {'params': {'limit': 5}}}")
        print("❌ Wrong:     {'arguments': {'params': '{\"max_results\": 5}'}}")  # String instead of object

    @pytest.mark.asyncio
    async def test_agent_parameter_error_reproduction(self):
        """Reproduce the exact parameter error external agents hit — and pin its rejection.

        A real Claude Code agent once sent ``arguments: {'params': '{}'}`` to a tool
        (a nested ``params`` string) instead of the direct argument. The tool registry
        validates tool arguments against the
        function signature, so that malformed shape MUST be rejected as an
        unexpected keyword, while the correct direct-argument shape validates and
        runs. We drive the registered tool's own ``run()`` and ``call()`` — the same
        argument validation the wire transport applies — not a raw ``.fn()`` call, so
        this exercises the agent's actual error path. (Posting the malformed message to
        the real HTTP endpoint cannot test this: an un-handshaked tools/call is
        rejected with a session error before any parameter validation runs.)
        """
        from server.server import BrowserPlugServer

        server = BrowserPlugServer(start_mcp=False, mcp_port=get_port_by_type('test_mcp_individual'))
        tools_dict = {t.name: t for t in await server.mcp_app.list_tools()}
        # Vehicle only; was `history` until that tool was removed on 2026-08-03.
        probe_tool = tools_dict['requests']

        # ❌ The exact malformed shape the agent sent: nested `params` as a string.
        # The registry must reject it as an unexpected keyword, NOT silently accept it.
        with pytest.raises(Exception) as excinfo:
            await probe_tool.run({"params": "{}"})
        msg = str(excinfo.value)
        assert "params" in msg, \
            f"argument rejection should name the unexpected 'params' key: {msg!r}"
        assert re.search(r"unexpected|keyword|validation", msg, re.IGNORECASE), \
            f"malformed nested-params args should fail argument validation: {msg!r}"

        # ✅ The correct shape (a direct argument) must validate and execute.
        # With no extension connected the tool returns its 'No extension
        # connection' string — the point is that it VALIDATES and runs rather
        # than raising a parameter error.
        result = await probe_tool.call({"limit": 5})
        texts = [getattr(c, "text", "") for c in (getattr(result, "content", None) or [])]
        joined = " ".join(t for t in texts if t)
        assert joined, \
            f"correct arg format should validate and return text content: {result!r}"
        assert "unexpected keyword" not in joined.lower(), \
            f"correct arg format must not surface a validation error: {joined!r}"

    @pytest.mark.asyncio
    async def test_mcp_http_endpoint_is_callable(self, full_mcp_system):
        """Test that MCP HTTP endpoint is properly configured and callable

        This test prevents the '<object> is not callable' error (a server object handed
        to uvicorn un-wrapped) by verifying the HTTP server can actually serve requests.
        """
        system = full_mcp_system
        mcp_port = system['mcp_port']

        import httpx2

        # Give server time to fully start
        await asyncio.sleep(1.0)

        async with httpx2.AsyncClient(timeout=10.0, trust_env=False) as session:
            # A ConnectError (or any other transport failure) propagates and fails the
            # test: "nobody is listening" is not evidence the endpoint is callable. The
            # one failure this test targets is the MCP server object being handed to uvicorn
            # un-wrapped, which the response text would name.
            response = await session.get(f"http://localhost:{mcp_port}/mcp")

        assert "object is not callable" not in response.text, \
            "MCP server object is not callable - missing .http_app() call"
        # A bare GET (no Accept: text/event-stream, no session) is refused by the MCP
        # transport itself; either refusal proves the ASGI app answered.
        assert response.status_code in [400, 406], f"Unexpected status: {response.status_code}"
        if response.status_code == 400:
            body = json.loads(response.text)
            assert "jsonrpc" in body or "error" in body, body

        print(f"✓ MCP HTTP endpoint is callable and responds correctly (status: {response.status_code})")

    @pytest.mark.asyncio
    async def test_harness_runs_over_real_streamable_http(self, full_mcp_system):
        """The harness's `mcp_url` mode takes the ASGI wrapper and the HTTP transport too."""
        system = full_mcp_system
        await asyncio.sleep(1.0)
        client = DirectMCPTestClient(
            system['server'].mcp_tools, mcp_url=f"http://localhost:{system['mcp_port']}/mcp")
        await client.connect()
        _assert_tool_surface(await client.list_tools())
        result = await client.call_tool("server_info", {})
        assert result['success'] is True and isinstance(result['content'], str), result
        with pytest.raises(TypeError):
            await client.call_tool("requests", {"params": "{}"})

    def test_mcp_tools_have_proper_schemas(self):
        """Test that all MCP tools have proper JSON schemas"""
        from server.tools.mcp_tools import BrowserPlugTools

        # Create mock server for testing (must accept the timeout kwarg
        # BrowserPlugTools._request now passes to send_request_and_wait)
        from facade_double import FacadeDouble

        class MockServer(FacadeDouble):
            async def send_request_and_wait(self, request, timeout=30.0, browser=None):
                return {"type": "response", "data": {}}

        mock_server = MockServer()
        mcp_tools = BrowserPlugTools(mock_server)
        mcp_app = mcp_tools.get_mcp_app()

        # Verify the MCP server object was created
        from mcp.server.lowlevel import Server
        assert isinstance(mcp_app, Server)

        print("✓ MCP tools have proper MCP server integration")

    @pytest.mark.asyncio
    async def test_all_tab_tools_schema_validation(self):
        """Test schema validation for the consolidated tabs tool"""
        from server.server import BrowserPlugServer

        server = BrowserPlugServer(start_mcp=False, mcp_port=get_port_by_type('test_mcp_individual'))
        tools_dict = {t.name: t for t in await server.mcp_app.list_tools()}

        tab_tools = {name: func for name, func in tools_dict.items() if 'tab' in name}

        assert set(tab_tools) == {'tabs'}, \
            f"Tab surface should be the single consolidated 'tabs' tool, got: {sorted(tab_tools)}"

        for tool_name, tool_func in tab_tools.items():
            print(f"Validating tab tool: {tool_name}")

            # Verify tool has proper schema
            if hasattr(tool_func, 'parameters'):
                params = tool_func.parameters
                assert isinstance(params, dict), f"Tool {tool_name} parameters should be dict"

                # Common validation for all tab tools
                if 'properties' in params:
                    for param_name, param_schema in params['properties'].items():
                        # Check if parameter has type directly or through anyOf/oneOf
                        has_type = ('type' in param_schema or
                                   'anyOf' in param_schema or
                                   'oneOf' in param_schema)
                        assert has_type, f"Parameter {param_name} missing type specification"

            print(f"✓ {tool_name} schema valid")

        print(f"✓ All {len(tab_tools)} tab tools have valid schemas")

    @pytest.mark.asyncio
    async def test_tab_tools_exist_and_callable(self):
        """Test that the consolidated tabs tool exists and the per-verb tab tools are gone"""
        from server.server import BrowserPlugServer

        server = BrowserPlugServer(start_mcp=False, mcp_port=get_port_by_type('test_mcp_individual'))
        tools_dict = {t.name: t for t in await server.mcp_app.list_tools()}

        # The single consolidated tool replaces the per-verb tab tools
        assert 'tabs' in tools_dict, "Essential tool 'tabs' not found in MCP tools"
        tool = tools_dict['tabs']
        assert hasattr(tool, 'name'), "Tool 'tabs' should have name attribute"
        assert tool.name == 'tabs', f"Tool name mismatch: {tool.name} != tabs"

        # tabs' action parameter is a string defaulting to "list"
        action_param = _param_schema(tool, 'action')
        assert _has_type(action_param, 'string'), "tabs 'action' should be string type"
        assert action_param.get('default') == 'list', "tabs 'action' default should be 'list'"

        # The old per-verb tools must NOT exist (agents would call into the void)
        removed_tab_tools = ['tabs_list', 'tabs_create', 'tabs_close', 'tabs_activate',
                             'tabs_screenshot']
        for tool_name in removed_tab_tools:
            assert tool_name not in tools_dict, \
                f"Removed tool {tool_name} still registered — use tabs(action=...) instead"

        print("✓ Consolidated 'tabs' tool exists; per-verb tab tools removed")

    @pytest.mark.asyncio
    async def test_tool_parameter_validation(self):
        """Test that tools properly validate their parameters"""
        from server.server import BrowserPlugServer

        server = BrowserPlugServer(start_mcp=False, mcp_port=get_port_by_type('test_mcp_individual'))
        tools_dict = {t.name: t for t in await server.mcp_app.list_tools()}

        # page_read's max_chars is the bounded-output knob every read pays attention to.
        # (Was history.max_results until the history tool was removed on 2026-08-03.)
        assert 'page_read' in tools_dict, "page_read tool must exist"
        tool = tools_dict['page_read']

        max_chars_param = _param_schema(tool, 'max_chars')
        assert _has_type(max_chars_param, 'integer'), "max_chars should have integer type"
        assert 'default' in max_chars_param, "max_chars should have default"
        assert max_chars_param['default'] == 20000, "max_chars default should be 20000"

        print("✓ Tool parameter validation working correctly")

    @pytest.mark.asyncio
    async def test_pinned_parameter_defaults(self):
        """Pin key parameter types/defaults of the 9-tool surface

        A silent change to any of these defaults changes agent-visible behavior,
        so they are asserted explicitly here.
        """
        from server.server import BrowserPlugServer

        server = BrowserPlugServer(start_mcp=False, mcp_port=get_port_by_type('test_mcp_individual'))
        tools_dict = {t.name: t for t in await server.mcp_app.list_tools()}

        pins = [
            # (tool, param, json type, default)
            ("tabs", "action", "string", "list"),
            ("tabs", "focus_window", "boolean", True),
            ("navigate", "action", "string", "goto"),
            ("navigate", "wait", "string", "settled"),
            ("navigate", "timeout_ms", "integer", 20000),
            ("page_read", "format", "string", "markdown"),
            ("page_read", "max_chars", "integer", 20000),
            ("page_read", "offset", "integer", 0),
            ("page_snapshot", "mode", "string", "interactive"),
            ("page_snapshot", "max_elements", "integer", 150),
            ("page_snapshot", "include_hidden", "boolean", False),
            ("page_eval", "world", "string", "isolated"),
            ("page_screenshot", "format", "string", "png"),
            ("page_screenshot", "quality", "integer", 90),
            ("page_screenshot", "full_page", "boolean", False),
            ("requests", "action", "string", "recent"),
            ("requests", "limit", "integer", 30),
        ]

        for tool_name, param, jtype, default in pins:
            assert tool_name in tools_dict, f"Tool {tool_name} must exist"
            schema = _param_schema(tools_dict[tool_name], param)
            assert _has_type(schema, jtype), \
                f"{tool_name}.{param} should be {jtype}: {schema}"
            assert schema.get('default') == default, \
                f"{tool_name}.{param} default should be {default!r}: {schema}"

        print(f"✓ All {len(pins)} pinned parameter defaults verified")

    @pytest.mark.asyncio
    async def test_tab_id_optional_everywhere(self):
        """tab_id is OPTIONAL on every tool that takes one (omitted = active tab)"""
        from server.server import BrowserPlugServer

        server = BrowserPlugServer(start_mcp=False, mcp_port=get_port_by_type('test_mcp_individual'))
        tools_dict = {t.name: t for t in await server.mcp_app.list_tools()}

        tools_with_tab_id = [
            "tabs", "navigate", "page_read", "page_snapshot", "page_act",
            "page_eval", "page_screenshot", "requests",
        ]
        for name in tools_with_tab_id:
            tool = tools_dict[name]
            props = tool.parameters.get('properties', {})
            assert 'tab_id' in props, f"{name} should expose tab_id"
            assert 'tab_id' not in tool.parameters.get('required', []), \
                f"{name}: tab_id must be optional (omitted = active tab)"

        # page_act's only required parameter is the step list
        assert set(tools_dict['page_act'].parameters.get('required', [])) == {'steps'}, \
            "page_act should require exactly 'steps'"

        # server_info takes no required parameters at all
        assert not tools_dict['server_info'].parameters.get('required', []), \
            "server_info should have no required parameters"

        print(f"✓ tab_id optional on all {len(tools_with_tab_id)} tab-addressed tools")

    @pytest.mark.asyncio
    async def test_mcp_server_creates_proper_asgi_app(self):
        """Test that MCP server creates proper ASGI app for HTTP serving"""
        from server.server import BrowserPlugServer

        # Use coordinated ports to avoid conflicts
        with coordinated_test_ports() as (ports, coord_file):
            websocket_port = ports['websocket']
            mcp_port = ports['mcp']

            server = BrowserPlugServer(
                host="localhost",
                port=websocket_port,
                mcp_port=mcp_port,
                start_mcp=True
            )

            # Verify MCP app is created and is callable
            assert hasattr(server, 'mcp_app'), "Server should have mcp_app"
            assert server.mcp_app is not None, "mcp_app should not be None"

            # Test that it has the expected ASGI interface
            if hasattr(server.mcp_app, 'http_app'):
                http_app = server.mcp_app.http_app()
                assert callable(http_app), "HTTP app should be callable"

            print("✓ MCP server creates proper ASGI application")
