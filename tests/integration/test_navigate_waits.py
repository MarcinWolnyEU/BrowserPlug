"""navigate's waits name themselves, and wait="dom" says when the DOM never settled.

`wait="dom"` returns once the load event has fired and the DOM has gone 100 ms without a
mutation, bounded at 3 s of waiting after the load (chrome-devtools-mcp's rule after its
own load event). A page that keeps changing must come back after that bound saying so,
never reported stable: `ticking-dom.html` rewrites its text without pause from the load
event on. The result line names the wait that ran, so `"settled"`, `"load"` and `"dom"`
read differently. Runs on both engines; the DOM probe is a code string on gecko and a
function on Chromium (`waitForStableDom` in each background.js). A page busy in long tasks
from its load event on still hands its console over within the flush bound in every wait
(`busy-after-load.html`).
"""

import re
import time

import pytest
import pytest_asyncio

import test_imports  # noqa: F401  (path setup)
from mcp_client_harness import DirectMCPTestClient

# Serial phase only (pytest.ini `timing`): the wait="dom" bound is asserted in
# wall-clock seconds, and a loaded machine pushes the console flush past its 500 ms bound.
pytestmark = pytest.mark.timing


@pytest.mark.slow
class TestNavigateWaits:
    @pytest_asyncio.fixture
    async def mcp(self, server_with_extension):
        client = DirectMCPTestClient(server_with_extension['server'].mcp_tools)
        client.engine = server_with_extension['engine']
        await client.connect()
        yield client
        await client.disconnect()

    async def _open(self, mcp, url):
        res = await mcp.call_tool("tabs", {"action": "open", "url": url, "background": True})
        assert not res.get("isError"), res
        return int(re.search(r"Opened tab \[(\d+)\]", res["content"]).group(1))

    @pytest.mark.asyncio
    async def test_each_wait_names_itself_on_a_still_page(self, mcp, fixtures_server):
        url = fixtures_server + "titled.html"
        tab_id = await self._open(mcp, url)
        try:
            load = await mcp.call_tool("navigate", {"tab_id": tab_id, "url": url + "?w=load",
                                                    "wait": "load"})
            assert not load.get("isError"), load
            assert "(load event in " in load["content"], load["content"]

            dom = await mcp.call_tool("navigate", {"tab_id": tab_id, "url": url + "?w=dom",
                                                   "wait": "dom"})
            assert not dom.get("isError"), dom
            assert "(load event, then DOM unchanged for 100ms, in " in dom["content"], dom["content"]
            assert "NOTE: the DOM" not in dom["content"], dom["content"]

            settled = await mcp.call_tool("navigate", {"tab_id": tab_id, "url": url + "?w=s"})
            assert not settled.get("isError"), settled
            assert "(settled in " in settled["content"], settled["content"]
        finally:
            await mcp.call_tool("tabs", {"action": "close", "tab_id": tab_id})

    @pytest.mark.asyncio
    async def test_wait_dom_gives_up_on_a_dom_that_never_settles(self, mcp, fixtures_server):
        tab_id = await self._open(mcp, fixtures_server + "titled.html")
        try:
            started = time.monotonic()
            res = await mcp.call_tool("navigate", {
                "tab_id": tab_id, "url": fixtures_server + "ticking-dom.html",
                "wait": "dom", "timeout_ms": 15000})
            elapsed = time.monotonic() - started
            content = res["content"]
            assert not res.get("isError"), res
            assert "with the DOM not yet stable)" in content, content
            assert "NOTE: the DOM was still changing 3 s after the load event" in content, content
            assert 2.5 < elapsed < 12, f"the 3 s bound did not hold: {elapsed:.1f}s\n{content}"
        finally:
            await mcp.call_tool("tabs", {"action": "close", "tab_id": tab_id})

    @pytest.mark.asyncio
    async def test_a_busy_page_hands_over_its_console_within_the_bound(self, mcp, fixtures_server):
        """The console flush costs a busy page one of its tasks, in every wait.

        `busy-after-load.html` runs 300 ms tasks back to back for 2.4 s from the load event
        and logs an error in each, so every flush request lands mid-task. The relay answers
        in the task that receives the request. Each further task it waited on (a posted
        marker, the delivery of its own batch) would queue behind another 300 ms task and
        overrun the 500 ms bound, and the result would carry the may-be-incomplete note.
        """
        tab_id = await self._open(mcp, fixtures_server + "titled.html")
        try:
            for wait in ("settled", "load", "dom"):
                res = await mcp.call_tool("navigate", {
                    "tab_id": tab_id, "url": fixtures_server + f"busy-after-load.html?w={wait}",
                    "wait": wait, "timeout_ms": 15000})
                content = res["content"]
                assert not res.get("isError"), res
                assert "load-error early" in content, f"wait={wait}:\n{content}"
                assert "may be incomplete" not in content, f"wait={wait}:\n{content}"
        finally:
            await mcp.call_tool("tabs", {"action": "close", "tab_id": tab_id})
