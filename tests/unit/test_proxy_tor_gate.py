"""proxy="tor" must not start Tor for a call the browser is going to refuse.

`tor_proxy.refuse_before_start` reads the target browser's grant from its last hello
before `resolve` may download or start anything (.tmp/dev-docs/tor.md). Each tool test
drives the real tool function over the mock harness from test_proxy_tools and counts both
`resolve` calls and Tor starts, so "refused" here means neither happened and nothing went
on the wire.
"""

import pytest

import test_imports  # noqa: F401
from server.tools import tool_outcome, tor_proxy
from server.tools.mcp_tools import BrowserPlugTools
from test_proxy_tools import GRANTED, ONION, TOR_ENDPOINT, BlinkServer, FakeTor, MockServer, resp

NO_GRANT = {"has_proxy_permission": False}
CHROMIUM_NO_SWITCH = {"has_proxy_permission": False, "proxy_supported": True,
                      "proxy_scope": "browser"}
GECKO_REFUSAL = ('SOCKS proxy routing needs the optional "proxy" permission (Control browser '
                 'proxy settings). Enable it in about:addons ▸ BrowserPlug ▸ Permissions, '
                 'then retry.')
GECKO_GRANT_TEXT = ("Error: Proxy routing is not allowed in this browser yet; grant the "
                    "optional proxy permission in about:addons ▸ Permissions, then retry.")
NO_BROWSER = "No extension connection available"


class TorSpy:
    def __init__(self):
        self.tor = FakeTor()
        self.resolved = []

    @property
    def starts(self):
        return self.tor.starts


@pytest.fixture
def spy(monkeypatch):
    """Counts `tor_proxy.resolve` calls and Tor starts; the tools look both up per call."""
    from server.tor import manager
    s = TorSpy()
    monkeypatch.setattr(manager, "shared", lambda: s.tor)
    real_resolve = tor_proxy.resolve

    async def counting_resolve(proxy):
        s.resolved.append(proxy)
        return await real_resolve(proxy)

    monkeypatch.setattr(tor_proxy, "resolve", counting_resolve)
    return s


async def call(server, name, **kw):
    tool_outcome.arm()
    fn = (await BrowserPlugTools(server).mcp.get_tool(name)).fn
    out = await fn(**kw)
    return out, tool_outcome.read() or {}


OPEN = ("tabs", {"action": "open", "url": ONION, "proxy": "tor"})
NAVIGATE = ("navigate", {"url": ONION, "tab_id": 3, "proxy": "tor"})
BOTH_TOOLS = pytest.mark.parametrize("tool,kw", [OPEN, NAVIGATE], ids=["tabs_open", "navigate"])


class FakeGecko:
    engine = "gecko"
    # Read by the `browsers` roster, which exists while two browsers are connected.
    browser_info = None
    friendly = None
    is_remote = False
    remote_addr = None

    def __init__(self, slot, name, proxy_access):
        self.slot = slot
        self.name = name
        self.proxy_access = proxy_access

    def describe(self):
        return f"[{self.slot}] {self.name}"


class RoutedServer(MockServer):
    """Routes every call to `target`; `others` are connected too."""

    def __init__(self, target, *others):
        super().__init__()
        self.target = target
        self.others = list(others)

    def live_browsers(self):
        return [self.target, *self.others]

    def resolve_target_browser(self, client_key):
        return self.target


class EmptyServer(MockServer):
    """Nothing connected. `launch` is what starting the pinned browser brings up, if anything."""

    def __init__(self, launch=None):
        super().__init__()
        self.launch = launch
        self.up = None
        self.launches = 0

    def live_browsers(self):
        return [self.up] if self.up else []

    def resolve_target_browser(self, client_key):
        return self.up or NO_BROWSER

    async def ensure_default_browser_running(self):
        self.launches += 1
        self.up = self.launch
        return self.up


# --- the decision, without a browser --------------------------------------------------

