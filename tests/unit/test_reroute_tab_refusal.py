"""C4: a call that carries a tab id, frame id or snapshot ref must not cross a reroute.

Firefox tab ids are small integers, so the previous browser's tab 7 usually exists in the
browser that took over. Before this guard `page_act(tab_id=7)` after the selected browser
vanished acted on the OTHER (also logged-in) browser's tab 7, and only a call-level
notice, if any, said so afterwards. Now the first tab-, frame- or ref-bearing call after
a change is refused with the routing-change message and never reaches a browser; the agent
re-lists tabs and carries on. Calls that name nothing, and every later call, go through.
"""

import asyncio

import pytest
from mcp_types import CallToolResult, TextContent

import test_imports  # noqa: F401  (path setup)
from server.browsers import routing
from server.tools.tool_outcome import ToolOutcomePolicy
from test_browser_registry import disconnect, wait_until
from test_browser_routing import add_browser, as_client, call_browsers, server, tools  # noqa: F401
from test_routing_notice import run_tabs


async def run_tool(tools, server, wss, name, **kwargs):
    """Run a REAL tool through the REAL outcome policy; answer whatever browser got a
    request.

    Returns (the result's text, error or not; the FakeWS that got the request or None).
    """
    for ws in wss:
        ws.sent.clear()
    fn = (await tools.mcp.get_tool(name)).fn

    async def run():
        return CallToolResult(content=[TextContent(type="text", text=str(await fn(**kwargs)))])

    task = asyncio.create_task(ToolOutcomePolicy().apply(name, run))
    await wait_until(lambda: any(ws.sent for ws in wss) or task.done(),
                     "the call either reached a browser or was refused")
    got = [ws for ws in wss if ws.sent]
    for ws in got:
        req = ws.sent[-1]
        await server.rpc.handle_response({
            "id": req["id"], "type": "response", "action": req["action"], "data": {}})
    await asyncio.sleep(0)
    result = await asyncio.wait_for(task, timeout=2.0)
    text = result.content[0].text
    assert len(got) <= 1
    return text, (got[0] if got else None)


async def two_browsers_selected_gone(tools, server):
    """Client 4242 selects the first browser and drives it; then it vanishes."""
    a, a_task = await add_browser(server, "ff-a", 101, "Firefox")
    b, b_task = await add_browser(server, "ff-b", 102, "Firefox")
    with as_client(4242):
        await call_browsers(tools, action="use", target=1)
        await run_tabs(tools, server, [a, b])
        a.drop(code=1006)
        await asyncio.wait_for(a_task, timeout=2.0)
    return a, a_task, b, b_task


@pytest.mark.asyncio
async def test_tab_id_call_after_the_selected_browser_left_is_refused(tools, server):
    a, a_task, b, b_task = await two_browsers_selected_gone(tools, server)
    with as_client(4242):
        text, got = await run_tool(tools, server, [b], "page_read", tab_id=7)
    assert got is None, "tab 7 of the previous browser must not run in the other browser's tab 7"
    assert "ROUTING CHANGED" in text
    # It sends the agent to its own tab (find it, or open one), never to a list to pick from.
    assert 'tabs(action="find"' in text and 'tabs(action="open"' in text
    await disconnect(b, b_task)


@pytest.mark.asyncio
async def test_only_the_first_call_after_the_change_is_refused(tools, server):
    a, a_task, b, b_task = await two_browsers_selected_gone(tools, server)
    with as_client(4242):
        _, got = await run_tool(tools, server, [b], "page_read", tab_id=7)
        assert got is None
        _, got = await run_tool(tools, server, [b], "page_read", tab_id=7)
        assert got is b, "the refusal is one-shot; a repeat after re-listing must go through"
    await disconnect(b, b_task)


@pytest.mark.asyncio
async def test_a_call_naming_no_tab_is_served_with_the_notice(tools, server):
    a, a_task, b, b_task = await two_browsers_selected_gone(tools, server)
    with as_client(4242):
        text, got = await run_tabs(tools, server, [b])
    assert got is b
    assert text.startswith("ROUTING CHANGED")
    await disconnect(b, b_task)


@pytest.mark.asyncio
async def test_a_ref_target_call_is_refused_too(tools, server):
    a, a_task, b, b_task = await two_browsers_selected_gone(tools, server)
    step = {"action": "click", "target": "e5"}
    with as_client(4242):
        text, got = await run_tool(tools, server, [b], "page_act", steps=[step])
    assert got is None
    assert "ROUTING CHANGED" in text
    await disconnect(b, b_task)


@pytest.mark.asyncio
async def test_no_change_no_refusal(tools, server):
    a, a_task = await add_browser(server, "ff-a", 101, "Firefox")
    with as_client(4242):
        await run_tabs(tools, server, [a])
        _, got = await run_tool(tools, server, [a], "page_read", tab_id=7)
    assert got is a
    await disconnect(a, a_task)


@pytest.mark.asyncio
async def test_own_browsers_use_is_not_a_change(tools, server):
    a, a_task = await add_browser(server, "ff-a", 101, "Firefox")
    b, b_task = await add_browser(server, "ff-b", 102, "Firefox")
    with as_client(4242):
        await run_tabs(tools, server, [a, b])
        await call_browsers(tools, action="use", target=2)
        _, got = await run_tool(tools, server, [a, b], "page_read", tab_id=7)
    assert got is b
    await disconnect(a, a_task)
    await disconnect(b, b_task)


class _B:
    def __init__(self, instance_id, provisional):
        self.instance_id = instance_id
        self.provisional = provisional

    def describe(self):
        return self.instance_id


def test_a_pre_hello_socket_is_not_routable():
    pre, ready = _B("pending-1", True), _B("ff-a", False)
    assert routing.resolve_target([pre], {}, {}, "k") == "No extension connection available"
    assert routing.resolve_target([pre, ready], {}, {}, "k") is ready
