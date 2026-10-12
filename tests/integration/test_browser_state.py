"""
End-to-end proof of the browser_state push (and the version each party reports).

The options page's connection map draws a connected browser as TWO boxes: the
EXTENSION the WebSocket reaches, and the BROWSER it drives. The browser box's content
(window/tab counts) cannot be fetched on demand — get_connections_info() is synchronous
— so the extension PUSHES it: on connect, on each health-probe tick, and whenever a tab
or window comes or goes. Unit tests cover the server's half against a synthetic push;
only a real browser proves the extension actually sends one, with real counts.

Runs on BOTH engines: the push is a plain WebSocket message from top-level listeners
that exist in both trees (parity invariant A does not cover it — it is not an `action` —
so this is the test that keeps the two honest about it).
"""

import asyncio

import pytest
import pytest_asyncio

import test_imports  # noqa: F401  (path setup)
from mcp_client_harness import DirectMCPTestClient


async def wait_for_state(server, timeout=15.0):
    """The first browser_state push for the connected browser, or None on timeout.

    The extension sends one right after its hello, so this normally resolves on the
    first poll; the budget covers a slow cold start rather than a retry loop.
    """
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        for inst in server.get_connections_info()["extension_instances"]:
            if inst["currently_connected"] and inst["windows"] is not None:
                return inst
        await asyncio.sleep(0.25)
    return None


@pytest.mark.slow
class TestBrowserStatePush:

    @pytest_asyncio.fixture
    async def mcp(self, server_with_extension):
        client = DirectMCPTestClient(server_with_extension['server'].mcp_tools)
        client.engine = server_with_extension['engine']
        await client.connect()
        try:
            yield client, server_with_extension['server']
        finally:
            await client.disconnect()

    @pytest.mark.asyncio
    async def test_the_browser_reports_its_windows_and_tabs(self, mcp):
        client, server = mcp
        inst = await wait_for_state(server)
        assert inst is not None, (
            "no browser_state arrived — the extension never pushed its window/tab "
            "counts, so the connection map's browser box would stay empty")

        assert inst["windows"] >= 1, "a running browser has at least one window"
        assert inst["tabs"] >= 1
        assert inst["active_tabs"] >= 1
        # One foreground tab per window, and never more tabs active than exist.
        assert inst["active_tabs"] <= inst["tabs"]
        assert inst["loaded_tabs"] <= inst["tabs"]
        assert inst["state_hms"], "the push is timestamped so the UI can age it"

        # The foreground tab of each window, named rather than merely counted (the
        # count was "1 active" forever on a single-window browser).
        listed = inst["active_tab_list"]
        assert isinstance(listed, list), "a current extension always sends the list"
        assert len(listed) == inst["active_tabs"], "one entry per active tab"
        assert all(isinstance(t["id"], int) for t in listed), (
            "each entry names the tab by the id every other tool takes")
        assert all("title" in t and "last_hms" in t for t in listed)

    @pytest.mark.asyncio
    async def test_opening_a_tab_moves_the_count(self, mcp):
        """The counts are LIVE: tab events trigger a fresh (debounced) push."""
        client, server = mcp
        before = await wait_for_state(server)
        assert before is not None

        opened = await client.call_tool("tabs", {"action": "open",
                                                 "url": "https://example.org"})
        assert opened, "precondition: the tab really opened"
        try:
            deadline = asyncio.get_event_loop().time() + 15.0
            after = before
            while asyncio.get_event_loop().time() < deadline:
                after = next(i for i in server.get_connections_info()["extension_instances"]
                             if i["slot"] == before["slot"])
                if after["tabs"] > before["tabs"]:
                    break
                await asyncio.sleep(0.25)
            assert after["tabs"] > before["tabs"], (
                f"tab count did not follow a real tab opening "
                f"({before['tabs']} -> {after['tabs']}); the push is not live")
        finally:
            await client.call_tool("tabs", {"action": "close"})

    @pytest.mark.asyncio
    async def test_the_extension_version_and_the_server_version_are_both_reported(self, mcp):
        """Both halves of the map carry a version, and /health compares them."""
        client, server = mcp
        health = server.get_health_info()
        assert health["server_version"], "the server names its own build"

        inst = server.get_connections_info()["extension_instances"][0]
        assert inst["version"], "and the extension names its build (manifest.json)"
        # This repo ships them in lockstep (parity invariant H), so a test run against
        # a freshly built extension must see no skew. A mismatch here means the loaded
        # extension is a stale build — exactly what the warning exists to catch.
        assert inst["version"] == health["server_version"], (
            f"extension {inst['version']} vs server {health['server_version']} — the "
            f"test profile is running a stale XPI; rebuild with build_xpi.py")
        assert health["version_warning"] is None
