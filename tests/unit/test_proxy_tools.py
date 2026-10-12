"""Unit tests for the per-tab SOCKS proxy FEATURE at the MCP tool layer (server/tools/mcp_tools.py):
the `proxy` arg on tabs/navigate, its fail-closed validation, the tabs.routing readback,
and the optional-permission gate that hides the arg until the extension holds `proxy`.

Split out of test_page_tools.py so every proxy test lives under a test_proxy_* name in a
dedicated file. The browser-side routing logic (proxyDecision / persistence / restore /
fail-closed) is unit-tested in tests/js/proxy.test.mjs, and the end-to-end wire path in
tests/integration/test_proxy_routing.py. The mock harness below is a deliberate small
copy — it keeps this file independent of test_page_tools.

Related: the launcher's MCP-transport proxy (server/proxy/, BROWSERPLUG_LAUNCHER_RELAY=0)
is a DIFFERENT subsystem; its tests are tests/unit/test_proxy_{policy,backend,front,
side_channel,e2e}.py.
"""

import pytest

import test_imports  # noqa: F401
from facade_double import FacadeDouble
from server.tools.mcp_tools import BrowserPlugTools


class MockServer(FacadeDouble):
    def __init__(self):
        super().__init__()
        self.sent = []
        self.responses = {}
        self.extension_proxy_access = None

    def set_response(self, action, response):
        self.responses[action] = response

    async def send_request_and_wait(self, request, timeout=30.0, browser=None):
        self.sent.append(request)
        action = request.get("action", "")
        if action in self.responses:
            return self.responses[action]
        return {"type": "response", "data": {"ok": True, "echo": action}}


def resp(data):
    return {"type": "response", "data": data}


@pytest.fixture
def server():
    return MockServer()


@pytest.fixture
def tools(server):
    return BrowserPlugTools(server)


async def _fn(tools, name):
    return (await tools.mcp.get_tool(name)).fn


def _last(server):
    return server.sent[-1]


def _props(tool):
    return set(tool.parameters.get("properties", {}).keys())


# =============================================================================
# tabs / navigate — opt-in SOCKS proxy routing
# =============================================================================

@pytest.mark.asyncio
async def test_proxy_tabs_open_threads_and_reports(tools, server):
    server.set_response("tabs.create", resp({
        "tab": {"id": 42, "windowId": 10, "url": "http://svc.onion/",
                "proxy": "socks5://127.0.0.1:9050"}}))
    fn = await _fn(tools, "tabs")
    out = await fn(action="open", url="http://svc.onion/", proxy="socks5://127.0.0.1:9050")
    req = _last(server)
    assert req["action"] == "tabs.create"
    assert req["data"]["proxy"] == "socks5://127.0.0.1:9050"
    assert "routed via socks5://127.0.0.1:9050" in out
    assert "fail-closed" in out


@pytest.mark.asyncio
async def test_proxy_tabs_open_invalid_rejected_before_wire(tools, server):
    fn = await _fn(tools, "tabs")
    out = await fn(action="open", url="http://x/", proxy="127.0.0.1:9050")  # missing scheme
    assert out.startswith("Error: Invalid proxy")
    assert server.sent == []  # validation failure never hits the wire (fail-closed)


@pytest.mark.asyncio
async def test_proxy_tabs_open_new_window_rejected(tools, server):
    fn = await _fn(tools, "tabs")
    out = await fn(action="open", url="http://x/", proxy="socks5://127.0.0.1:9050",
                   new_window=True)
    assert "proxy routing is supported on a normal tab open" in out
    assert server.sent == []


@pytest.mark.asyncio
async def test_proxy_tabs_open_omits_key_when_unset(tools, server):
    server.set_response("tabs.create", resp({"tab": {"id": 1, "windowId": 10, "url": "http://x/"}}))
    fn = await _fn(tools, "tabs")
    await fn(action="open", url="http://x/")
    assert "proxy" not in _last(server)["data"]  # opt-in: no proxy key when unset


