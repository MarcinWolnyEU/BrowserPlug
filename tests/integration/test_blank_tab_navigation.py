"""A fresh background tab navigated at once reports a clean load, on both engines.

`tabs(action="open", url="about:blank")` returns at once, and an agent's next call is
usually a `navigate` of that tab. On Firefox that navigation moves the tab to another
content process, and both processes report it (WebNavigationContent.cpp, Bugzilla
1732564): the process the tab leaves ends its own attempt at the requested URL with
NS_BINDING_ABORTED, and that abort can reach the extension after the new process has
committed the page. Live on Firefox Developer Edition 158 with extension 1.0.1, 3 to 9
runs in 10 came back as `net error: request aborted ... [NS_BINDING_ABORTED = 2152398850]`
and failed the call, while the request log held one 200 for the page. The extension now
sets that abort aside once the page it names has finished loading
(`abortOfCompletedLoad` in background.js; navdiag.test.mjs replays the event order).

The repetition is the point: the race lost about half the time, so one clean run proves
nothing. Chromium reports a navigation from the browser process alone and never showed
the echo; it runs the same loop as a guard against a false error there.
"""

import re

import pytest
import pytest_asyncio

import test_imports  # noqa: F401  (path setup)
from mcp_client_harness import DirectMCPTestClient

TARGET = "https://example.org/"
RUNS = 20

# Serial phase only (pytest.ini `timing`): the abort echo is a race between two
# content processes, and load on the machine changes how often each side wins.
pytestmark = pytest.mark.timing


@pytest.mark.slow
class TestBlankTabNavigation:
    @pytest_asyncio.fixture
    async def mcp(self, server_with_extension):
        client = DirectMCPTestClient(server_with_extension['server'].mcp_tools)
        client.engine = server_with_extension['engine']
        await client.connect()
        yield client
        await client.disconnect()

    @pytest.mark.asyncio
    async def test_a_blank_tab_navigated_at_once_reports_no_abort(self, mcp):
        flagged = []
        for run in range(1, RUNS + 1):
            opened = await mcp.call_tool(
                "tabs", {"action": "open", "url": "about:blank", "background": True})
            assert not opened.get("isError"), opened
            tab_id = int(re.search(r"Opened tab \[(\d+)\]", opened["content"]).group(1))
            try:
                res = await mcp.call_tool("navigate", {"tab_id": tab_id, "url": TARGET})
                content = res.get("content") or ""
                if res.get("isError") or "net error" in content:
                    log = await mcp.call_tool(
                        "requests", {"action": "recent", "tab_id": tab_id, "limit": 10})
                    flagged.append(f"run {run}: {content}\n  request log: {log.get('content')}")
                else:
                    assert "Example Domain" in content, content
            finally:
                await mcp.call_tool("tabs", {"action": "close", "tab_id": tab_id})
        assert not flagged, (f"{len(flagged)}/{RUNS} navigations of a fresh blank tab came "
                             "back with a net error:\n" + "\n".join(flagged))
