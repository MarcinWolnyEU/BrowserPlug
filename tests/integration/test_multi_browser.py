"""Multi-Firefox integration: two real browsers on one server, and routing.

BrowserPlug drives the user's REAL, logged-in Firefox. With several Firefoxes connected to
the one shared server, the failure that matters is not "the tool errored" — it is a call
landing in the WRONG logged-in browser. So this module runs TWO real headless Firefoxes
(separate profiles => separate extension instance_ids => two entries in the server's
browser registry) against the same server/WS port and proves, at the tool surface, that
each call lands in the browser it was supposed to.

Distinguishability is established at the product level, not from server internals: each
browser gets a tab whose URL carries a marker unique to it, and every routing assertion
is "this browser's tab list HAS its own marker and does NOT have the other's".

The sequence under test:
  two browsers connect -> the `browsers` tool APPEARS -> list shows both -> a call with
  no selection lands in the DEFAULT (first-connected) -> browsers(use, 2) -> calls now
  land in #2 -> switch back and forth (both directions cross-checked) -> kill #2 ->
  the next call routes back to #1 (stale selection dropped) and the `browsers` tool
  DISAPPEARS.

Firefox-gated: skipped cleanly when Firefox is unavailable (via server_with_extension).
Cleanup discipline (.tmp/dev-docs/testing.md): the second Firefox and every tab this module opens
are torn down in a finally, and every navigation is issued with wait="settled".
"""

import asyncio
import re

import pytest
import pytest_asyncio

import test_imports  # noqa: F401  (path setup)
from firefox_test_utils import async_wait_for_browser_count, start_additional_firefox
from chromium_test_utils import start_additional_chromium
from mcp_client_harness import DirectMCPTestClient

# Query-string markers: a tab's URL is browser-local state, so seeing one browser's
# marker in the other's tab list would mean a call landed in the wrong Firefox.
MARK_ONE = "browser=one"
MARK_TWO = "browser=two"