@pytest.mark.asyncio
async def test_proxy_tabs_routing_action_routed_and_direct(tools, server):
    fn = await _fn(tools, "tabs")
    server.set_response("tabs.routing", resp({
        "tabId": 5, "routed": True, "type": "socks",
        "endpoint": "socks5://127.0.0.1:9050", "proxyDNS": True}))
    out = await fn(action="routing", tab_id=5)
    assert _last(server)["action"] == "tabs.routing"
    assert _last(server)["data"] == {"tabId": 5}
    assert "routed via socks5://127.0.0.1:9050" in out and "proxyDNS=True" in out

    server.set_response("tabs.routing", resp({"tabId": 6, "routed": False, "type": "direct"}))
    out = await fn(action="routing", tab_id=6)
    assert "Tab 6 is direct (no proxy)" in out


@pytest.mark.asyncio
async def test_proxy_tabs_list_shows_proxied_tab(tools, server):
    server.set_response("tabs.list", resp({
        "tabs": [{"id": 1, "url": "http://svc.onion/", "title": "Onion", "active": True,
                  "windowId": 10, "pinned": False, "proxy": "socks5://127.0.0.1:9050"}],
        "windows": [{"id": 10, "focused": True, "state": "normal", "incognito": False}]}))
    fn = await _fn(tools, "tabs")
    out = await fn()
    assert "id: 1 active proxied:socks5://127.0.0.1:9050" in out


@pytest.mark.asyncio
async def test_proxy_navigate_threaded(tools, server):
    server.set_response("navigation.navigate", resp({
        "ok": True, "op": "goto", "tabId": 3, "url": "http://svc.onion/", "settled": True,
        "waitedMs": 100}))
    fn = await _fn(tools, "navigate")
    await fn(url="http://svc.onion/", tab_id=3, proxy="socks5://127.0.0.1:9050")
    assert _last(server)["data"]["proxy"] == "socks5://127.0.0.1:9050"


@pytest.mark.asyncio
async def test_proxy_navigate_invalid_rejected_before_wire(tools, server):
    fn = await _fn(tools, "navigate")
    out = await fn(url="http://x/", proxy="http://1.2.3.4:8080")  # not a socks scheme
    assert out.startswith("Error: Invalid proxy")
    assert server.sent == []


@pytest.mark.asyncio
async def test_proxy_empty_and_direct_are_valid_and_clear(tools, server):
    server.set_response("navigation.navigate", resp({
        "ok": True, "op": "goto", "tabId": 1, "url": "http://x/", "settled": True,
        "waitedMs": 1}))
    fn = await _fn(tools, "navigate")
    for clearing in ("", "direct"):
        await fn(url="http://x/", proxy=clearing)
        assert _last(server)["data"]["proxy"] == clearing  # threaded so the tab is un-routed


# =============================================================================
# tabs / navigate — the `proxy` arg is gated on the optional "proxy" permission
# =============================================================================

@pytest.mark.asyncio
async def test_proxy_arg_hidden_until_permission_granted(tools, server):
    # Default (extension not connected / permission unknown): proxy is NOT in the
    # schema and no proxy wording leaks into the tool descriptions.
    assert tools.proxy_tools_enabled() is False
    for name in ("tabs", "navigate"):
        tool = await tools.mcp.get_tool(name)
        assert "proxy" not in _props(tool), f"{name} must hide proxy when the permission is off"
        assert "proxy" not in (tool.description or "").lower(), \
            f"{name} description must not mention proxy when the permission is off"
    sig_off = tools.tools_signature
    assert sig_off is not None

    # Extension reports the permission granted -> proxy appears, documented in its own
    # parameter description (the .onion use case, the supported and unsupported proxy
    # types), never in the tool description that opens with the common calls.
    server.extension_proxy_access = {"has_proxy_permission": True}
    assert tools.refresh_proxy_tool_visibility() is True
    assert tools.proxy_tools_enabled() is True
    assert tools.tools_signature != sig_off  # signature flips -> launcher notifies clients
    for name in ("tabs", "navigate"):
        tool = await tools.mcp.get_tool(name)
        assert "proxy" in _props(tool), f"{name} must expose proxy when granted"
        assert "proxy" not in (tool.description or "").lower(), name
        desc = tool.parameters["properties"]["proxy"].get("description") or ""
        assert desc.startswith("only when the user asks for a proxy"), desc
        assert ".onion" in desc, f"{name} should call out the .onion use case"
        assert "socks5" in desc and "socks4a" in desc, f"{name} should list the supported schemes"
        assert "NOT supported" in desc, f"{name} should list what isn't supported"

    # Revoked again -> proxy hidden once more; signature returns to the off value.
    server.extension_proxy_access = {"has_proxy_permission": False}
    assert tools.refresh_proxy_tool_visibility() is True
    for name in ("tabs", "navigate"):
        assert "proxy" not in _props(await tools.mcp.get_tool(name))
    assert tools.tools_signature == sig_off


