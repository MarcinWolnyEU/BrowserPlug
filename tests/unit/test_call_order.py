"""A browser switch and the same client's other calls take effect in arrival order.

The eval of 2026-10-08 found pi running `browsers(action="use")` and `tabs(action="open")`
from one turn in parallel: 5 of the 17 tabs opened that way went to the browser the agent
had just left, and the next call, routed to the new browser, could not find them. The
pure half (`server/tools/call_order.py`) is tested on its own; the glue test drives the
real dispatcher (`BrowserPlugMCPServer._on_call_tool`) with two connected browsers,
because the ordering helps only if the hook in front of every tool call is armed.
"""

import asyncio
import types

import pytest

import test_imports  # noqa: F401
from server.server import BrowserPlugServer
from server.tools import call_order
from server.tools.call_order import CallOrder, calls_ahead_of_switch, call_label
from server.tools.response_hints import stale_tab_text
from fake_process_table import fake_process_table  # noqa: F401  (autouse)
from test_browser_registry import disconnect, wait_until
from test_browser_routing import add_browser, as_client


# --- the pure lanes ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_calls_that_do_not_switch_run_side_by_side():
    order = CallOrder()
    release = asyncio.Event()
    inside = []

    async def read(i):
        async with order.admitted("c", switches=False, label=f"r{i}"):
            inside.append(i)
            await release.wait()

    tasks = [asyncio.create_task(read(i)) for i in range(3)]
    await wait_until(lambda: len(inside) == 3, "all three reads inside at once")
    assert order.in_flight("c") == 3
    release.set()
    await asyncio.gather(*tasks)
    assert order.in_flight("c") == 0, "a finished lane leaves nothing behind"


@pytest.mark.asyncio
async def test_a_switch_waits_for_calls_ahead_and_calls_behind_wait_for_it():
    order = CallOrder()
    events = []
    first_done = asyncio.Event()

    async def first():
        async with order.admitted("c", switches=False, label='tabs(action="list")'):
            events.append("first in")
            await first_done.wait()
            events.append("first out")

    async def switch():
        async with order.admitted("c", switches=True, label='browsers(action="use")'):
            events.append(("switch", calls_ahead_of_switch()))

    async def after():
        async with order.admitted("c", switches=False, label='tabs(action="open")'):
            events.append("after")

    t1 = asyncio.create_task(first())
    await wait_until(lambda: "first in" in events, "the first call is running")
    t2 = asyncio.create_task(switch())
    t3 = asyncio.create_task(after())
    await asyncio.sleep(0.05)
    assert events == ["first in"], "neither the switch nor the call behind it overtook"
    first_done.set()
    await asyncio.gather(t1, t2, t3)
    assert events == ["first in", "first out", ("switch", ('tabs(action="list")',)), "after"]


@pytest.mark.asyncio
async def test_lanes_are_per_client():
    order = CallOrder()
    hold = asyncio.Event()

    async def slow():
        async with order.admitted("a", switches=False, label="x"):
            await hold.wait()

    t = asyncio.create_task(slow())
    await wait_until(lambda: order.in_flight("a") == 1, "client a busy")
    async with order.admitted("b", switches=True, label="browsers"):
        pass                                   # client b's switch never waits for a
    hold.set()
    await t


@pytest.mark.asyncio
async def test_a_switch_gives_up_waiting_after_the_cap_and_names_the_call():
    order = CallOrder(switch_wait_cap=0.05)
    hold = asyncio.Event()

    async def stuck():
        async with order.admitted("c", switches=False, label='page_screenshot'):
            await hold.wait()

    t = asyncio.create_task(stuck())
    await wait_until(lambda: order.in_flight("c") == 1, "the stuck call is running")
    async with order.admitted("c", switches=True, label="browsers"):
        assert calls_ahead_of_switch() == ("page_screenshot",)
    assert calls_ahead_of_switch() == (), "reset once the switch is done"
    hold.set()
    await t


@pytest.mark.asyncio
async def test_a_cancelled_waiting_call_leaves_the_lane_clean():
    order = CallOrder()
    hold = asyncio.Event()

    async def switch():
        async with order.admitted("c", switches=True, label="browsers"):
            await hold.wait()

    async def behind():
        async with order.admitted("c", switches=False, label="tabs"):
            pass

    t1 = asyncio.create_task(switch())
    await wait_until(lambda: order.in_flight("c") == 1, "switch running")
    t2 = asyncio.create_task(behind())
    await wait_until(lambda: order.in_flight("c") == 2, "the next call is waiting")
    t2.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t2
    assert order.in_flight("c") == 1
    hold.set()
    await t1
    assert order.in_flight("c") == 0


