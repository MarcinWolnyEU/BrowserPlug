"""Unit tests for first-class CHROMIUM (Blink) browser support on the server side.

Covers the four server-side change-list items that make a Chrome/Edge/Brave/… MV3
extension a first-class client alongside Firefox and its forks (no Firefox, no psutil):

  1. identity probe  — browser_brand()/(gecko_brand shim)/browser_variant, the Chromium
     process-brand table, and the runtime-sibling fallback for an unknown fork;
  2. engine-aware defaults — claimed_names + ExtBrowser.family/edition never default a
     Blink browser to "Firefox"; ExtBrowser.engine flows from the hello for a remote one;
  3. labels: browser_line renders a Blink browser as "{edition} {ver}", never the bogus
     "(Firefox {ver})" a gecko fork gets;
  4. proxy safety — the pre-wire capability check refuses a proxy=… call routed to a
     browser that cannot do per-tab SOCKS routing, naming a capable Firefox to switch to.

The gecko-side behavior these share is pinned in test_browser_registry.py; here we prove
the Blink path and that the gecko path is untouched.
"""

from unittest.mock import AsyncMock

import pytest

import test_imports  # noqa: F401
from facade_double import FacadeDouble
from server.browsers.browser_identity import (ExtBrowser, _gecko_dir_cache,
                                              browser_brand, browser_variant, chromium_variant,
                                              claimed_names, subbrand_edition)
from brand_probe import gecko_brand
from server.server import BrowserPlugServer
from server.tools.mcp_tools import BrowserPlugTools
from server.tools.browsers import browser_line


# =============================================================================
# 1. Identity probe — browser_brand / gecko_brand shim / variant
# =============================================================================

def test_browser_brand_on_gecko_stems_reports_gecko_engine():
    assert browser_brand("firefox.exe", None) == ("Firefox", "gecko")
    assert browser_brand("zen.exe", r"C:\Program Files\Zen Browser\zen.exe") \
        == ("Zen Browser", "gecko")
    assert browser_brand("librewolf", "/usr/lib/librewolf/librewolf") \
        == ("LibreWolf", "gecko")


def test_browser_brand_on_chromium_stems_reports_blink_engine():
    assert browser_brand("chrome.exe", None) == ("Google Chrome", "blink")
    assert browser_brand("msedge.exe", None) == ("Microsoft Edge", "blink")
    assert browser_brand("brave.exe", None) == ("Brave", "blink")
    assert browser_brand("vivaldi.exe", None) == ("Vivaldi", "blink")
    assert browser_brand("opera.exe", None) == ("Opera", "blink")


def test_an_unknown_stem_is_a_chromium_fork_if_a_chromium_runtime_sits_beside_it(tmp_path):
    """The Blink analog of the xul.dll fallback: a browser nobody has heard of yet is
    still recognized (and gets a pid + name) from the Chromium runtime next to its exe."""
    exe = tmp_path / "moonchrome.exe"
    exe.write_text("")
    assert browser_brand("moonchrome.exe", str(exe)) == (None, None)   # nothing says so yet
    (tmp_path / "chrome_elf.dll").write_text("")                       # …now it does
    _gecko_dir_cache.clear()
    assert browser_brand("moonchrome.exe", str(exe)) == ("Moonchrome", "blink")


def test_an_unknown_stem_with_no_runtime_siblings_is_not_a_browser(tmp_path):
    """A python test harness faking the extension must NOT be probed as a browser —
    that is the guard that keeps two of its sockets from merging into one browser."""
    py = tmp_path / "python.exe"
    py.write_text("")
    assert browser_brand("python.exe", str(py)) == (None, None)
    assert browser_brand("node.exe", str(tmp_path / "node.exe")) == (None, None)
    assert browser_brand("", None) == (None, None)


def test_gecko_brand_shim_keeps_gecko_and_drops_blink():
    """gecko_brand stays a thin compat wrapper: a family for gecko, None for Blink."""
    assert gecko_brand("firefox.exe", None) == "Firefox"
    assert gecko_brand("zen.exe", r"C:\Program Files\Zen Browser\zen.exe") == "Zen Browser"
    assert gecko_brand("chrome.exe", None) is None      # a Blink browser is not a gecko brand
    assert gecko_brand("msedge.exe", None) is None
    assert gecko_brand("python.exe", None) is None