@pytest.mark.asyncio
async def test_proxy_refresh_visibility_is_idempotent(tools, server):
    server.extension_proxy_access = {"has_proxy_permission": True}
    assert tools.refresh_proxy_tool_visibility() is True   # off -> on: re-registers
    assert tools.refresh_proxy_tool_visibility() is False  # already on: no-op, no churn


@pytest.mark.asyncio
async def test_proxy_fn_accepts_arg_even_when_schema_hides_it(tools, server):
    # add_tool_hiding_args only prunes the EXPOSED schema; the Python fn (and thus
    # the wire path, see test_tool_schema.py) still accepts proxy. This keeps
    # direct/unit invocation working and means the gate is purely about what a
    # client is shown.
    assert tools.proxy_tools_enabled() is False
    server.set_response("tabs.create", resp({
        "tab": {"id": 7, "windowId": 1, "url": "http://svc.onion/",
                "proxy": "socks5://127.0.0.1:9050"}}))
    fn = await _fn(tools, "tabs")
    await fn(action="open", url="http://svc.onion/", proxy="socks5://127.0.0.1:9050")
    assert _last(server)["data"]["proxy"] == "socks5://127.0.0.1:9050"


# =============================================================================
# proxy="tor": the word becomes the managed Tor client's endpoint (.tmp/dev-docs/tor.md)
# =============================================================================

TOR_ENDPOINT = "socks5://127.0.0.1:41999"
# What a hello reports for a browser that may use a proxy. proxy="tor" starts nothing without it.
GRANTED = {"has_proxy_permission": True}


class FakeTor:
    def __init__(self, error=None):
        self.starts = 0
        self.error = error

    def start(self):
        self.starts += 1
        if self.error:
            raise self.error
        return TOR_ENDPOINT


@pytest.fixture
def fake_tor(monkeypatch):
    from server.tor import manager
    fake = FakeTor()
    monkeypatch.setattr(manager, "shared", lambda: fake)
    return fake


@pytest.mark.parametrize("spelling", ["tor", "Tor", "  TOR "])
@pytest.mark.asyncio
async def test_proxy_tor_tabs_open_sends_the_managed_endpoint(tools, server, fake_tor, spelling):
    server.extension_proxy_access = GRANTED
    server.set_response("tabs.create", resp({
        "tab": {"id": 7, "windowId": 10, "url": "http://svc.onion/", "proxy": TOR_ENDPOINT}}))
    fn = await _fn(tools, "tabs")
    out = await fn(action="open", url="http://svc.onion/", proxy=spelling)
    assert _last(server)["data"]["proxy"] == TOR_ENDPOINT   # never the word itself
    assert f"routed via {TOR_ENDPOINT}" in out
    assert fake_tor.starts == 1


@pytest.mark.asyncio
async def test_proxy_tor_navigate_sends_the_managed_endpoint(tools, server, fake_tor):
    server.extension_proxy_access = GRANTED
    server.set_response("navigation.navigate", resp({
        "ok": True, "op": "goto", "tabId": 3, "url": "http://svc.onion/", "settled": True,
        "proxy": TOR_ENDPOINT}))
    fn = await _fn(tools, "navigate")
    await fn(url="http://svc.onion/", tab_id=3, proxy="tor")
    assert _last(server)["action"] == "navigation.navigate"
    assert _last(server)["data"]["proxy"] == TOR_ENDPOINT
    assert fake_tor.starts == 1


