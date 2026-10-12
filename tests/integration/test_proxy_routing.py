"""Integration tests for per-tab SOCKS5 proxy routing.

Proves, with a local SOCKS5 proxy (no Tor, no third-party service), that:

  1. a tab opened with `proxy=` loads a host that ONLY the proxy can resolve
     (http://<name>.invalid:<real_port>/) — i.e. DNS happened at the proxy (the
     .onion analog) and the full browser context (page_read) works on the tab.
  2. the endpoint is entirely caller-supplied (the test passes its own ephemeral
     SOCKS port; nothing is hard-coded), and a malformed endpoint is rejected.
  3. fail-closed: a DIRECT tab to the proxy-only host can't load it (no clearnet
     leak), and a tab pointed at a DOWN proxy errors rather than loading direct.
  4. tabs(action="routing") distinguishes a routed tab from a direct one.

Firefox-gated via `server_with_extension` (skips cleanly when Firefox is absent).

`proxy` is an OPTIONAL WebExtension permission (manifest optional_permissions), off
by default — but the test profile GRANTS it before launch
(firefox_test_utils.FirefoxTestManager._grant_optional_permissions writes it into
<profile>/extension-preferences.json). The _require_proxy_permission guard below
therefore ASSERTS the grant took (rather than skipping): a missing grant means the
grant mechanism broke and must fail loudly, not silently skip the whole module.
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

# Whole-module firefox_only: per-tab SOCKS routing rides browser.proxy.onRequest, a
# Firefox-only per-request hook. chrome.proxy is global-only, so on Chromium `proxy` switches
# the whole browser instead (test_browser_wide_proxy.py). Deselected under the chromium
# param; listed in tests/parity/parity.toml.
pytestmark = pytest.mark.firefox_only(
    "per-tab SOCKS routing needs browser.proxy.onRequest; chrome.proxy is global-only "
    "(Chromium switches the whole browser: test_browser_wide_proxy.py)"
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
    """A claimed port nothing listens on, held for the whole test, so it keeps refusing
    connections while parallel workers bind ports of their own."""
    with dead_port() as port:
        yield port


@pytest.mark.slow
class TestProxyRouting:

    @pytest_asyncio.fixture(autouse=True)
    async def _require_proxy_permission(self, server_with_extension):
        """The test profile is SUPPOSED to grant the optional `proxy` permission
        before Firefox launches (firefox_test_utils.FirefoxTestManager.
        _grant_optional_permissions writes it into
        <profile>/extension-preferences.json). If the connected extension reports
        the permission as NOT granted, the grant mechanism itself broke — a real
        regression that must FAIL loudly, not silently skip the whole module. A
        skipped module reads as 'not applicable' when it actually means 'the grant
        is broken', and profile-cache validation only catches STALE profiles, not a
        fresh-grant failure. (Firefox-absence is handled one layer up by the
        server_with_extension fixture's own skip, not here.)"""
        srv = server_with_extension['server']
        pa = getattr(srv, 'extension_proxy_access', None) or {}
        assert pa.get('has_proxy_permission'), (
            "extension reports the optional 'proxy' permission is NOT granted "
            f"(extension_proxy_access={pa!r}). The test profile is supposed to grant "
            "it before launch — check firefox_test_utils._grant_optional_permissions "
            "and that <profile>/extension-preferences.json still holds "
            "{'browserplug@marcinwolny.net': {'permissions': ['proxy'], ...}} in a form this "
            "Firefox honors. A silently-broken grant would otherwise re-mask this "
            "whole proxy-routing module as skipped.")

    @pytest_asyncio.fixture
    async def mcp(self, server_with_extension):
        client = DirectMCPTestClient(server_with_extension['server'].mcp_tools)
        await client.connect()
        yield client
        await client.disconnect()

    # ---- helpers ------------------------------------------------------------

    @staticmethod
    def _proxy_only_url(fixtures_server, page="form.html"):
        """A URL whose host no local resolver can resolve (RFC 6761 reserves
        .invalid as never-resolvable) but whose port is the real fixtures port."""
        port = urlparse(fixtures_server).port
        return f"http://fixtures.invalid:{port}/{page}"

    @staticmethod
    def _tab_id(open_result):
        m = re.search(r'Opened tab \[(\d+)\]', open_result)
        assert m, f"could not parse tab id from: {open_result!r}"
        return int(m.group(1))

    async def _read_text(self, mcp, tab_id):
        res = await mcp.call_tool("page_read", {"tab_id": tab_id, "format": "text"})
        return res.get('content', '')

    # ---- the routing cases --------------------------------------------------

    @pytest.mark.asyncio
    async def test_proxy_routed_tab_reaches_proxy_only_host(self, mcp, fixtures_server, socks5_proxy):
        """A routed tab resolves & loads a host only the proxy can reach."""
        url = self._proxy_only_url(fixtures_server)
        endpoint = f"socks5://127.0.0.1:{socks5_proxy.port}"
        opened = await mcp.call_tool("tabs", {"action": "open", "url": url, "proxy": endpoint})
        assert not opened.get('isError'), opened
        assert f"routed via {endpoint}" in opened['content']
        tab_id = self._tab_id(opened['content'])

        try:
            # Settle the load through the proxy (proxy stays set; navigate omits proxy).
            await mcp.call_tool("navigate", {"tab_id": tab_id, "url": url,
                                             "wait": "settled", "timeout_ms": 15000})
            text = await self._read_text(mcp, tab_id)
            assert MARKER in text, f"proxied tab did not load the proxy-only host: {text[:300]!r}"
            # The proxy genuinely resolved the hostname (remote DNS / no local leak).
            assert any("fixtures.invalid" in h for h in socks5_proxy.requested_hosts), \
                socks5_proxy.requested_hosts

            routing = await mcp.call_tool("tabs", {"action": "routing", "tab_id": tab_id})
            assert "routed via" in routing['content'] and endpoint in routing['content']
            assert "isProxy: true" in routing['content'], routing['content']
        finally:
            await mcp.call_tool("tabs", {"action": "close", "tab_id": tab_id})

    @pytest.mark.asyncio
    async def test_proxy_direct_tab_cannot_reach_proxy_only_host(self, mcp, fixtures_server):
        """Without the proxy, the proxy-only host can't be reached (no leak)."""
        url = self._proxy_only_url(fixtures_server)
        opened = await mcp.call_tool("tabs", {"action": "open", "url": url})
        tab_id = self._tab_id(opened['content'])
        try:
            await mcp.call_tool("navigate", {"tab_id": tab_id, "url": url,
                                             "wait": "settled", "timeout_ms": 8000})
            text = await self._read_text(mcp, tab_id)
            assert MARKER not in text, "direct tab unexpectedly resolved a .invalid host"

            routing = await mcp.call_tool("tabs", {"action": "routing", "tab_id": tab_id})
            assert "direct" in routing['content']
            assert "isProxy: false" in routing['content'], routing['content']
        finally:
            await mcp.call_tool("tabs", {"action": "close", "tab_id": tab_id})

    @pytest.mark.asyncio
    async def test_proxy_fail_closed_when_proxy_down(self, mcp, fixtures_server, dead_proxy_port):
        """A DOWN proxy must fail the load with a PROXY error, never go direct.

        NOTE: the target must be a NON-loopback host. Firefox refuses to proxy
        127.0.0.1/localhost by default (network.proxy.allow_hijacking_localhost),
        so a loopback URL would bypass the proxy entirely and load directly — which
        would mask, not test, the fail-closed property. We use the proxy-only
        .invalid host: the up-proxy can resolve it (other tests), but here the proxy
        is DOWN, so the request must fail at the proxy hop with no direct fallback.
        """
        url = self._proxy_only_url(fixtures_server)
        endpoint = f"socks5://127.0.0.1:{dead_proxy_port}"
        opened = await mcp.call_tool("tabs", {"action": "open", "url": url, "proxy": endpoint})
        tab_id = self._tab_id(opened['content'])
        try:
            nav = await mcp.call_tool("navigate", {"tab_id": tab_id, "url": url,
                                                   "wait": "settled", "timeout_ms": 8000})
            # The failure must be a PROXY connection error (fail-closed), not a silent
            # direct load and not merely an unknown-host (which a no-proxy tab gives).
            assert "PROXY" in nav['content'].upper(), nav['content']
            text = await self._read_text(mcp, tab_id)
            assert MARKER not in text, "fail-open leak: page loaded despite a down proxy"
        finally:
            await mcp.call_tool("tabs", {"action": "close", "tab_id": tab_id})

    @pytest.mark.asyncio
    async def test_proxy_invalid_endpoint_rejected(self, mcp, fixtures_server):
        """A malformed endpoint is rejected (no silent direct connection)."""
        res = await mcp.call_tool("tabs", {"action": "open",
                                           "url": fixtures_server + "form.html",
                                           "proxy": "not-a-socks-url"})
        assert "Invalid proxy" in res['content']
