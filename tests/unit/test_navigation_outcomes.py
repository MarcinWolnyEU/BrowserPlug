"""Failed and partial navigations must reach the client as failed / degraded calls.

`instructions.py` promises "Failures set the MCP error flag". Before this file, navigate
recorded nothing for a DNS failure, a closed tab, a superseded navigation or an
interstitial, so each came back as a clean success. Each test drives the real tool
function and reads the record `tool_outcome` keeps for the call, which is what the
middleware turns into `isError` / `_meta`.
"""

import pytest

import test_imports  # noqa: F401
from server.tools import tool_outcome
from server.tools.mcp_tools import BrowserPlugTools
from server.tools import browsers as browsers_tool
from test_agent_economy import MockServer, READ_OK, SETTLED_NAV


@pytest.fixture
def server():
    return MockServer()


@pytest.fixture
def tools(server):
    return BrowserPlugTools(server)


async def call(tools, name, **kw):
    tool_outcome.arm()
    fn = (await tools.mcp.get_tool(name)).fn
    out = await fn(**kw)
    return out, tool_outcome.read()


def nav(**over):
    d = dict(SETTLED_NAV)
    d.update(over)
    return d


def kinds(outcome):
    return [m["kind"] for m in (outcome or {}).get("degraded", [])]


# --- navigate: hard failures ------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [
    {"netError": "DNS lookup failed (NS_ERROR_UNKNOWN_HOST)", "settled": False},
    {"aboutBlank": True, "pendingUrl": "https://x.example/", "settled": False},
    {"tabClosed": True, "settled": False},
    {"superseded": True, "settled": False},
], ids=["netError", "aboutBlank", "tabClosed", "superseded"])
async def test_navigate_records_a_failure(tools, server, extra):
    server.set_response("navigation.navigate", nav(**extra))
    out, oc = await call(tools, "navigate", url="https://example.org/")
    assert oc and oc.get("failed"), oc
    assert oc["code"] == tool_outcome.PAGE_ERROR
    assert oc["message"] == out                       # the prose is unchanged


@pytest.mark.asyncio
async def test_a_blocked_landing_is_a_failure(tools, server):
    server.set_response("navigation.navigate", nav(
        url="https://example.org/captcha/verify", title="Are you a robot?"))
    out, oc = await call(tools, "navigate", url="https://example.org/private")
    assert "BLOCKED" in out
    assert oc and oc.get("failed"), oc


@pytest.mark.asyncio
async def test_a_failed_navigation_with_a_skipped_read_fails_with_the_whole_text(tools, server):
    server.set_response("navigation.navigate", nav(netError="NS_ERROR_CONNECTION_REFUSED",
                                                  settled=False))
    out, oc = await call(tools, "navigate", url="https://example.org/", read="markdown")
    assert "READ SKIPPED" in out
    assert oc and oc.get("failed") and oc["message"] == out


# --- navigate: degradations -------------------------------------------------

@pytest.mark.asyncio
async def test_http_error_status_degrades_and_keeps_the_page_readable(tools, server):
    server.set_response("navigation.navigate", nav(httpStatus=404))
    server.set_response("content.read", READ_OK)
    out, oc = await call(tools, "navigate", url="https://example.org/", read="markdown")
    assert "HTTP 404" in out
    assert "This domain is for use in examples." in out     # a 404 page may be wanted
    assert oc and not oc.get("failed")
    assert "http_error" in kinds(oc)


@pytest.mark.asyncio
async def test_landing_elsewhere_degrades(tools, server):
    server.set_response("navigation.navigate", nav(url="https://example.org/other/path"))
    out, oc = await call(tools, "navigate", url="https://example.org/wanted")
    assert "LANDED ELSEWHERE" in out
    assert oc and not oc.get("failed")
    assert "landed_elsewhere" in kinds(oc)


@pytest.mark.asyncio
async def test_a_skipped_read_on_a_cross_site_landing_degrades(tools, server):
    server.set_response("navigation.navigate", nav(url="https://en.wikipedia.org/wiki/X"))
    server.set_response("content.read", READ_OK)
    out, oc = await call(tools, "navigate", url="https://hardwaredirect.pl/x", read="markdown")
    assert "READ SKIPPED" in out
    assert oc and not oc.get("failed")
    assert "read_skipped" in kinds(oc)


@pytest.mark.asyncio
async def test_a_clean_navigation_records_nothing(tools, server):
    server.set_response("navigation.navigate", SETTLED_NAV)
    out, oc = await call(tools, "navigate", url="https://example.org/")
    assert oc is None


# --- tabs open --------------------------------------------------------------