@pytest.mark.asyncio
async def test_proxy_tor_that_cannot_start_fails_closed_before_the_wire(tools, server, monkeypatch):
    from server.tor import manager
    monkeypatch.setattr(manager, "shared", lambda: FakeTor(manager.TorError("no network")))
    server.extension_proxy_access = GRANTED
    for call in (
        (await _fn(tools, "tabs"), dict(action="open", url="http://x.onion/", proxy="tor")),
        (await _fn(tools, "navigate"), dict(url="http://x.onion/", proxy="tor")),
    ):
        out = await call[0](**call[1])
        assert out.startswith('Error: proxy="tor" could not start Tor: no network')
    assert server.sent == []          # never a direct load, never a half-routed tab


@pytest.mark.asyncio
async def test_proxy_tor_is_not_started_for_a_call_that_is_refused_or_direct(tools, server, fake_tor):
    server.extension_proxy_access = GRANTED
    fn = await _fn(tools, "tabs")
    for window in ({"new_window": True}, {"incognito": True}):
        out = await fn(action="open", url="http://x/", proxy="tor", **window)
        assert "proxy routing is supported on a normal tab open" in out
    assert server.sent == []
    assert (await fn(action="open", url="http://x/", proxy="tor2")).startswith("Error: Invalid proxy")
    server.set_response("tabs.create", resp({"tab": {"id": 1, "windowId": 10, "url": "http://x/"}}))
    for direct in (None, "", "direct", "socks5://127.0.0.1:9050"):
        await fn(action="open", url="http://x/", proxy=direct)
    assert fake_tor.starts == 0


# =============================================================================
# an unresolved .onion without a proxy tells the agent the call that fixes it
# =============================================================================

ONION = "http://2gzyxa5ihm7nsggfxnu52rck2vv4rvmdlkiu3zzui5du4xyclen53wid.onion/"
UNKNOWN_HOST = "unknown host - DNS could not resolve the domain [NS_ERROR_UNKNOWN_HOST]"


def _nav_failed(tab_id=3, **extra):
    return resp({"ok": True, "op": "goto", "tabId": tab_id, "url": "about:neterror",
                 "aboutBlank": False, "netError": UNKNOWN_HOST, **extra})


@pytest.mark.asyncio
async def test_onion_without_a_proxy_names_the_call_that_works(tools, server):
    server.extension_proxy_access = {"has_proxy_permission": True}
    server.set_response("tabs.routing", resp({"tabId": 3, "routed": False, "type": "direct"}))
    server.set_response("navigation.navigate", _nav_failed())
    out = await (await _fn(tools, "navigate"))(url=ONION, tab_id=3)
    assert "HINT: .onion addresses resolve only inside Tor" in out
    assert f'navigate(url="{ONION}", tab_id=3, proxy="tor")' in out


@pytest.mark.asyncio
async def test_onion_hint_says_how_to_grant_the_permission_when_it_is_missing(tools, server):
    server.extension_proxy_access = {"has_proxy_permission": False}
    server.set_response("tabs.routing", resp({"tabId": 3, "routed": False}))
    server.set_response("navigation.navigate", _nav_failed())
    out = await (await _fn(tools, "navigate"))(url=ONION, tab_id=3)
    assert "Proxy routing is not allowed in this browser yet" in out and "about:addons" in out
    assert 'proxy="tor"' in out


@pytest.mark.asyncio
async def test_no_onion_hint_when_the_tab_is_already_routed(tools, server):
    server.extension_proxy_access = {"has_proxy_permission": True}
    server.set_response("tabs.routing", resp({"tabId": 3, "routed": True,
                                              "endpoint": TOR_ENDPOINT}))
    server.set_response("navigation.navigate", _nav_failed())
    out = await (await _fn(tools, "navigate"))(url=ONION, tab_id=3)
    assert "HINT" not in out         # the hidden service is down; Tor is not the answer


@pytest.mark.asyncio
async def test_no_onion_hint_for_an_explicit_proxy_a_clearnet_host_or_another_error(
        tools, server, fake_tor):
    server.extension_proxy_access = {"has_proxy_permission": True}
    server.set_response("tabs.routing", resp({"tabId": 3, "routed": False}))
    fn = await _fn(tools, "navigate")
    server.set_response("navigation.navigate", _nav_failed())
    assert "HINT" not in await fn(url=ONION, tab_id=3, proxy="tor")
    assert "HINT" not in await fn(url=ONION, tab_id=3, proxy="socks5://127.0.0.1:9050")
    assert "HINT" not in await fn(url="http://example.invalid/", tab_id=3)
    server.set_response("navigation.navigate", resp({
        "ok": True, "op": "goto", "tabId": 3, "url": "about:neterror",
        "netError": "connection timed out [NS_ERROR_NET_TIMEOUT]"}))
    assert "HINT" not in await fn(url=ONION, tab_id=3)


