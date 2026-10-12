"""
Awaiting a lazy-loaded client-side route mount (SPA settle).

The problem: on a client-routed SPA whose routes lazy-load slowly, an agent had no way
to await "the new view actually mounted" — `document.readyState` stays "complete" the
whole time (the document never reloads), so it fell back to blind sleeps. BrowserPlug already
has the capability: a `page_act` wait step blocks on a selector with an exact timeout and
reports a distinct timeout diagnostic on a miss. What was missing was fixture-backed proof.

These tests use the `spa-lazy-route.html` fixture: View A has a "Go to B" button; clicking
it does a history.pushState route change (NO reload) and, after an artificial delay, mounts
a freshly-CREATED `<div data-view="b">` (it does not exist in the DOM until then). So a wait
on `[data-view=b]` genuinely blocks for the lazy mount — no external sleep, no reliance on a
document reload, no transient-document read.

Runs on BOTH engines (firefox + chromium): page_act's click + wait and page_read all work
on MV3, so there is no engine-only marker. Mirrors test_agent_experience.py's conventions
(the `mcp` fixture over DirectMCPTestClient, tab_id addressing, finally-block cleanup).

Note on timing: a page_act sequence runs a short (~600ms) post-click settle window BEFORE
the wait step's own clock starts. The fixture's lazy delay is sized (2000ms) so the wait
step's OWN reported elapsed still clears the ~1100ms "did it really block?" floor with
margin, while staying well under the 10s wait timeout.
"""

import re

import pytest
import pytest_asyncio

import test_imports  # noqa: F401  (path setup)
from mcp_client_harness import DirectMCPTestClient

# Serial phase only (pytest.ini `timing`): the wait step's own elapsed must clear
# a floor that a 600 ms settle window and a 2 s lazy mount leave little margin above.
pytestmark = pytest.mark.timing


@pytest.mark.slow
class TestSPASettle:
    """End-to-end SPA lazy-route settle tests (require a real browser + extension)."""

    @pytest_asyncio.fixture
    async def mcp(self, server_with_extension):
        client = DirectMCPTestClient(server_with_extension['server'].mcp_tools)
        # Tag with the engine for parity with the rest of the suite (unused here — this
        # module reads back state via page_read, which works on both engines).
        client.engine = server_with_extension['engine']
        await client.connect()
        yield client
        await client.disconnect()

    # ---- helpers ------------------------------------------------------------

    async def _open_tab(self, mcp, url):
        """Open `url` in a new tab via tabs(action='open') and return its tab_id.

        No post-open sleep: callers immediately navigate(wait='settled') which drives the
        load and blocks until the page is ready, so nothing here has to guess a settle time
        (keeps this module free of any external sleeps). Headless browsers don't reliably
        switch the OS-active tab on open, so everything is addressed by this returned id.
        """
        res = await mcp.call_tool("tabs", {"action": "open", "url": url})
        assert not res.get('isError'), res
        m = re.search(r'Opened tab \[(\d+)\]', res['content'])
        assert m, f"could not parse tab id from: {res['content']!r}"
        return int(m.group(1))

    # ---- tests --------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_wait_blocks_until_lazy_route_mounts(self, mcp, fixtures_server):
        """DoD: one page_act [click 'Go to B', wait for [data-view=b]] resolves exactly
        once the lazy route mounts — genuinely blocking, no external sleep, no reload."""
        url = fixtures_server + "spa-lazy-route.html"
        tab_id = await self._open_tab(mcp, url)
        try:
            # Settle the initial View A load first (assert it actually settled).
            nav = (await mcp.call_tool("navigate", {
                "tab_id": tab_id, "url": url, "wait": "settled"
            }))['content']
            assert "settled" in nav.lower() and "TIMED OUT" not in nav, nav

            # ONE page_act call: click the route button, then wait for the lazily-mounted
            # container. The wait must block until [data-view=b] both exists AND is visible.
            act = (await mcp.call_tool("page_act", {
                "tab_id": tab_id,
                "steps": [
                    {"action": "click", "target": "Go to B"},
                    {"action": "wait", "target": "[data-view=b]",
                     "require_visible": True, "timeout_ms": 10000},
                ],
            }))['content']

            first = act.splitlines()[0]
            assert first.startswith("OK"), act
            assert "2/2 steps completed" in first, act

            # The wait step reports a match...
            wait_line = next(
                (ln for ln in act.splitlines() if re.search(r"Step \d+ wait: ok", ln)), None)
            assert wait_line, f"no successful wait step line:\n{act}"
            assert "matched" in wait_line, wait_line

            # ...and its OWN reported elapsed proves it genuinely awaited the lazy mount
            # (not an early return). The wait clock starts AFTER the click step's ~600ms
            # settle window, so with the fixture's 2000ms delay it reports ~1.4s. Floor at
            # 1100ms leaves slack for timer jitter; it must also be well under the timeout.
            m = re.search(r"matched .* in (\d+(?:\.\d+)?)(ms|s)\b", wait_line)
            assert m, f"wait line has no parseable elapsed value:\n{wait_line}"
            wait_ms = float(m.group(1)) * (1000 if m.group(2) == "s" else 1)
            assert wait_ms >= 1100, (
                f"wait returned after only {wait_ms:.0f}ms — it did not block for the lazy "
                f"mount (expected >= ~1.1s for a 2s route delay):\n{act}")
            assert wait_ms < 10000, (
                f"wait elapsed {wait_ms:.0f}ms should be well under the 10s timeout:\n{act}")

            # View B actually mounted (read back via page_read — works on both engines).
            body = (await mcp.call_tool("page_read", {
                "tab_id": tab_id, "format": "text"}))['content']
            assert "View B mounted" in body, body
        finally:
            await mcp.call_tool("tabs", {"action": "close", "tab_id": tab_id})

    @pytest.mark.asyncio
    async def test_wait_for_absent_route_times_out_distinctly(self, mcp, fixtures_server):
        """DoD: a wait for a selector that never mounts ([data-view=c]) fails with a DISTINCT
        timeout diagnostic (names the timeout + the never-matched last-seen state), not a
        generic error."""
        url = fixtures_server + "spa-lazy-route.html"
        tab_id = await self._open_tab(mcp, url)
        try:
            nav = (await mcp.call_tool("navigate", {
                "tab_id": tab_id, "url": url, "wait": "settled"
            }))['content']
            assert "settled" in nav.lower() and "TIMED OUT" not in nav, nav

            # [data-view=c] never exists in this fixture: the wait must time out ~at its
            # 1200ms budget and say WHY, rather than returning a generic failure.
            miss = (await mcp.call_tool("page_act", {
                "tab_id": tab_id,
                "steps": [{"action": "wait", "target": "[data-view=c]", "timeout_ms": 1200}],
            }))['content']

            assert "FAILED" in miss, miss
            # Distinct timeout wording (not a bare "unknown error").
            assert "timed out" in miss.lower(), miss
            # The last-seen diagnostic pins it as never-matched (0 matches at last check),
            # so the agent knows the selector was evaluated and simply never appeared.
            assert "never-matched" in miss, (
                f"expected the 'never-matched' last-seen diagnostic:\n{miss}")
        finally:
            await mcp.call_tool("tabs", {"action": "close", "tab_id": tab_id})