@pytest.mark.parametrize("proxy,kw,expected", [
    (None, {}, None),
    ("", {}, None),
    ("direct", {}, None),
    # A plain endpoint starts nothing; the extension alone checks its grant.
    ("socks5://127.0.0.1:9050", {"proxy_access": NO_GRANT}, None),
    ("tor", {"proxy_access": GRANTED}, None),
    (" TOR ", {"proxy_access": GRANTED}, None),
    ("tor", {"proxy_access": {"has_proxy_permission": True, "proxy_supported": True,
                              "proxy_scope": "browser"}}, None),
    ("tor", {"proxy_access": NO_GRANT}, tor_proxy.NOT_PERMITTED),
    ("tor", {"proxy_access": CHROMIUM_NO_SWITCH}, tor_proxy.NOT_PERMITTED),
    # Firefox reports null when permissions.contains failed: unknown is not granted.
    ("tor", {"proxy_access": {"has_proxy_permission": None}}, tor_proxy.NOT_PERMITTED),
    ("tor", {"proxy_access": {}}, tor_proxy.NOT_PERMITTED),       # a hello without the field
    ("tor", {"proxy_access": None}, tor_proxy.NOT_PERMITTED),     # no hello yet
    ("tor", {"routing_error": NO_BROWSER, "proxy_access": GRANTED}, tor_proxy.NO_BROWSER),
], ids=lambda v: repr(v)[:40])
def test_start_blocker(proxy, kw, expected):
    assert tor_proxy.start_blocker(proxy, **kw) == expected


# --- the tools: refused before resolve, so Tor never starts -------------------------

@BOTH_TOOLS
@pytest.mark.parametrize("access", [None, {}, NO_GRANT, {"has_proxy_permission": None}],
                         ids=["no_hello", "no_field", "false", "unknown"])
@pytest.mark.asyncio
async def test_firefox_without_the_grant_never_starts_tor(spy, tool, kw, access):
    server = MockServer()
    server.extension_proxy_access = access
    out, outcome = await call(server, tool, **kw)
    assert out == GECKO_GRANT_TEXT
    assert spy.resolved == [] and spy.starts == 0
    assert server.sent == []
    assert outcome["failed"] and outcome["code"] == "PROXY_PERMISSION_REQUIRED"


@BOTH_TOOLS
@pytest.mark.asyncio
async def test_chromium_with_the_switch_off_never_starts_tor(spy, tool, kw):
    server = BlinkServer(CHROMIUM_NO_SWITCH)
    out, outcome = await call(server, tool, **kw)
    assert out.startswith("Error: Proxy routing is not allowed in this browser yet; turn it on "
                          "in the BrowserPlug popup (toolbar button, Permissions, SOCKS proxy, "
                          "Allow the browser-wide proxy), then retry.")
    assert "about:addons" not in out
    assert spy.resolved == [] and spy.starts == 0
    assert server.sent == []
    assert outcome["failed"]


@BOTH_TOOLS
@pytest.mark.asyncio
async def test_the_refusal_names_a_connected_browser_that_holds_the_grant(spy, tool, kw):
    server = RoutedServer(FakeGecko(1, "Firefox pid 101", NO_GRANT),
                          FakeGecko(2, "Zen Browser pid 102", GRANTED))
    out, _ = await call(server, tool, **kw)
    assert "[2] Zen Browser pid 102 allows it" in out
    assert 'browsers(action="use", target=2)' in out
    assert "Extension error" not in out          # the extension was never asked
    assert spy.starts == 0 and server.sent == []


@BOTH_TOOLS
@pytest.mark.asyncio
async def test_the_grant_is_read_from_the_target_not_from_any_browser(spy, tool, kw):
    # The `proxy` argument is visible because SOME browser holds the grant; the call
    # still lands in the one that does not.
    server = RoutedServer(FakeGecko(1, "Firefox pid 101", NO_GRANT),
                          FakeGecko(2, "Zen Browser pid 102", GRANTED))
    await call(server, tool, **kw)
    assert spy.starts == 0
    server.target, server.others = server.others[0], [server.target]
    server.set_response("tabs.create", resp({"tab": {"id": 9, "windowId": 1, "url": ONION,
                                                     "proxy": TOR_ENDPOINT}}))
    server.set_response("navigation.navigate", resp({"ok": True, "op": "goto", "tabId": 3,
                                                     "url": ONION, "settled": True}))
    await call(server, tool, **kw)
    assert spy.starts == 1 and server.sent[-1]["data"]["proxy"] == TOR_ENDPOINT