@pytest.mark.asyncio
async def test_tabs_open_of_an_onion_without_a_proxy_names_the_call_that_works(tools, server):
    server.extension_proxy_access = {"has_proxy_permission": True}
    server.set_response("tabs.create", resp({"tab": {
        "id": 9, "windowId": 10, "url": "about:neterror", "navPending": ONION,
        "navError": UNKNOWN_HOST}}))
    out = await (await _fn(tools, "tabs"))(action="open", url=ONION)
    assert f'tabs(action="open", url="{ONION}", proxy="tor")' in out
    assert out.count("HINT") == 1


# =============================================================================
# Chromium: `proxy` switches the WHOLE browser, and the result says so
# =============================================================================

class FakeBlink:
    engine = "blink"
    slot = 2

    def __init__(self, proxy_access):
        self.proxy_access = proxy_access

    def describe(self):
        return "[2] Google Chrome Canary pid 24188"


class BlinkServer(MockServer):
    def __init__(self, proxy_access=None):
        super().__init__()
        self.browser = FakeBlink(proxy_access if proxy_access is not None else {
            "has_proxy_permission": True, "proxy_supported": True, "proxy_scope": "browser"})

    def live_browsers(self):
        return [self.browser]

    def resolve_target_browser(self, client_key):
        return self.browser


@pytest.fixture
def blink():
    return BlinkServer()


@pytest.fixture
def blink_tools(blink):
    return BrowserPlugTools(blink)


ON = {"off": False, "endpoint": TOR_ENDPOINT, "scope": "browser", "already": False}


@pytest.mark.asyncio
async def test_a_current_chromium_is_no_longer_refused_and_gets_the_endpoint(
        blink_tools, blink, fake_tor):
    blink.set_response("tabs.create", resp({
        "tab": {"id": 9, "windowId": 3, "url": ONION}, "browserProxyChange": ON}))
    out = await (await _fn(blink_tools, "tabs"))(action="open", url=ONION, proxy="tor")
    assert blink.sent[-1]["data"]["proxy"] == TOR_ENDPOINT
    assert "BROWSER-WIDE PROXY ON" in out


@pytest.mark.asyncio
async def test_the_on_notice_says_what_the_agent_needs_to_know(blink_tools, blink, fake_tor):
    blink.set_response("tabs.create", resp({
        "tab": {"id": 9, "windowId": 3, "url": ONION}, "browserProxyChange": ON}))
    out = await (await _fn(blink_tools, "tabs"))(action="open", url=ONION, proxy="tor")
    assert "ENTIRE BROWSER" in out and "[2] Google Chrome Canary" in out        # it happened
    assert f"Tor ({TOR_ENDPOINT})" in out                                       # to what
    assert "including the ones the user may be using right now" in out
    assert 'tabs(action="routing", proxy="")' in out and 'proxy=""' in out      # how to undo
    assert "first opportunity after you finish the current set of operations" in out
    assert "free" in out and "all tabs" in out and "actively using" in out      # re-enabling


@pytest.mark.asyncio
async def test_the_notice_survives_a_navigation_that_failed(blink_tools, blink, fake_tor):
    blink.set_response("tabs.create", resp({"tab": {
        "id": 9, "windowId": 3, "url": "about:neterror", "navPending": ONION,
        "navError": "connection refused [net::ERR_CONNECTION_REFUSED]"},
        "browserProxyChange": ON}))
    out = await (await _fn(blink_tools, "tabs"))(action="open", url=ONION, proxy="tor")
    assert "WARNING: navigation" in out and "BROWSER-WIDE PROXY ON" in out   # the switch is not lost


@pytest.mark.asyncio
async def test_navigate_reports_the_switch_too(blink_tools, blink, fake_tor):
    blink.set_response("navigation.navigate", resp({
        "ok": True, "op": "goto", "tabId": 3, "url": ONION, "settled": True,
        "browserProxyChange": ON}))
    out = await (await _fn(blink_tools, "navigate"))(url=ONION, tab_id=3, proxy="tor")
    assert "BROWSER-WIDE PROXY ON" in out


