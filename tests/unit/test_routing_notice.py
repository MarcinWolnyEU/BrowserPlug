"""Unit tests for server/browsers/routing_notice.py and the two hooks that deliver it.

The evidence (2026-09-21): a Firefox extension dropped its socket mid-run. The server
evicted the browser, dropped the client's stale selection and served the next call from
the surviving Chrome — all correct. What the agent received was
`No open tab matches "bgap_h3/dispatch" (searched url + title of 4 tabs)`: a
success-shaped answer from a browser it had never asked for. It spent eight minutes
diagnosing a content-process crash that had not happened.

Nothing about the routing needed fixing. What was missing was a sentence, so these tests
are mostly about WHEN the sentence appears:

  * never on a client's first call — there is nothing to have changed;
  * once after a browser departs, then silence, because a notice that repeats is one that
    gets skimmed past;
  * never after the client's own `browsers(action="use")` — that move is its own
    decision, not news;
  * again when the departed browser comes back and routing returns to it, since the tab
    ids it is now holding are once more a different set.

Half of them drive the REAL tools through the REAL outcome policy (the one the server's
tools/call dispatch runs) with the FakeWS harness playing the browser. This codebase's recurring failure is arming code that stays dead
through hundreds of green helper tests: every test below that asserts on a notice would
pass with `_note_routing_change` deleted if it only exercised the renderer.
"""

import asyncio

import pytest
from mcp_types import CallToolResult, ImageContent, TextContent

import test_imports  # noqa: F401  (path setup)
from server.browsers import routing_notice
from server.browsers.routing_notice import DepartedBrowser, RoutingMemory
from server.tools.tool_outcome import ToolOutcomePolicy, fail
from test_browser_registry import disconnect, wait_until
from test_browser_routing import add_browser, as_client, call_browsers, server, tools  # noqa: F401

PNG_B64 = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDw"
           "AChwGA60e6kgAAAABJRU5ErkJggg==")


def _text(text):
    return CallToolResult(content=[TextContent(type="text", text=text)])


async def run_tabs(tools, server, wss, timeout=2.0, expect_error=False):
    """Run the REAL `tabs` tool through the REAL outcome policy, with FakeWS as the
    browser.

    Returns (the result's leading text, the FakeWS that got the request). The policy is
    what arms the notice slot and what puts the notice at the front of the response, so a
    test that called `tool.fn` directly would prove neither.

    The call must succeed unless `expect_error`: a failed call comes back as an isError
    result, not an exception, and a test asserting only that no notice appeared would
    otherwise pass on "Error listing tabs: ...".
    """
    for ws in wss:
        ws.sent.clear()
    fn = (await tools.mcp.get_tool("tabs")).fn

    async def run():
        return _text(await fn(action="list"))

    policy = ToolOutcomePolicy()
    task = asyncio.create_task(policy.apply("tabs", run))
    await wait_until(lambda: any(ws.sent for ws in wss) or task.done(),
                     "the tabs call either reached a browser or was refused")
    got = [ws for ws in wss if ws.sent]
    for ws in got:
        req = ws.sent[-1]
        await server.rpc.handle_response({
            "id": req["id"], "type": "response", "action": req["action"],
            "data": {"tabs": [{"id": 1, "url": "https://example.org/", "title": "T",
                               "windowId": 1, "active": True}],
                     "windows": [{"id": 1, "focused": True}]},
        })
    await asyncio.sleep(0)
    result = await asyncio.wait_for(task, timeout=timeout)
    assert result.is_error is expect_error, result.content[0].text
    return result.content[0].text, (got[0] if got else None)


# =============================================================================
# The memory: when a change counts as a change
# =============================================================================

def test_the_first_call_of_a_client_reports_nothing():
    memory = RoutingMemory()
    assert memory.served("pid:1", "ff-a") is None


def test_the_same_browser_again_reports_nothing():
    memory = RoutingMemory()
    memory.served("pid:1", "ff-a")
    assert memory.served("pid:1", "ff-a") is None


def test_a_different_browser_reports_the_previous_one_once():
    memory = RoutingMemory()
    memory.served("pid:1", "ff-a")
    assert memory.served("pid:1", "chrome-b") == "ff-a"
    assert memory.served("pid:1", "chrome-b") is None, "a notice that repeats is skimmed"


def test_the_memory_is_per_client():
    """Several agents share one server. A global "last browser" would fire a notice at
    everyone whenever any one of them switched."""
    memory = RoutingMemory()
    memory.served("pid:1", "ff-a")
    assert memory.served("pid:2", "chrome-b") is None
    assert memory.served("pid:1", "chrome-b") == "ff-a"


def test_a_clients_own_choice_is_not_news_to_it():
    memory = RoutingMemory()
    memory.served("pid:1", "ff-a")
    memory.chose("pid:1", "chrome-b")
    assert memory.served("pid:1", "chrome-b") is None