def test_call_label():
    assert call_label("tabs", {"action": "open", "url": "x"}) == 'tabs(action="open")'
    assert call_label("page_read", {"tab_id": 3}) == "page_read"
    assert call_label("page_read", None) == "page_read"


def test_the_stale_tab_sentence_sends_the_agent_to_its_own_tab():
    old = ('Error navigating: Tab 1704838294 no longer exists. It was probably closed. 80 tabs '
           'are open; the active tab is 227 ("Activity | OpenRouter" - https://openrouter.ai/'
           'workspaces/default/activity). Re-list with the tabs tool, or omit tab_id to target '
           'the active tab.')
    one = stale_tab_text(old)
    assert one.startswith("Error navigating: Tab 1704838294 no longer exists")
    assert 'tabs(action="open", background=true, url="https://…")' in one
    for leaked in ("OpenRouter", "openrouter.ai", "active tab", "omit tab_id"):
        assert leaked not in one, leaked
    two = stale_tab_text("Tab 9 no longer exists.", "[2] Firefox Developer Edition")
    assert two.startswith("Tab 9 no longer exists in [2] Firefox Developer Edition.")
    assert "Tab ids belong to one browser" in two
    assert stale_tab_text(two, "[2] Firefox Developer Edition") == two, "applied once only"
    assert stale_tab_text("Tab 9 is a privileged page") == "Tab 9 is a privileged page"


# --- the glue: the real dispatcher with two browsers ------------------------------

@pytest.fixture
def server():
    return BrowserPlugServer(host="localhost", port=8772, start_mcp=False)


async def _dispatch(server, name, arguments):
    ctx = types.SimpleNamespace(request_id=None, lifespan_context={})
    params = types.SimpleNamespace(name=name, arguments=arguments)
    result = await server.mcp_tools.mcp._on_call_tool(ctx, params)
    return "".join(getattr(b, "text", "") for b in result.content), result.is_error


async def _answer(server, ws, data):
    req = ws.sent[-1]
    await server.rpc.handle_response({"id": req["id"], "type": "response",
                                      "action": req["action"], "data": data})


@pytest.mark.asyncio
async def test_a_call_sent_after_a_switch_reaches_the_browser_it_chose(server):
    a, a_task = await add_browser(server, "ff-a", 101, "Firefox")
    b, b_task = await add_browser(server, "ff-b", 102, "Zen Browser")
    assert server.mcp_tools.mcp.call_gate == server.mcp_tools.call_gate, "the hook is armed"
    tabs_data = {"tabs": [{"id": 1, "url": "https://example.org/", "title": "T",
                           "windowId": 1, "active": True}],
                 "windows": [{"id": 1, "focused": True}]}
    with as_client(777):
        # Tasks copy this context, so all three calls come from client 777.
        listing = asyncio.create_task(_dispatch(server, "tabs", {"action": "list"}))
        await wait_until(lambda: a.sent, "the list reached the default browser")
        list_request = a.sent.pop()
        switch = asyncio.create_task(
            _dispatch(server, "browsers", {"action": "use", "target": 2}))
        opened = asyncio.create_task(
            _dispatch(server, "tabs", {"action": "open", "url": "http://x/", "background": True}))
    await asyncio.sleep(0.05)
    assert not switch.done(), "the switch waits for the call that arrived before it"
    assert not b.sent and not a.sent, "the open waits for the switch"
    await server.rpc.handle_response({"id": list_request["id"], "type": "response",
                                      "action": "tabs.list", "data": tabs_data})
    text, is_error = await asyncio.wait_for(switch, 2.0)
    assert not is_error, text
    assert 'among them tabs(action="list")' in text, text
    await wait_until(lambda: b.sent, "the open reached the browser the switch chose")
    assert not a.sent
    await _answer(server, b, {"tab": {"id": 5, "windowId": 3, "url": "http://x/"}})
    text, is_error = await asyncio.wait_for(opened, 2.0)
    assert not is_error, text
    assert text.startswith("Opened tab [5] in [2] "), "the result names the owning browser"
    await asyncio.wait_for(listing, 2.0)
    await disconnect(a, a_task)
    await disconnect(b, b_task)


def test_module_is_stdlib_only():
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(call_order))
    names = {alias.name.split(".")[0] for node in ast.walk(tree)
             if isinstance(node, (ast.Import, ast.ImportFrom))
             for alias in (node.names if isinstance(node, ast.Import) else [node])
             if not isinstance(node, ast.ImportFrom)}
    names |= {node.module.split(".")[0] for node in ast.walk(tree)
              if isinstance(node, ast.ImportFrom) and node.module}
    assert names <= {"asyncio", "contextlib", "contextvars", "typing"}, names