@BOTH_TOOLS
@pytest.mark.asyncio
async def test_a_stale_grant_still_meets_the_extensions_refusal(spy, tool, kw):
    # The hello said granted; the permission went away after it. Tor starts (the cost of
    # trusting the hello), and the extension refuses the call: nothing loads.
    server = MockServer()
    server.extension_proxy_access = GRANTED
    refusal = {"type": "error", "data": {"code": "PROXY_PERMISSION_REQUIRED",
                                         "message": GECKO_REFUSAL}}
    server.set_response("tabs.create", refusal)
    server.set_response("navigation.navigate", refusal)
    out, outcome = await call(server, tool, **kw)
    assert spy.starts == 1
    assert server.sent[-1]["data"]["proxy"] == TOR_ENDPOINT
    assert outcome["failed"]
    assert GECKO_REFUSAL in out
    assert "Opened tab" not in out and "routed via" not in out


@BOTH_TOOLS
@pytest.mark.asyncio
async def test_with_nothing_connected_tor_never_starts(spy, tool, kw):
    server = EmptyServer()
    out, outcome = await call(server, tool, **kw)
    assert out.startswith(f"Error: {NO_BROWSER}")
    assert server.launches == 1                  # the pinned browser was tried, as a call would
    assert spy.starts == 0 and server.sent == []
    assert outcome["code"] == tool_outcome.EXTENSION_DISCONNECTED


@BOTH_TOOLS
@pytest.mark.parametrize("access,starts", [(GRANTED, 1), (NO_GRANT, 0)],
                         ids=["granted", "not_granted"])
@pytest.mark.asyncio
async def test_a_browser_started_for_the_call_is_checked_before_tor(spy, tool, kw, access,
                                                                    starts):
    server = EmptyServer(launch=FakeGecko(1, "Firefox pid 101", access))
    server.set_response("tabs.create", resp({"tab": {"id": 9, "windowId": 1, "url": ONION,
                                                     "proxy": TOR_ENDPOINT}}))
    server.set_response("navigation.navigate", resp({"ok": True, "op": "goto", "tabId": 3,
                                                     "url": ONION, "settled": True}))
    await call(server, tool, **kw)
    assert server.launches == 1                  # started once, not again by the request
    assert spy.starts == starts
    assert len(server.sent) == starts


@pytest.mark.asyncio
async def test_navigate_goto_without_a_url_never_starts_tor(spy):
    # The server refuses it before Tor or the wire. Both extensions refuse it too, before
    # they touch the routing, for a caller that reaches them with a plain SOCKS endpoint.
    server = MockServer()
    server.extension_proxy_access = GRANTED
    out, outcome = await call(server, "navigate", proxy="tor")
    assert out.startswith("Error navigating: url is required for goto")
    assert outcome["code"] == tool_outcome.INVALID_ARGUMENT
    assert spy.resolved == [] and spy.starts == 0 and server.sent == []

    server.set_response("navigation.navigate", resp({"ok": True, "op": "reload", "tabId": 3,
                                                     "url": ONION, "settled": True}))
    await call(server, "navigate", action="reload", tab_id=3, proxy="tor")
    assert spy.starts == 1                       # reload needs no url


@pytest.mark.asyncio
async def test_the_extension_error_hint_keeps_its_wording():
    server = MockServer()
    err = BrowserPlugTools(server).calls.error_of({"error": "PROXY_PERMISSION_REQUIRED: x"})
    assert err == ("Proxy routing is not allowed in this browser yet; grant the optional proxy "
                   "permission in about:addons ▸ Permissions, then retry. Extension error: "
                   "PROXY_PERMISSION_REQUIRED: x")
