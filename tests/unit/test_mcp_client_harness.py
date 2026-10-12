"""The integration harness goes through a real MCP client, not `tool.fn`.

`tests/mcp_client_harness.py` used to call the tool function directly, which skipped
JSON-RPC, argument validation and the server's outcome policy. These tests pin the three things
that only exist on the protocol path, so a harness that slips back below it fails here in
seconds instead of passing quietly through the 37-minute sweep.
"""

import pytest

import test_imports  # noqa: F401  (path setup)
from facade_double import FacadeDouble
from mcp_client_harness import DirectMCPTestClient, ToolArgumentError
from server.tools.mcp_tools import BrowserPlugTools


class _Server(FacadeDouble):
    def __init__(self, response=None):
        super().__init__()
        self.sent = []
        self.response = response or {"type": "response", "data": {}}

    async def send_request_and_wait(self, request, timeout=30.0, browser=None):
        self.sent.append(request)
        return self.response


async def _client(server):
    client = DirectMCPTestClient(BrowserPlugTools(server))
    await client.connect()
    return client


async def test_json_step_dicts_become_actstep_models_on_the_wire():
    # The dicts a test (or an agent) sends are validated into ActStep by pydantic, and the
    # `text` alias is folded into `value` by the model. A harness that called `tool.fn`
    # would hand the function raw dicts and never exercise either.
    server = _Server({"type": "response", "data": {
        "success": True, "completedSteps": 2, "totalSteps": 2,
        "results": [{"step": 1, "success": True}, {"step": 2, "success": True}]}})
    client = await _client(server)
    result = await client.call_tool("page_act", {"steps": [
        {"action": "fill", "target": "#email", "text": "a@b.co", "submit": True},
        {"action": "click", "target": "Log in", "nth": 0, "settle_ms": 50},
    ], "tab_id": 3})
    assert result["success"] is True, result
    (request,) = server.sent
    assert request["action"] == "content.act"
    steps = request["data"]["steps"]
    assert steps[0]["action"] == "fill" and steps[0]["value"] == "a@b.co"
    assert steps[0]["submit"] is True and "text" not in steps[0]
    assert steps[1]["nth"] == 0 and steps[1]["settle_ms"] == 50


async def test_a_malformed_step_is_an_argument_error_and_never_reaches_the_tool():
    server = _Server()
    client = await _client(server)
    with pytest.raises(ToolArgumentError):
        await client.call_tool("page_act", {"steps": [{"target": "#x"}]})  # no `action`
    with pytest.raises(ToolArgumentError):
        await client.call_tool("page_act", {"steps": "click #x"})          # not a list
    assert server.sent == []


async def test_an_unknown_argument_is_rejected():
    server = _Server()
    client = await _client(server)
    with pytest.raises(ToolArgumentError):
        await client.call_tool("requests", {"params": "{}"})
    assert server.sent == []


async def test_a_recorded_failure_surfaces_as_iserror_through_the_harness():
    # The server's outcome policy turns the outcome the tool recorded into the protocol's
    # isError flag; the tool itself only returns prose. Only a call that goes through the
    # server's tools/call dispatch can show it.
    server = _Server({"type": "error", "error": "Tab 999 not found"})
    client = await _client(server)
    result = await client.call_tool("page_read", {"tab_id": 999})
    assert result["isError"] is True and result["success"] is False, result
    assert result["content"] == result["error"] and result["content"]


async def test_a_clean_call_is_a_success_with_text_content():
    client = await _client(_Server())
    result = await client.call_tool("server_info", {})
    assert result["isError"] is False and isinstance(result["content"], str)


async def test_list_tools_returns_names_from_tools_list():
    names = await (await _client(_Server())).list_tools()
    assert {"tabs", "navigate", "page_read", "page_act", "server_info"} <= set(names)


async def test_an_unknown_tool_reads_as_an_error_naming_the_available_tools():
    result = await (await _client(_Server())).call_tool("no_such_tool", {})
    assert result["isError"] is True
    assert "not found" in result["content"] and "page_act" in result["content"]


async def test_calling_before_connect_raises():
    client = DirectMCPTestClient(BrowserPlugTools(_Server()))
    with pytest.raises(RuntimeError):
        await client.call_tool("server_info", {})