@pytest.mark.asyncio
async def test_tabs_open_with_a_navigation_error_fails(tools, server):
    server.set_response("tabs.create", {"tab": {
        "id": 9, "windowId": 1, "url": "about:neterror?e=dnsNotFound",
        "navPending": "https://nope.invalid/", "navError": "NS_ERROR_UNKNOWN_HOST"}})
    out, oc = await call(tools, "tabs", action="open", url="https://nope.invalid/")
    assert "WARNING: navigation to" in out
    assert oc and oc.get("failed"), oc


@pytest.mark.asyncio
async def test_tabs_open_window_with_a_navigation_error_fails(tools, server):
    server.set_response("windows.create", {"window": {
        "id": 2, "tabs": [{"id": 9, "url": "about:neterror"}],
        "navPending": "https://nope.invalid/", "navError": "NS_ERROR_UNKNOWN_HOST"}})
    out, oc = await call(tools, "tabs", action="open", url="https://nope.invalid/",
                         new_window=True)
    assert oc and oc.get("failed"), oc


@pytest.mark.asyncio
async def test_tabs_open_navigation_timeout_degrades(tools, server):
    server.set_response("tabs.create", {"tab": {
        "id": 9, "windowId": 1, "url": "about:blank",
        "navPending": "https://slow.example/", "navTimedOut": True}})
    out, oc = await call(tools, "tabs", action="open", url="https://slow.example/")
    assert oc and not oc.get("failed")
    assert "nav_uncommitted" in kinds(oc)


# --- truncated records ------------------------------------------------------

@pytest.mark.asyncio
async def test_truncated_records_degrade(tools, server):
    server.set_response("content.snapshot", {
        "ok": True, "mode": "records", "count": 2, "totalItems": 9, "records": [], "detection": {},
        "pageState": {"url": "https://example.org/", "title": "T"}})
    out, oc = await call(tools, "page_snapshot", mode="records", tab_id=1)
    assert "TRUNCATED" in out
    assert "truncated" in kinds(oc)


@pytest.mark.asyncio
async def test_fused_truncated_records_degrade(tools, server):
    server.set_response("navigation.navigate", SETTLED_NAV)
    server.set_response("content.snapshot", {
        "ok": True, "mode": "records", "count": 2, "totalItems": 9, "records": [], "detection": {}})
    out, oc = await call(tools, "navigate", url="https://example.org/", read="records")
    assert "TRUNCATED" in out
    assert "truncated" in kinds(oc)


# --- browsers tool ----------------------------------------------------------

class _B:
    def __init__(self, slot, name):
        self.slot, self.name, self.instance_id = slot, name, name
        self.edition = self.friendly = name
        self.browser_info = {"version": "150"}
        self.is_remote = False
        self.remote_addr = self.connected_at = self.last_activity = self.last_action = None
        self.engine = "gecko"

    def describe(self):
        return f"[{self.slot}] {self.name}"


class _Calls:
    def __init__(self, live, routing_error=None):
        self.live, self.routing_error = live, routing_error

    def connected_browsers(self):
        return self.live

    def target_browser(self):
        return (self.live[0] if self.live else None), self.routing_error

    def preferred_browser(self, live):
        return live[0]


async def browsers_call(calls, **kw):
    fn = browsers_tool.make_browsers_tool(calls)
    tool_outcome.arm()
    out = await fn(**kw)
    return out, tool_outcome.read()


LIVE = [_B(1, "Firefox"), _B(2, "Zen")]


@pytest.mark.asyncio
@pytest.mark.parametrize("kw", [
    {"action": "use"},
    {"action": "use", "target": "opera"},
    {"action": "use", "target": "9"},
    {"action": "swtich"},
], ids=["no-target", "no-match", "no-slot", "unknown-action"])
async def test_browsers_error_branches_fail(kw):
    out, oc = await browsers_call(_Calls(LIVE), **kw)
    assert oc and oc.get("failed"), (kw, oc)
    assert oc["message"] == out


@pytest.mark.asyncio
async def test_browsers_with_none_connected_fails():
    out, oc = await browsers_call(_Calls([]))
    assert oc and oc.get("failed"), oc
    assert oc["code"] == tool_outcome.EXTENSION_DISCONNECTED


@pytest.mark.asyncio
async def test_browsers_list_with_a_vanished_selection_degrades():
    out, oc = await browsers_call(_Calls(LIVE, routing_error="Your browser went away."))
    assert oc and not oc.get("failed")
    assert "selection_lost" in kinds(oc)


@pytest.mark.asyncio
async def test_browsers_list_clean_records_nothing():
    out, oc = await browsers_call(_Calls(LIVE))
    assert oc is None
