"""Tool args hidden from the advertised schema, not from the call (ToolSpec `hidden`).

The gated arguments (`proxy` on tabs/navigate, `script_name`/`script_args` on page_eval)
are pruned from the schema a client lists while the call path still validates against the
function's own signature: a value a client sends reaches the function, and an argument the
function does not take is still refused. The wire-level tests below are the ones that tell
that apart from dropping the parameter; tests that call `tool.fn` directly pass either way.
"""

from typing import Optional

import pytest
from mcp import Client

import test_imports  # noqa: F401  (path setup)
from facade_double import FacadeDouble
from server.tools.mcp_server import BrowserPlugMCPServer
from server.tools.mcp_tools import BrowserPlugTools
from server.tools.tool_registry import ToolRegistry, ToolSpec


async def navigate(url: str, proxy: Optional[str] = None, wait: bool = True) -> str:
    """Go somewhere."""
    return f"url={url} proxy={proxy} wait={wait}"


def _spec(hidden=("proxy",)):
    return ToolSpec.from_function(navigate, name="navigate", description="Go.", hidden=hidden)


def _app(hidden=("proxy",)):
    return BrowserPlugMCPServer(ToolRegistry([_spec(hidden)]))


def test_hidden_arg_is_pruned_from_properties_and_required():
    spec = _spec()
    assert set(spec.parameters["properties"]) == {"url", "wait"}
    assert spec.parameters["required"] == ["url"]
    # the strictness marker survives the prune
    assert spec.parameters.get("additionalProperties") is False


def test_only_parameters_differ_from_an_unhidden_twin():
    # Hiding must not change anything but the advertised schema: not the name, the
    # description, the function the call reaches or the other properties.
    ref = _spec(hidden=())
    for hidden in ((), ("proxy",)):
        spec = _spec(hidden)
        assert spec.fn is ref.fn is navigate
        wire, ref_wire = spec.to_wire(), ref.to_wire()
        assert {k: v for k, v in wire.items() if k != "inputSchema"} == \
            {k: v for k, v in ref_wire.items() if k != "inputSchema"}
        want = {k: v for k, v in ref.parameters["properties"].items() if k not in hidden}
        assert spec.parameters["properties"] == want


async def test_hidden_arg_sent_by_a_client_still_reaches_the_function():
    async with Client(_app(), mode="legacy") as client:
        listed = await client.list_tools()
        result = await client.call_tool(
            "navigate", {"url": "u", "proxy": "socks5://127.0.0.1:9050"})
    assert "proxy" not in listed.tools[0].input_schema["properties"]
    assert result.is_error is False, result.content[0].text
    assert result.content[0].text == "url=u proxy=socks5://127.0.0.1:9050 wait=True"


async def test_an_unknown_arg_is_still_rejected():
    async with Client(_app(), mode="legacy") as client:
        result = await client.call_tool("navigate", {"url": "u", "bogus": 1})
    assert result.is_error is True
    assert "bogus" in result.content[0].text


@pytest.mark.parametrize("hidden, match", [
    (("proxxy",), "not in the signature"),
    (("url",), "has no default"),
])
def test_a_hidden_arg_must_exist_and_have_a_default(hidden, match):
    with pytest.raises(ValueError, match=match):
        _spec(hidden)


# =============================================================================
# The two gates in server/tools/mcp_tools.py, over the wire
# =============================================================================

class _Server(FacadeDouble):
    def __init__(self):
        super().__init__()
        self.sent = []

    async def send_request_and_wait(self, request, timeout=30.0, browser=None):
        self.sent.append(request)
        return {"type": "response", "data": {
            "tab": {"id": 7, "windowId": 1, "url": "http://x/"}}}


async def test_tabs_threads_a_proxy_the_schema_does_not_show():
    server = _Server()
    tools = BrowserPlugTools(server)
    assert tools.proxy_tools_enabled() is False
    assert "proxy" not in (await tools.mcp.get_tool("tabs")).parameters["properties"]
    async with Client(tools.mcp, mode="legacy") as client:
        result = await client.call_tool(
            "tabs", {"action": "open", "url": "http://x/",
                     "proxy": "socks5://127.0.0.1:9050"})
    assert result.is_error is False, result.content[0].text
    assert server.sent[-1]["data"]["proxy"] == "socks5://127.0.0.1:9050"


async def test_page_eval_script_args_follow_the_env_gate(monkeypatch, tmp_path):
    monkeypatch.delenv("BROWSERPLUG_EXT_SCRIPTS", raising=False)
    tools = BrowserPlugTools(_Server())
    props = (await tools.mcp.get_tool("page_eval")).parameters["properties"]
    assert "script_name" not in props and "script_args" not in props
    async with Client(tools.mcp, mode="legacy") as client:
        result = await client.call_tool("page_eval", {"script_name": "gen.sh"})
    # Reaches the function, which names the missing server-side setting.
    assert "BROWSERPLUG_EXT_SCRIPTS is not set" in result.content[0].text

    monkeypatch.setenv("BROWSERPLUG_EXT_SCRIPTS", str(tmp_path))
    tools = BrowserPlugTools(_Server())
    props = (await tools.mcp.get_tool("page_eval")).parameters["properties"]
    assert {"script_name", "script_args"} <= set(props)