@pytest.mark.asyncio
async def test_switching_off_is_reported_and_names_what_it_was(blink_tools, blink):
    blink.set_response("tabs.routing", resp({
        "tabId": 3, "routed": False, "type": "direct", "browserProxy": None,
        "browserProxyChange": {"off": True, "was": TOR_ENDPOINT, "scope": "browser"}}))
    out = await (await _fn(blink_tools, "tabs"))(action="routing", tab_id=3, proxy="")
    assert blink.sent[-1]["data"]["proxy"] == ""
    assert "BROWSER-WIDE PROXY OFF" in out and TOR_ENDPOINT in out
    assert "Tab 3 is direct (no proxy)" in out


@pytest.mark.asyncio
async def test_routing_only_takes_the_off_values_and_only_on_a_browser_wide_browser(
        blink_tools, blink, tools, server):
    fn = await _fn(blink_tools, "tabs")
    assert (await fn(action="routing", proxy=TOR_ENDPOINT)).startswith("Error: action=\"routing\" only accepts")
    assert blink.sent == []
    ffn = await _fn(tools, "tabs")
    out = await ffn(action="routing", proxy="")
    assert "only switches off a browser-wide proxy, which Chromium has" in out
    assert server.sent == []


@pytest.mark.asyncio
async def test_the_readbacks_remind_the_agent_while_it_is_on(blink_tools, blink):
    blink.set_response("tabs.routing", resp({
        "tabId": 3, "routed": False, "browserProxy": {"endpoint": TOR_ENDPOINT}}))
    routing = await (await _fn(blink_tools, "tabs"))(action="routing", tab_id=3)
    assert "has no per-tab proxy" in routing and "BROWSER-WIDE PROXY ON" in routing
    blink.set_response("tabs.list", resp({
        "tabs": [{"id": 1, "url": "http://x/", "title": "X", "active": True, "windowId": 3,
                  "pinned": False}],
        "windows": [{"id": 3, "focused": True, "state": "normal", "incognito": False}],
        "browserProxy": {"endpoint": TOR_ENDPOINT}}))
    listing = await (await _fn(blink_tools, "tabs"))()
    assert listing.startswith("BROWSER-WIDE PROXY ON") and "id: 1 active" in listing


@pytest.mark.asyncio
async def test_an_outdated_chromium_build_is_still_refused_before_the_wire(fake_tor):
    old = BlinkServer({"has_proxy_permission": False, "proxy_supported": False})
    out = await (await _fn(BrowserPlugTools(old), "tabs"))(
        action="open", url="http://x/", proxy="socks5://127.0.0.1:9050")
    assert "does not support proxy routing" in out and "reload the BrowserPlug extension" in out
    assert old.sent == [] and fake_tor.starts == 0


@pytest.mark.asyncio
async def test_the_onion_hint_on_chromium_offers_the_call_with_the_warning(blink_tools, blink):
    blink.set_response("tabs.routing", resp({"tabId": 3, "routed": False, "browserProxy": None}))
    blink.set_response("navigation.navigate", _nav_failed())
    out = await (await _fn(blink_tools, "navigate"))(url=ONION, tab_id=3)
    assert 'proxy="tor"' in out and "switches the ENTIRE browser" in out
    assert "cannot do per-tab" not in out          # it is no longer a refusal


@pytest.mark.asyncio
async def test_the_onion_hint_on_chromium_says_where_to_grant_the_permission():
    no_grant = BlinkServer({"has_proxy_permission": False, "proxy_supported": True,
                            "proxy_scope": "browser"})
    no_grant.set_response("tabs.routing", resp({"tabId": 3, "routed": False}))
    no_grant.set_response("navigation.navigate", _nav_failed())
    out = await (await _fn(BrowserPlugTools(no_grant), "navigate"))(url=ONION, tab_id=3)
    assert "BrowserPlug popup" in out and "Allow the browser-wide proxy" in out
    assert "about:addons" not in out