def test_chromium_variant_names_the_channel_from_the_install_path():
    assert chromium_variant("Google Chrome", r"C:\Program Files\Google\Chrome\chrome.exe") \
        == "Google Chrome"
    assert chromium_variant("Google Chrome", r"C:\Program Files\Google\Chrome Beta\chrome.exe") \
        == "Google Chrome Beta"
    assert chromium_variant("Google Chrome", r"C:\...\Google\Chrome SxS\chrome.exe") \
        == "Google Chrome Canary"
    assert chromium_variant("Microsoft Edge", r"C:\...\Microsoft\Edge Dev\msedge.exe") \
        == "Microsoft Edge Dev"


def test_browser_variant_routes_by_engine():
    # gecko -> gecko_variant (edition from path), blink -> chromium_variant (channel)
    assert browser_variant("Firefox", "gecko",
                           r"C:\Program Files\Firefox Developer Edition\firefox.exe") \
        == "Firefox Developer Edition"
    assert browser_variant("Google Chrome", "blink",
                           r"C:\...\Chrome Canary\chrome.exe") == "Google Chrome Canary"
    assert browser_variant(None, "blink", None) is None


# =============================================================================
# 2. Engine-aware defaults — claimed_names + ExtBrowser.family / edition / engine
# =============================================================================

def test_claimed_names_never_defaults_a_declared_blink_browser_to_firefox():
    # engine:"blink" self-declaration wins even when name is a generic brand.
    assert claimed_names({"name": "Google Chrome", "engine": "blink", "version": "151.0"}) \
        == ("Google Chrome", "Google Chrome")
    # A known Chromium brand name outs a Blink browser even without an engine field.
    assert claimed_names({"name": "Microsoft Edge", "version": "151.0"}) \
        == ("Microsoft Edge", "Microsoft Edge")
    # engine:"blink" with no usable name still refuses the Firefox default.
    assert claimed_names({"engine": "blink"}) == ("Chromium", "Chromium")


def test_claimed_names_keeps_the_gecko_path_intact():
    # A gecko browser (a fork lies "Firefox" over the API) still reads as Firefox.
    assert claimed_names({"name": "Firefox", "version": "152.0"}) == ("Firefox", "Firefox")
    assert claimed_names(None) == (None, None)
    assert claimed_names({}) == (None, None)


def test_extbrowser_family_edition_defaults_are_engine_aware():
    # A Blink browser with no probe brand and no browser_info never reads as "Firefox".
    blink = ExtBrowser(ws=object(), instance_id="c1", slot=1)
    blink.engine = "blink"
    assert blink.family == "Chromium"
    assert blink.edition == "Chromium"

    # engine None (a plain / remote Firefox) still defaults to Firefox — no regression.
    gecko = ExtBrowser(ws=object(), instance_id="f1", slot=2)
    assert gecko.family == "Firefox"
    assert gecko.edition == "Firefox"

    # A remote Blink browser names itself from its (self-declared) browser_info.
    edge = ExtBrowser(ws=object(), instance_id="e1", slot=3, is_remote=True)
    edge.engine = "blink"
    edge.browser_info = {"name": "Microsoft Edge", "engine": "blink", "version": "151.0"}
    assert edge.family == "Microsoft Edge"
    assert edge.edition == "Microsoft Edge"


def test_subbrand_edition_refines_a_family_with_the_browsers_own_brand():
    """Opera and Opera GX are one process name and one install parent; the UA brand list
    is the only thing that separates them."""
    gx = {"name": "Opera GX", "engine": "blink", "version": "135.0.5973.94"}
    assert subbrand_edition("Opera", "Opera", gx) == "Opera GX"
    # Plain Opera claims exactly its family: nothing to refine.
    assert subbrand_edition("Opera", "Opera",
                            {"name": "Opera", "engine": "blink"}) == "Opera"
    # A channel the install path found rides along instead of being dropped…
    assert subbrand_edition("Opera", "Opera Beta", gx) == "Opera GX Beta"
    # …and a word already in the claim is never said twice.
    assert subbrand_edition("Opera", "Opera GX", gx) == "Opera GX"


