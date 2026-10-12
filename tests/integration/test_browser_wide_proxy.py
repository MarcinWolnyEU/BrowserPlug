"""Integration tests for the BROWSER-WIDE SOCKS5 proxy on Chromium.

Chromium cannot route one tab (chrome.proxy has no per-request hook), so a `proxy` on
`tabs` / `navigate` there switches the whole browser. With a local SOCKS5 proxy (no Tor)
this proves, in a real Chromium, that:

  1. a `proxy` switches the browser: the opened tab loads a host only the proxy can
     resolve, and the result tells the agent it was browser-wide and how to undo it;
  2. it really is the WHOLE browser: a second tab opened with no `proxy` reaches the same
     proxy-only host;
  3. `tabs(action="routing", proxy="")` undoes it: the proxy-only host is unreachable again;
  4. fail-closed: with the proxy down a load errors, it never goes out direct.

Chrome allows no optional `proxy` permission, so the manifest requires it and the user's
consent is a switch in the popup (`allowBrowserProxy`). The harness bakes that consent into
config.js (BUILD_CONFIG.allowBrowserProxy), and the guard below asserts it took, rather
than skipping.
"""

import re
from urllib.parse import urlparse

import pytest
import pytest_asyncio

import test_imports  # noqa: F401  (path setup)
from mcp_client_harness import DirectMCPTestClient
from port_coordinator import dead_port
from socks5_test_server import Socks5TestServer

MARKER = "BrowserPlug Form Fixture"  # present in form.html

pytestmark = pytest.mark.chromium_only(
    "a browser-wide proxy through chrome.proxy; Firefox routes one tab instead "
    "(test_proxy_routing.py)"
)


@pytest.fixture
def socks5_proxy():
    srv = Socks5TestServer()
    try:
        yield srv
    finally:
        srv.stop()


@pytest.fixture
def dead_proxy_port():
    """A claimed port nothing listens on, held for the whole test (port_coordinator)."""
    with dead_port() as port:
        yield port