@pytest.mark.asyncio
async def test_no_onion_hint_while_a_browser_wide_proxy_is_already_on(blink_tools, blink):
    blink.set_response("tabs.routing", resp({
        "tabId": 3, "routed": False, "browserProxy": {"endpoint": TOR_ENDPOINT}}))
    blink.set_response("navigation.navigate", _nav_failed())
    out = await (await _fn(blink_tools, "navigate"))(url=ONION, tab_id=3)
    assert "HINT" not in out


# =============================================================================
# isProxy: the generic "is this tab behind a proxy" answer (not a Tor one)
# =============================================================================

@pytest.mark.asyncio
async def test_routing_says_isproxy_for_any_socks_endpoint_and_for_a_direct_tab(tools, server):
    fn = await _fn(tools, "tabs")
    server.set_response("tabs.routing", resp({
        "tabId": 5, "routed": True, "type": "socks",
        "endpoint": "socks5://203.0.113.9:1080", "proxyDNS": True}))
    routed = await fn(action="routing", tab_id=5)
    assert "isProxy: true" in routed and "socks5://203.0.113.9:1080" in routed

    server.set_response("tabs.routing", resp({"tabId": 6, "routed": False, "type": "direct"}))
    direct = await fn(action="routing", tab_id=6)
    assert "Tab 6 is direct (no proxy)" in direct and "isProxy: false" in direct


@pytest.mark.asyncio
async def test_chromium_isproxy_is_true_for_every_tab_while_the_browser_is_switched(
        blink_tools, blink):
    blink.set_response("tabs.routing", resp({
        "tabId": 3, "routed": False, "browserProxy": {"endpoint": TOR_ENDPOINT}}))
    on = await (await _fn(blink_tools, "tabs"))(action="routing", tab_id=3)
    assert "has no per-tab proxy" in on and "isProxy: true" in on    # inferred (older build)

    blink.set_response("tabs.routing", resp({
        "tabId": 3, "routed": False, "browserProxy": None, "isProxy": False}))
    off = await (await _fn(blink_tools, "tabs"))(action="routing", tab_id=3)
    assert "isProxy: false" in off


@pytest.mark.asyncio
async def test_the_extensions_own_isproxy_beats_the_inference(blink_tools, blink):
    # Our note says a proxy is set, but the browser says another extension took it over.
    blink.set_response("tabs.routing", resp({
        "tabId": 3, "routed": False, "browserProxy": {"endpoint": TOR_ENDPOINT},
        "isProxy": False}))
    out = await (await _fn(blink_tools, "tabs"))(action="routing", tab_id=3)
    assert "isProxy: false" in out


@pytest.mark.asyncio
async def test_a_chromium_tab_list_marks_every_tab_proxied_while_the_browser_is_switched(
        blink_tools, blink):
    blink.set_response("tabs.list", resp({
        "tabs": [
            {"id": 1, "url": "http://a/", "title": "A", "active": True, "windowId": 3, "pinned": False},
            {"id": 2, "url": "http://b/", "title": "B", "active": False, "windowId": 3, "pinned": False}],
        "windows": [{"id": 3, "focused": True, "state": "normal", "incognito": False}],
        "browserProxy": {"endpoint": TOR_ENDPOINT}}))
    out = await (await _fn(blink_tools, "tabs"))()
    assert out.count(f"proxied:{TOR_ENDPOINT}") == 2

    blink.set_response("tabs.list", resp({
        "tabs": [{"id": 1, "url": "http://a/", "title": "A", "active": True, "windowId": 3,
                  "pinned": False}],
        "windows": [{"id": 3, "focused": True, "state": "normal", "incognito": False}],
        "browserProxy": None}))
    assert "proxied:" not in await (await _fn(blink_tools, "tabs"))()


@pytest.mark.asyncio
async def test_a_plain_socks5_address_is_sent_as_given_and_never_starts_tor(
        tools, server, fake_tor):
    server.set_response("tabs.create", resp({"tab": {
        "id": 8, "windowId": 1, "url": "http://x/", "proxy": "socks5://203.0.113.9:1080"}}))
    await (await _fn(tools, "tabs"))(action="open", url="http://x/",
                                     proxy="socks5://203.0.113.9:1080")
    assert server.sent[-1]["data"]["proxy"] == "socks5://203.0.113.9:1080"
    assert fake_tor.starts == 0
