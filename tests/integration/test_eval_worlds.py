"""page_eval isolated-vs-main world blind-spot signal.

An agent had to read Vue framework internals (`el.__vueParentComponent`) via
page_eval. In the DEFAULT world ("isolated") the walk returned an EMPTY result —
indistinguishable from "there is no Vue here" — because the isolated content-script
world cannot see page-JS expando properties set on DOM nodes by the page's own
scripts. Only world="main" reaches them. These end-to-end tests prove:

  * an empty isolated-world read of such an expando yields the DISTINGUISHABLE
    outcome — the value is NOT returned, and a blind-spot NOTE tells the agent the
    result may be invisible-from-this-world (retry with world="main")     — not a
    bare empty that reads as absence; and
  * the SAME expression in world="main" returns the framework data.

Firefox-only: page_eval runs user-supplied code, which on headless Chromium needs
the un-grantable "Allow user scripts" toggle (EVAL_UNAVAILABLE — design.md D4/D12),
so eval-SUBJECT tests do not run on Blink. Deselected under the chromium param;
listed in tests/parity/parity.toml [integration].
"""

import re

import pytest
import pytest_asyncio

import test_imports  # noqa: F401  (path setup)
from mcp_client_harness import DirectMCPTestClient

# Whole-module firefox_only: the SUBJECT is page_eval running user-supplied code in
# the isolated vs main world. On headless Chromium page_eval requires the un-grantable
# "Allow user scripts" toggle -> EVAL_UNAVAILABLE (design.md D4/D12), so there is nothing
# to assert on Blink. Deselected under the chromium param; listed in parity.toml.
pytestmark = pytest.mark.firefox_only(
    "SUBJECT is page_eval running user-supplied code (isolated vs main world); headless "
    "Chromium needs the un-grantable 'Allow user scripts' toggle -> EVAL_UNAVAILABLE"
)

FIXTURE = "page-world-expando.html"


@pytest.mark.slow
class TestEvalWorlds:

    @pytest_asyncio.fixture
    async def mcp(self, server_with_extension):
        client = DirectMCPTestClient(server_with_extension['server'].mcp_tools)
        await client.connect()
        yield client
        await client.disconnect()

    # ---- helpers ------------------------------------------------------------

    async def _open_settled(self, mcp, url):
        """Open a fresh tab on `url`, then navigate() it once so the page (and its
        expando-setting script) has fully run before we eval. Returns the tab_id."""
        res = await mcp.call_tool("tabs", {"action": "open", "url": url})
        assert not res.get('isError'), res
        m = re.search(r'Opened tab \[(\d+)\]', res['content'])
        assert m, f"could not parse tab id from: {res['content']!r}"
        tab_id = int(m.group(1))
        nav = await mcp.call_tool(
            "navigate", {"tab_id": tab_id, "url": url, "wait": "settled"})
        assert not nav.get('isError'), nav
        return tab_id

    @staticmethod
    async def _eval(mcp, tab_id, code, world="isolated"):
        res = await mcp.call_tool(
            "page_eval", {"tab_id": tab_id, "code": code, "world": world})
        assert not res.get('isError'), res
        return res['content']

    # ---- tests --------------------------------------------------------------

    EXPANDO_EXPR = "document.getElementById('carrier').__pageExpando"

    @pytest.mark.asyncio
    async def test_isolated_world_expando_read_is_distinguishable(self, mcp, fixtures_server):
        """Isolated world: the page-set expando reads as undefined AND the empty
        result carries the blind-spot NOTE — not a bare empty that reads as absence."""
        tab_id = await self._open_settled(mcp, fixtures_server + FIXTURE)
        try:
            out = await self._eval(mcp, tab_id, self.EXPANDO_EXPR, world="isolated")
            # The framework data is NOT returned from the isolated world...
            assert "42" not in out, f"isolated world unexpectedly saw the expando:\n{out}"
            assert "fixture" not in out, f"isolated world unexpectedly saw the expando:\n{out}"
            # ...and the outcome is DISTINGUISHABLE from absence: a blind-spot note.
            assert "INVISIBLE FROM THE ISOLATED WORLD" in out, (
                f"expected the isolated-world blind-spot note:\n{out}")
            assert 'world:"main"' in out, f"note should point at world=main:\n{out}"
        finally:
            await mcp.call_tool("tabs", {"action": "close", "tab_id": tab_id})

    @pytest.mark.asyncio
    async def test_main_world_expando_read_returns_data(self, mcp, fixtures_server):
        """Main world: the SAME expression returns the framework data (n:42, framework:'fixture')."""
        tab_id = await self._open_settled(mcp, fixtures_server + FIXTURE)
        try:
            out = await self._eval(mcp, tab_id, self.EXPANDO_EXPR, world="main")
            assert "42" in out, f"main world did not see the expando value:\n{out}"
            assert "fixture" in out, f"main world did not see the expando framework:\n{out}"
            # The blind-spot note is a world=isolated concern; it must not appear here.
            assert "INVISIBLE FROM THE ISOLATED WORLD" not in out, (
                f"blind-spot note wrongly fired in the main world:\n{out}")
        finally:
            await mcp.call_tool("tabs", {"action": "close", "tab_id": tab_id})