async def _poll(predicate, timeout=15.0, interval=0.25):
    """Await a condition that a WebSocket-thread event (a browser leaving) will make true."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if await predicate():
            return True
        await asyncio.sleep(interval)
    return False


async def _tool_gone(mcp, name):
    return name not in await mcp.list_tools()


@pytest.mark.slow
class TestMultiBrowserRouting:
    """Two live Firefoxes on one server (requires Firefox)."""

    @pytest_asyncio.fixture
    async def two_browsers(self, server_with_extension):
        """server_with_extension's Firefox + a SECOND one on the same server/port.

        Yields (setup, second_firefox). The second browser is always stopped in the
        finally — including when the test itself killed it, which is the point of the
        last step in the sequence.
        """
        setup = server_with_extension
        server = setup.server

        # The SECOND browser matches the engine under test, so each engine's
        # multi-browser routing is proven SAME-ENGINE (Firefox+Firefox / Chrome+Chrome)
        # — the first browser comes from server_with_extension (this param's engine),
        # and a mismatched second browser would test a different topology per param.
        # Its own profile/user-data-dir => its own extension instance_id => a distinct
        # registry entry, on the same server/WS port.
        if setup.engine == 'chromium':
            second = start_additional_chromium(setup.test_port, profile_tag='b',
                                             mcp_port=setup.mcp_port)
        else:
            second = start_additional_firefox(setup.test_port, profile_tag='b',
                                           mcp_port=setup.mcp_port)
        if second is None:
            # start_additional_* returns None ONLY when the browser binary is missing
            # (the one sanctioned skip family) — every other failure raises.
            pytest.skip(f"Second {setup.engine} setup or extension installation failed")

        try:
            # NOT a skip — the same lie conftest's server_with_extension already stopped
            # telling. A second browser that starts but never REGISTERS is what a broken WS
            # contract, a failed token bootstrap, an exception out of a message handler, or
            # a registry that no longer separates instance_ids all look like. Skipping it
            # reports "not applicable" for a broken product; fail, and hand over the state
            # needed to tell those causes apart.
            if not await async_wait_for_browser_count(server, 2, timeout=30.0):
                alive = second.firefox_process and second.firefox_process.poll() is None
                try:
                    stamped = second._profile_points_at_test_port()
                except Exception as e:
                    stamped = f"could not read: {e}"
                registered = server.live_browsers()
                raise AssertionError(
                    f"Second Firefox never registered on the test server on port "
                    f"{setup.test_port} within 30s.\n"
                    f"  second Firefox still running: {alive}\n"
                    f"  its profile points at {setup.test_port}: {stamped}\n"
                    f"  browsers registered: {len(registered)} "
                    f"{[(b.instance_id, 'provisional' if b.provisional else 'named') for b in registered]}\n"
                    "Exactly 1 registered + a live Firefox means it connected but its "
                    "extension_hello never landed (the wait requires a NAMED browser), or "
                    "the two profiles minted the SAME instance_id and the second displaced "
                    "the first instead of coexisting. Look in the captured server log for a "
                    "4401 auth rejection (token bootstrap) or a handler exception — both "
                    "drop the socket. If Firefox died, it is a profile/XPI problem."
                )
            yield setup, second
        finally:
            second.cleanup()
            # This module is the only thing that ever sets a selection on this server,
            # and the server outlives the test by one fixture teardown — leave nothing.
            server.browser_selection.clear()
            await async_wait_for_browser_count(server, 1, timeout=15.0)

    # ---- helpers ------------------------------------------------------------

    @staticmethod
    async def _tabs(mcp, **kwargs):
        res = await mcp.call_tool("tabs", kwargs)
        assert not res.get('isError'), res
        return res['content']

    async def _tab_list(self, mcp):
        """Full tab list (recent=False: the default view hides older tabs) of the browser
        THIS session currently drives."""
        return await self._tabs(mcp, action="list", recent=False)

    async def _open_marked_tab(self, mcp, fixtures_server, marker):
        """Open a marked page and settle it, in whichever browser this session drives.

        Uses tabs(open) + navigate(wait="settled") so the test exercises BOTH of the
        tools the routing has to get right, and leaves no navigation in flight.
        """
        url = f"{fixtures_server}titled.html?{marker}"
        opened = await self._tabs(mcp, action="open", url=url)
        m = re.search(r'Opened tab \[(\d+)\]', opened)
        assert m, f"could not parse tab id from: {opened!r}"
        tab_id = int(m.group(1))

        res = await mcp.call_tool("navigate", {"tab_id": tab_id, "url": url,
                                               "wait": "settled"})
        assert not res.get('isError'), res
        assert marker in res['content'], res['content']
        return tab_id

    @staticmethod
    async def _use(mcp, target):
        res = await mcp.call_tool("browsers", {"action": "use", "target": target})
        assert not res.get('isError'), res
        return res['content']

    @pytest.mark.asyncio
    async def test_two_browsers_route_switch_and_survive_a_death(
            self, two_browsers, fixtures_server):
        """The whole multi-browser sequence, in the order an agent would live it."""
        setup, second = two_browsers
        server = setup.server
        mcp = DirectMCPTestClient(server.mcp_tools)
        await mcp.connect()

        tab_one = None
        try:
            # --- Two DISTINCT browsers are registered -------------------------------
            live = server.live_browsers()
            assert len(live) == 2, live
            ids = [b.instance_id for b in live]
            assert len(set(ids)) == 2, (
                "the two Firefoxes reported the SAME extension instance_id, so the server "
                "sees one browser (the second socket displaced the first). Their profiles "
                f"must differ — check FirefoxTestManager(profile_tag=...): {ids}")
            # live[0] is the first-connected browser = the default. Slots are ordinals
            # assigned per server lifetime, so read them rather than assuming 1 and 2.
            default_id, other_id = ids
            slot_default, slot_other = live[0].slot, live[1].slot
            assert slot_default < slot_other, (slot_default, slot_other)

            # --- The `browsers` tool APPEARED (it must not exist for a single browser) ---
            assert "browsers" in await mcp.list_tools()

            roster = (await mcp.call_tool("browsers", {"action": "list"}))['content']
            assert "2 browser(s) connected" in roster, roster
            # Named from the PROCESS, not from what the extension claims — a fork (Zen,
            # LibreWolf) says "Firefox" there. The roster must name the real engine/edition:
            # "Firefox" on the gecko param, "Chrome" on the chromium param.
            # A claim may only make that name FINER (Opera GX inside Opera, subbrand_edition).
            expected_engine_name = "Chrome" if setup.engine == "chromium" else "Firefox"
            assert expected_engine_name in roster, roster
            assert f"[{slot_default}]" in roster and f"[{slot_other}]" in roster, roster
            assert "YOUR active browser" in roster and "server default" in roster, roster

            # --- No selection -> the DEFAULT (first-connected) browser ---------------
            tab_one = await self._open_marked_tab(mcp, fixtures_server, MARK_ONE)
            tabs_default = await self._tab_list(mcp)
            assert MARK_ONE in tabs_default, tabs_default

            # ...and it really was the default, not "whichever answered": the other
            # browser has never seen that tab.
            switched = await self._use(mcp, slot_other)
            assert "now drives" in switched and f"[{slot_other}]" in switched, switched

            tabs_two = await self._tab_list(mcp)
            assert MARK_ONE not in tabs_two, (
                f"the no-selection call landed in browser [{slot_other}], not the "
                "first-connected default — it opened its tab in the wrong Firefox:\n"
                f"{tabs_two}")

            # --- Selected the 2nd browser -> calls land there ------------------------
            await self._open_marked_tab(mcp, fixtures_server, MARK_TWO)
            tabs_two = await self._tab_list(mcp)
            assert MARK_TWO in tabs_two, tabs_two
            assert MARK_ONE not in tabs_two, tabs_two

            # --- ...and the default was untouched by any of it (the other direction) --
            await self._use(mcp, slot_default)
            tabs_one = await self._tab_list(mcp)
            assert MARK_ONE in tabs_one, tabs_one
            assert MARK_TWO not in tabs_one, (
                f"a call made while browser [{slot_other}] was selected landed in browser "
                f"[{slot_default}] — the worst failure mode this product has:\n{tabs_one}")

            # Selection is sticky and per-client: back to the 2nd for the death test.
            await self._use(mcp, slot_other)
            assert MARK_TWO in await self._tab_list(mcp)

            # --- The SELECTED browser dies ------------------------------------------
            second.stop_firefox()
            assert await async_wait_for_browser_count(server, 1, timeout=20.0), \
                "the killed Firefox never left the server's browser registry"
            # The one that died is the one we killed — the survivor is the default.
            assert [b.instance_id for b in server.live_browsers()] == [default_id], \
                f"the wrong browser left the registry (expected {other_id} to go)"

            # The selection pointed at the dead browser. With <=1 left the `browsers`
            # tool is gone, so an error would be unrecoverable: the stale selection is
            # dropped and the call routes to the survivor instead.
            tabs_after = await self._tab_list(mcp)
            assert MARK_ONE in tabs_after, (
                "after the selected browser died the call did not route back to the "
                f"surviving one:\n{tabs_after}")
            assert MARK_TWO not in tabs_after, tabs_after
            assert not server.browser_selection, \
                f"the stale selection was not dropped: {server.browser_selection}"

            # --- ...and the `browsers` tool DISAPPEARED -----------------------------
            assert await _poll(lambda: _tool_gone(mcp, "browsers")), \
                "the `browsers` tool is still registered with a single browser connected"

            info = (await mcp.call_tool("server_info", {}))['content']
            assert "Browsers: 1 connected" in info, info

        finally:
            # Everything this test opened in the SURVIVING browser (the second one took
            # its own tab to the grave). Function scope means no next test inherits it,
            # but leave the fixture browser clean anyway (the cleanup rule in .tmp/dev-docs/testing.md).
            if tab_one is not None:
                try:
                    await mcp.call_tool("tabs", {"action": "close", "tab_id": tab_one})
                except Exception:
                    pass
            await mcp.disconnect()