def test_subbrand_edition_refuses_a_claim_that_is_not_a_refinement():
    """Vivaldi ships a stock Chrome UA. A claim that does not EXTEND the family the probe
    found names a different browser, never a finer version of this one."""
    assert subbrand_edition("Vivaldi", "Vivaldi",
                            {"name": "Google Chrome", "engine": "blink"}) == "Vivaldi"
    # A gecko fork's "Firefox" claim never overwrites the edition read from the path.
    assert subbrand_edition("Firefox", "Firefox Developer Edition",
                            {"name": "Firefox", "version": "152.0"})         == "Firefox Developer Edition"
    assert subbrand_edition("Opera", "Opera", None) == "Opera"
    assert subbrand_edition(None, "Opera",
                            {"name": "Opera GX", "engine": "blink"}) == "Opera"


def test_a_local_opera_gx_reads_as_gx_and_plain_opera_stays_opera():
    """The end of the chain a person sees. Both processes are `opera.exe`, so the probe
    brands both "Opera"; the hello is what tells the two popup rows apart."""
    def _opera(slot, instance_id, pid, name, version):
        b = ExtBrowser(ws=object(), instance_id=instance_id, slot=slot)
        b.engine = "blink"
        b.brand = b.friendly = "Opera"          # all the process probe can say
        b.browser_pid = pid
        b.browser_info = {"name": name, "engine": "blink", "version": version}
        return b

    gx = _opera(1, "gx-1", 45216, "Opera GX", "135.0.5973.94")
    assert gx.family == "Opera"                 # the family is still the family
    assert gx.edition == "Opera GX"
    assert gx.display_name == "Opera GX"
    assert gx.name == "Opera GX pid 45216"
    assert gx.describe() == "[1] Opera GX pid 45216"

    plain = _opera(2, "op-1", 9912, "Opera", "135.0.5973.133")
    assert plain.family == "Opera"
    assert plain.edition == "Opera"
    assert plain.describe() == "[2] Opera pid 9912"


def test_remote_browser_engine_comes_from_the_hello():
    """A REMOTE browser has no process to probe — the hello's browserInfo.engine is the
    only authoritative engine signal, and BrowserRegistry.handle_hello must record it."""
    srv = BrowserPlugServer(host="localhost", port=8772, start_mcp=False)
    ws = AsyncMock()
    ws.close_code = None
    b = ExtBrowser(ws=ws, instance_id="pending-1", slot=1, is_remote=True)
    srv.browsers["pending-1"] = b

    out = srv.registry.handle_hello(b, {
        "instanceId": "chrome-remote-1",
        "browserInfo": {"name": "Google Chrome", "engine": "blink", "version": "151.0"},
    })
    assert out.engine == "blink"
    assert out.family == "Google Chrome"

    # A Firefox hello carries no engine; the field stays None (gecko/unknown default).
    ws2 = AsyncMock()
    ws2.close_code = None
    b2 = ExtBrowser(ws=ws2, instance_id="pending-2", slot=2, is_remote=True)
    srv.browsers["pending-2"] = b2
    out2 = srv.registry.handle_hello(b2, {
        "instanceId": "ff-remote-1",
        "browserInfo": {"name": "Firefox", "version": "152.0"},
    })
    assert out2.engine is None
    assert out2.family == "Firefox"


# =============================================================================
# 3. Labels: browser_line renders Blink honestly (no bogus "(Firefox {ver})")
# =============================================================================

@pytest.fixture
def tools():
    """A BrowserPlugTools over a bare mock server — enough for the pure label builder."""
    return BrowserPlugTools(FacadeDouble())


def test_browser_line_for_a_blink_browser_names_its_own_version(tools):
    b = ExtBrowser(ws=object(), instance_id="chrome-1", slot=1)
    b.engine = "blink"
    b.brand = b.friendly = "Google Chrome"
    b.browser_info = {"name": "Google Chrome", "engine": "blink",
                      "version": "151.0.7922.34"}
    line = browser_line(b)
    assert "Google Chrome 151.0.7922.34" in line
    assert "(Firefox" not in line          # a Blink browser has no "built-on-Firefox" base


def test_browser_line_for_a_gecko_fork_still_labels_the_firefox_base(tools):
    """The gecko fork rendering is untouched: Zen 1.21.6b (Firefox 152.0.5)."""
    b = ExtBrowser(ws=object(), instance_id="zen-1", slot=2)
    b.engine = "gecko"
    b.brand = b.friendly = "Zen Browser"
    b.browser_info = {"name": "Firefox", "version": "152.0.5",
                      "zen": {"version": "1.21.6b"}}
    line = browser_line(b)
    assert "Zen Browser 1.21.6b (Firefox 152.0.5)" in line