# =============================================================================
# DepartedBrowser: still a string, everywhere it used to be one
# =============================================================================

def test_a_departed_browser_is_still_just_its_name():
    """`resolve_target_browser` formats this straight into a sentence and must not learn
    anything new to keep doing so."""
    gone = DepartedBrowser("[1] Firefox Developer Edition pid 23176",
                           instance_id="f4714cf8", close_code=1006)
    assert gone == "[1] Firefox Developer Edition pid 23176"
    assert f"{gone} is no longer connected".startswith("[1] Firefox")
    assert gone.close_code == 1006
    assert gone.at_hms is None


# =============================================================================
# The wording
# =============================================================================

def test_the_departed_flavour_names_both_browsers_and_why_the_first_went():
    text = routing_notice.notice(
        now_serving="[4] Google Chrome Dev pid 10516",
        previous="[1] Firefox Developer Edition pid 23176",
        departed=True, departed_hms="23:54:58", close_code=1006, connected_count=1)
    assert "[1] Firefox Developer Edition pid 23176" in text
    assert "[4] Google Chrome Dev pid 10516" in text
    assert "23:54:58" in text and "1006" in text
    assert "Tab ids and page_snapshot refs" in text
    assert "reconnects under the same identity" in text
    assert len(text.splitlines()) <= 4, "a notice that scrolls is one that gets skipped"


def test_the_departed_flavour_offers_the_browsers_tool_when_there_is_a_choice():
    text = routing_notice.notice(
        now_serving="[4] Google Chrome Dev pid 10516", previous="[1] Firefox pid 23176",
        departed=True, departed_hms="23:54:58", close_code=1006, connected_count=2)
    assert 'browsers(action="use"' in text
    assert "2 browsers are connected" in text


def test_the_still_connected_flavour_says_where_calls_go_now_and_why():
    text = routing_notice.notice(
        now_serving="[1] Firefox Developer Edition pid 9232",
        previous="[4] Google Chrome Dev pid 10516", departed=False, connected_count=2,
        reason=routing_notice.PINNED_REASON)
    assert "still connected" in text
    assert "pinned" in text
    assert "[1] Firefox Developer Edition pid 9232" in text
    assert len(text.splitlines()) <= 4


def test_a_notice_with_nothing_to_name_is_no_notice():
    assert routing_notice.notice(now_serving="", previous="[1] Firefox",
                                 departed=True) == ""
    assert routing_notice.notice(now_serving="[1] Firefox", previous="",
                                 departed=True) == ""


def test_prepend_puts_the_notice_first():
    assert routing_notice.prepend("NOTICE", "4 tabs").startswith("NOTICE")
    assert routing_notice.prepend("", "4 tabs") == "4 tabs"


# =============================================================================
# Delivery: the outcome policy seam
# =============================================================================

async def test_the_notice_leads_the_response_text():
    policy = ToolOutcomePolicy()

    async def run():
        routing_notice.record("ROUTING CHANGED: ...")
        return _text("4 tabs")

    out = await policy.apply("tabs", run)
    assert out.content[0].text == "ROUTING CHANGED: ...\n\n4 tabs"


async def test_the_notice_leads_a_failure_message_too():
    """A call that fails because it landed in the wrong browser is exactly the call whose
    error text is about to be misread."""
    policy = ToolOutcomePolicy()

    async def run():
        routing_notice.record("ROUTING CHANGED: ...")
        return _text(fail("Error listing tabs: Request timed out after 30.0 seconds"))

    out = await policy.apply("tabs", run)
    assert out.is_error is True
    assert out.content[0].text.startswith("ROUTING CHANGED: ...")
    assert "Request timed out after 30.0 seconds" in out.content[0].text


async def test_a_screenshots_image_block_survives_the_notice():
    """page_screenshot returns TextContent + ImageContent, and the image is the payload
    the agent came for."""
    policy = ToolOutcomePolicy()
    image = ImageContent(type="image", data=PNG_B64, mime_type="image/png")

    async def run():
        routing_notice.record("ROUTING CHANGED: ...")
        return CallToolResult(content=[TextContent(type="text", text="Screenshot"), image])

    out = await policy.apply("page_screenshot", run)
    assert out.content[0].text.startswith("ROUTING CHANGED: ...")
    assert out.content[1].data == PNG_B64
    assert len(out.content) == 2


async def test_a_result_with_no_text_block_still_gets_the_notice():
    policy = ToolOutcomePolicy()
    image = ImageContent(type="image", data=PNG_B64, mime_type="image/png")

    async def run():
        routing_notice.record("ROUTING CHANGED: ...")
        return CallToolResult(content=[image])

    out = await policy.apply("page_screenshot", run)
    assert out.content[0].text == "ROUTING CHANGED: ..."
    assert out.content[1].data == PNG_B64