@pytest.mark.slow
class TestBrowserWideProxy:

    @pytest_asyncio.fixture(autouse=True)
    async def _require_proxy_permission(self, server_with_extension):
        srv = server_with_extension['server']
        pa = getattr(srv, 'extension_proxy_access', None) or {}
        assert pa.get('has_proxy_permission'), (
            f"the Chromium extension reports proxy use as NOT allowed "
            f"(extension_proxy_access={pa!r}); the harness bakes the consent in, so "
            "check the allowBrowserProxy bake in ChromiumTestManager._bake_config")
        assert pa.get('proxy_scope') == 'browser', pa

    @pytest_asyncio.fixture
    async def mcp(self, server_with_extension):
        client = DirectMCPTestClient(server_with_extension['server'].mcp_tools)
        await client.connect()
        yield client
        # Whatever a test left on must not leak into the next browser.
        await client.call_tool("tabs", {"action": "routing", "proxy": ""})
        await client.disconnect()

    @staticmethod
    def _proxy_only_url(fixtures_server, page="form.html"):
        port = urlparse(fixtures_server).port
        return f"http://fixtures.invalid:{port}/{page}"

    @staticmethod
    def _tab_id(text):
        m = re.search(r'Opened tab \[(\d+)\]', text)
        assert m, f"could not parse tab id from: {text!r}"
        return int(m.group(1))

    async def _load(self, mcp, url, **extra):
        opened = await mcp.call_tool("tabs", {"action": "open", "url": url, **extra})
        tab_id = self._tab_id(opened['content'])
        await mcp.call_tool("navigate", {"tab_id": tab_id, "url": url, "wait": "settled",
                                         "timeout_ms": 15000})
        text = (await mcp.call_tool("page_read", {"tab_id": tab_id, "format": "text"})
                ).get('content', '')
        return opened, tab_id, text

    @pytest.mark.asyncio
    async def test_a_proxy_switches_the_whole_browser_and_says_so(
            self, mcp, fixtures_server, socks5_proxy):
        url = self._proxy_only_url(fixtures_server)
        endpoint = f"socks5://127.0.0.1:{socks5_proxy.port}"
        opened, tab_id, text = await self._load(mcp, url, proxy=endpoint)
        try:
            assert not opened.get('isError'), opened
            body = opened['content']
            assert "BROWSER-WIDE PROXY ON" in body and "ENTIRE BROWSER" in body, body
            assert endpoint in body and 'proxy=""' in body, body
            assert MARKER in text, f"the tab did not load the proxy-only host: {text[:300]!r}"
            assert any("fixtures.invalid" in h for h in socks5_proxy.requested_hosts), \
                socks5_proxy.requested_hosts          # DNS happened at the proxy
            routing = (await mcp.call_tool("tabs", {"action": "routing", "tab_id": tab_id})
                       )['content']
            assert "isProxy: true" in routing, routing   # the generic readback, not a Tor page
        finally:
            await mcp.call_tool("tabs", {"action": "close", "tab_id": tab_id})

    @pytest.mark.asyncio
    async def test_a_tab_opened_without_a_proxy_goes_through_it_too(
            self, mcp, fixtures_server, socks5_proxy):
        url = self._proxy_only_url(fixtures_server)
        endpoint = f"socks5://127.0.0.1:{socks5_proxy.port}"
        first, first_id, _ = await self._load(mcp, url, proxy=endpoint)
        second, second_id, text = await self._load(mcp, url)         # NO proxy argument
        try:
            assert MARKER in text, "a second tab should ride the browser-wide proxy"
            listing = (await mcp.call_tool("tabs", {"action": "list"}))['content']
            assert listing.startswith("BROWSER-WIDE PROXY ON"), listing[:200]
        finally:
            for t in (first_id, second_id):
                await mcp.call_tool("tabs", {"action": "close", "tab_id": t})

    @pytest.mark.asyncio
    async def test_routing_with_an_empty_proxy_switches_it_off(
            self, mcp, fixtures_server, socks5_proxy):
        url = self._proxy_only_url(fixtures_server)
        endpoint = f"socks5://127.0.0.1:{socks5_proxy.port}"
        _, on_id, _ = await self._load(mcp, url, proxy=endpoint)
        off = await mcp.call_tool("tabs", {"action": "routing", "proxy": ""})
        assert "BROWSER-WIDE PROXY OFF" in off['content'] and endpoint in off['content'], off
        assert "isProxy: false" in off['content'], off
        listing = (await mcp.call_tool("tabs", {"action": "list"}))['content']
        assert "BROWSER-WIDE PROXY ON" not in listing, listing[:200]
        # A new query string: the same URL fetched through the proxy a moment ago may be
        # served from the browser's cache, which would say nothing about the proxy.
        _, after_id, text = await self._load(mcp, url + "?after-off=1")
        try:
            assert MARKER not in text, "the proxy-only host loaded after the proxy was switched off"
        finally:
            for t in (on_id, after_id):
                await mcp.call_tool("tabs", {"action": "close", "tab_id": t})

    @pytest.mark.asyncio
    async def test_a_down_proxy_fails_closed(self, mcp, fixtures_server, dead_proxy_port):
        url = self._proxy_only_url(fixtures_server)
        endpoint = f"socks5://127.0.0.1:{dead_proxy_port}"
        opened = await mcp.call_tool("tabs", {"action": "open", "url": url, "proxy": endpoint})
        tab_id = self._tab_id(opened['content'])
        try:
            nav = await mcp.call_tool("navigate", {"tab_id": tab_id, "url": url,
                                                   "wait": "settled", "timeout_ms": 8000})
            assert re.search(r"PROXY|SOCKS", nav['content'].upper()), nav['content']
            text = (await mcp.call_tool("page_read", {"tab_id": tab_id, "format": "text"})
                    ).get('content', '')
            assert MARKER not in text, "fail-open leak: the page loaded despite a down proxy"
        finally:
            await mcp.call_tool("tabs", {"action": "close", "tab_id": tab_id})

    @pytest.mark.asyncio
    async def test_a_malformed_proxy_changes_nothing(self, mcp, fixtures_server):
        res = await mcp.call_tool("tabs", {"action": "open", "url": fixtures_server + "form.html",
                                           "proxy": "not-a-socks-url"})
        assert "Invalid proxy" in res['content']
        listing = (await mcp.call_tool("tabs", {"action": "list"}))['content']
        assert "BROWSER-WIDE PROXY ON" not in listing