# =============================================================================
# 4. Proxy safety — pre-wire capability check + PROXY_UNSUPPORTED enrichment
# =============================================================================

def _browser(slot, engine, proxy_access=None, brand=None):
    """A real ExtBrowser (so describe()/family/name behave exactly as in production),
    with just the engine + proxy grant the proxy-capability helpers read set on it."""
    b = ExtBrowser(ws=object(), instance_id=f"b{slot}", slot=slot)
    b.engine = engine
    b.proxy_access = proxy_access
    if brand:
        b.brand = b.friendly = brand
    return b


class MockRoutingServer(FacadeDouble):
    """A server double that routes to a designated target and lists connected browsers."""

    def __init__(self, target, browsers):
        super().__init__()
        self._target = target
        self._browsers = list(browsers)
        self.sent = []
        self.responses = {}

    def resolve_target_browser(self, client_key):
        return self._target

    def live_browsers(self):
        return list(self._browsers)

    async def send_request_and_wait(self, request, timeout=30.0, browser=None):
        self.sent.append(request)
        action = request.get("action", "")
        return self.responses.get(action, {"type": "response", "data": {"ok": True}})


def _resp(data):
    return {"type": "response", "data": data}


async def _fn(tools, name):
    return (await tools.mcp.get_tool(name)).fn


@pytest.mark.asyncio
async def test_proxy_to_an_outdated_blink_build_is_refused_before_the_wire_and_names_a_firefox():
    # An extension that reports proxy_supported:false predates the browser-wide proxy.
    blink = _browser(2, "blink", {"has_proxy_permission": False, "proxy_supported": False})
    gecko = _browser(1, "gecko", {"has_proxy_permission": True})   # no proxy_supported == supported
    server = MockRoutingServer(target=blink, browsers=[gecko, blink])
    tools = BrowserPlugTools(server)

    fn = await _fn(tools, "tabs")
    out = await fn(action="open", url="http://svc.onion/", proxy="socks5://127.0.0.1:9050")

    assert out.startswith("Error:")
    assert "does not support proxy routing" in out
    assert 'browsers(action="use", target=1)' in out    # names the capable Firefox
    assert server.sent == []                             # fail-closed: never hit the wire


@pytest.mark.asyncio
async def test_navigate_proxy_to_an_outdated_blink_build_is_refused_before_the_wire():
    blink = _browser(1, "blink", {"has_proxy_permission": False, "proxy_supported": False})
    server = MockRoutingServer(target=blink, browsers=[blink])
    tools = BrowserPlugTools(server)

    fn = await _fn(tools, "navigate")
    out = await fn(url="http://svc.onion/", proxy="socks5://127.0.0.1:9050")

    assert out.startswith("Error:")
    assert "does not support proxy routing" in out
    # No capable browser connected -> it says to reload the extension, but STILL refuses.
    assert "reload the BrowserPlug extension" in out
    assert server.sent == []


@pytest.mark.asyncio
async def test_proxy_to_a_current_blink_build_goes_on_the_wire_and_switches_the_browser():
    """A Chromium that reports proxy_scope 'browser' is no longer refused: the proxy
    switches the whole browser, and the result says so."""
    blink = _browser(1, "blink", {"has_proxy_permission": True, "proxy_supported": True,
                                  "proxy_scope": "browser"})
    server = MockRoutingServer(target=blink, browsers=[blink])
    server.responses["tabs.create"] = _resp({
        "tab": {"id": 7, "windowId": 1, "url": "http://svc.onion/"},
        "browserProxyChange": {"off": False, "endpoint": "socks5://127.0.0.1:9050",
                               "scope": "browser", "already": False}})
    tools = BrowserPlugTools(server)

    fn = await _fn(tools, "tabs")
    out = await fn(action="open", url="http://svc.onion/", proxy="socks5://127.0.0.1:9050")

    assert server.sent[-1]["data"]["proxy"] == "socks5://127.0.0.1:9050"
    assert "BROWSER-WIDE PROXY ON" in out and "ENTIRE BROWSER" in out