async def test_a_call_with_no_routing_change_is_returned_untouched():
    policy = ToolOutcomePolicy()
    result = _text("4 tabs")

    async def run():
        return result

    assert await policy.apply("tabs", run) is result


async def test_a_notice_cannot_leak_from_one_call_into_the_next():
    policy = ToolOutcomePolicy()
    routing_notice.record("a notice from the PREVIOUS call")

    async def run():
        return _text("4 tabs")

    out = await policy.apply("tabs", run)
    assert out.content[0].text == "4 tabs"


# =============================================================================
# End to end: the real tools, the real outcome policy, a browser that goes away
# =============================================================================

@pytest.mark.asyncio
async def test_a_first_call_says_nothing_about_routing(tools, server):
    a, a_task = await add_browser(server, "ff-a", 101, "Firefox")
    with as_client(4242):
        text, got = await run_tabs(tools, server, [a])
    assert got is a
    assert "ROUTING CHANGED" not in text
    await disconnect(a, a_task)


@pytest.mark.asyncio
async def test_a_departed_browser_is_announced_once_then_never_again(tools, server):
    """The wedge, reproduced: two browsers, the one being driven goes away, and the next
    call is served by the survivor."""
    a, a_task = await add_browser(server, "ff-a", 101, "Firefox")
    b, b_task = await add_browser(server, "chrome-b", 102, "Google Chrome")

    with as_client(4242):
        _, got = await run_tabs(tools, server, [a, b])
        assert got is a, "first-connected takes an unselected session"

        a.drop(code=1006)                           # the browser vanished
        await asyncio.wait_for(a_task, timeout=2.0)

        text, got = await run_tabs(tools, server, [b])
        assert got is b
        assert text.startswith("ROUTING CHANGED"), \
            "the silent reroute is the whole defect; it must announce itself"
        assert "[1] Firefox pid 101" in text
        assert "[2] Google Chrome pid 102" in text
        assert "1006" in text
        assert "Tab ids and page_snapshot refs" in text

        again, _ = await run_tabs(tools, server, [b])
        assert "ROUTING CHANGED" not in again, "once per change, not once per call"

    await disconnect(b, b_task)


@pytest.mark.asyncio
async def test_switching_browsers_on_purpose_is_not_announced(tools, server):
    a, a_task = await add_browser(server, "ff-a", 101, "Firefox")
    b, b_task = await add_browser(server, "chrome-b", 102, "Google Chrome")

    with as_client(4242):
        await run_tabs(tools, server, [a, b])
        await call_browsers(tools, action="use", target=2)
        text, got = await run_tabs(tools, server, [a, b])

    assert got is b
    assert "ROUTING CHANGED" not in text, \
        "the client asked for this move; telling it back is noise"

    await disconnect(a, a_task)
    await disconnect(b, b_task)


@pytest.mark.asyncio
async def test_routing_back_to_a_reconnected_browser_is_announced_too(tools, server):
    """Coming back is as big a change as going away: the tab ids in hand belong to the
    OTHER browser again."""
    a, a_task = await add_browser(server, "ff-a", 101, "Firefox")
    b, b_task = await add_browser(server, "chrome-b", 102, "Google Chrome")

    with as_client(4242):
        await run_tabs(tools, server, [a, b])
        a.drop(code=1006)
        await asyncio.wait_for(a_task, timeout=2.0)
        await run_tabs(tools, server, [b])                       # the departure notice

        a2, a2_task = await add_browser(server, "ff-a", 101, "Firefox")
        text, got = await run_tabs(tools, server, [a2, b])

    assert got is a2, "the reconnected browser takes slot 1 back, so routing returns"
    assert text.startswith("ROUTING CHANGED")
    assert "still connected" in text, "Chrome did not go anywhere; routing moved"
    assert "[2] Google Chrome pid 102" in text

    await disconnect(a2, a2_task)
    await disconnect(b, b_task)


@pytest.mark.asyncio
async def test_two_clients_are_told_separately(tools, server):
    """One client's switch must not produce a notice for another that never moved."""
    a, a_task = await add_browser(server, "ff-a", 101, "Firefox")
    b, b_task = await add_browser(server, "chrome-b", 102, "Google Chrome")

    with as_client(4242):
        await run_tabs(tools, server, [a, b])
    with as_client(9999):
        await call_browsers(tools, action="use", target=2)
        await run_tabs(tools, server, [a, b])
    with as_client(4242):
        text, got = await run_tabs(tools, server, [a, b])

    assert got is a
    assert "ROUTING CHANGED" not in text

    await disconnect(a, a_task)
    await disconnect(b, b_task)