@pytest.mark.asyncio
async def test_proxy_to_a_gecko_target_is_unaffected():
    gecko = _browser(1, "gecko", {"has_proxy_permission": True})
    server = MockRoutingServer(target=gecko, browsers=[gecko])
    server.responses["tabs.create"] = _resp({
        "tab": {"id": 7, "windowId": 1, "url": "http://svc.onion/",
                "proxy": "socks5://127.0.0.1:9050"}})
    tools = BrowserPlugTools(server)

    fn = await _fn(tools, "tabs")
    out = await fn(action="open", url="http://svc.onion/", proxy="socks5://127.0.0.1:9050")

    assert not out.startswith("Error")
    assert server.sent, "a gecko target must reach the wire"
    assert server.sent[-1]["data"]["proxy"] == "socks5://127.0.0.1:9050"


@pytest.mark.asyncio
async def test_missing_proxy_supported_on_a_gecko_target_is_treated_as_supported():
    """A Firefox that predates proxy_supported omits it entirely — MISSING must read as
    supported (only an explicit False blocks); the call proceeds."""
    for pa in ({"has_proxy_permission": True}, None, {}):
        gecko = _browser(1, "gecko", pa)
        server = MockRoutingServer(target=gecko, browsers=[gecko])
        server.responses["navigation.navigate"] = _resp({
            "ok": True, "op": "goto", "tabId": 1, "url": "http://x/", "settled": True,
            "waitedMs": 1})
        tools = BrowserPlugTools(server)
        fn = await _fn(tools, "navigate")
        out = await fn(url="http://x/", proxy="socks5://127.0.0.1:9050")
        assert not out.startswith("Error"), f"proxy_access={pa!r} should be allowed"
        assert server.sent[-1]["data"]["proxy"] == "socks5://127.0.0.1:9050"


@pytest.mark.asyncio
async def test_explicit_proxy_supported_false_blocks_even_a_gecko_shaped_target():
    """proxy_supported:False is a direct 'this engine cannot' signal — block it whatever
    the engine label says."""
    incapable = _browser(1, "gecko", {"has_proxy_permission": True,
                                         "proxy_supported": False})
    server = MockRoutingServer(target=incapable, browsers=[incapable])
    tools = BrowserPlugTools(server)
    fn = await _fn(tools, "tabs")
    out = await fn(action="open", url="http://x/", proxy="socks5://127.0.0.1:9050")
    assert out.startswith("Error:")
    assert server.sent == []


@pytest.mark.asyncio
async def test_clearing_proxy_is_always_allowed_even_to_a_blink_target():
    """'' and 'direct' clear routing — they are never blocked by the capability check."""
    blink = _browser(1, "blink", {"has_proxy_permission": False, "proxy_supported": False})
    server = MockRoutingServer(target=blink, browsers=[blink])
    server.responses["navigation.navigate"] = _resp({
        "ok": True, "op": "goto", "tabId": 1, "url": "http://x/", "settled": True,
        "waitedMs": 1})
    tools = BrowserPlugTools(server)
    fn = await _fn(tools, "navigate")
    for clearing in ("", "direct"):
        server.sent.clear()
        out = await fn(url="http://x/", proxy=clearing)
        assert not out.startswith("Error")
        assert server.sent[-1]["data"]["proxy"] == clearing


def test_error_of_enriches_a_proxy_unsupported_code_with_the_switch_hint():
    """Even if the pre-wire check is bypassed, a PROXY_UNSUPPORTED error from the extension
    is enriched by error_of with the 'switch to a Firefox' hint."""
    gecko = _browser(1, "gecko", {"has_proxy_permission": True})
    blink = _browser(2, "blink", {"has_proxy_permission": False, "proxy_supported": False})
    server = MockRoutingServer(target=blink, browsers=[gecko, blink])
    tools = BrowserPlugTools(server)

    enriched = tools.calls.error_of({"error": "PROXY_UNSUPPORTED: chrome.proxy is global-only"})
    assert "Chromium-based" in enriched
    assert 'browsers(action="use", target=1)' in enriched     # names the capable Firefox
    assert "PROXY_UNSUPPORTED" in enriched                    # the raw error is preserved


def test_error_of_proxy_unsupported_without_a_capable_browser_still_explains():
    blink = _browser(1, "blink", {"has_proxy_permission": False, "proxy_supported": False})
    server = MockRoutingServer(target=blink, browsers=[blink])
    tools = BrowserPlugTools(server)
    enriched = tools.calls.error_of({"error": "PROXY_UNSUPPORTED: nope"})
    assert "Chromium-based" in enriched
    assert "PROXY_UNSUPPORTED" in enriched
