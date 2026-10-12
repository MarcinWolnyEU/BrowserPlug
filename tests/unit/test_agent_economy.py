"""The tool-call-economy work, at the server's rendering layer.

Companion to test_page_tools.py (which pins the pre-existing surface) and to the modules'
own suites (test_url_diff, test_fused_read, test_tool_outcome, test_request_queue,
test_response_hints, test_preflight). What is pinned HERE is the part none of those can
see: what an agent actually READS back, once the pure functions and the extension's new
wire fields have been rendered into a response.

Every fixture payload in this file is shaped like a real one — several are transcribed
from live captures taken on 2026-08-03 against allegro.pl and example.org.
"""

import pytest

import test_imports  # noqa: F401
from facade_double import FacadeDouble
from server.tools import tool_outcome
from server.tools.mcp_tools import BrowserPlugTools
from server.tools.act import ActStep


class MockServer(FacadeDouble):
    """Minimal duck-typed websocket server: canned response per action, calls recorded."""

    def __init__(self):
        super().__init__()
        self.sent = []
        self.responses = {}

    def set_response(self, action, payload):
        self.responses[action] = payload

    async def send_request_and_wait(self, request, timeout=30.0, browser=None):
        self.sent.append(request)
        payload = self.responses.get(request["action"])
        if payload is None:
            return {"error": f"no canned response for {request['action']}"}
        return {"type": "response", "action": request["action"], "data": payload}

    @property
    def actions(self):
        return [r["action"] for r in self.sent]


@pytest.fixture
def server():
    return MockServer()


@pytest.fixture
def tools(server):
    return BrowserPlugTools(server)


async def _fn(tools, name):
    return (await tools.mcp.get_tool(name)).fn


SETTLED_NAV = {"ok": True, "op": "goto", "tabId": 7, "settled": True, "waitedMs": 300,
               "url": "https://example.org/", "title": "Example Domain", "httpStatus": 200}
READ_OK = {"ok": True, "format": "markdown", "url": "https://example.org/",
           "title": "Example Domain", "totalChars": 40, "offset": 0,
           "content": "This domain is for use in examples.", "docMetrics": {"nodes": 12,
                                                                            "bodyChars": 60}}


# =============================================================================
# A navigation should return the page it navigated to
# =============================================================================

@pytest.mark.asyncio
async def test_navigate_read_returns_diagnostics_and_content_in_one_call(tools, server):
    """The headline: 512 corpus navigations were immediately followed by a read of the
    same tab. One MCP call must now carry both."""
    server.set_response("navigation.navigate", SETTLED_NAV)
    server.set_response("content.read", READ_OK)
    out = await (await _fn(tools, "navigate"))(url="https://example.org/", read="markdown")

    assert "Navigated tab 7 -> https://example.org/" in out       # the diagnostics
    assert "This domain is for use in examples." in out           # ...and the page
    assert server.actions == ["navigation.navigate", "content.read"]


@pytest.mark.asyncio
async def test_omitting_read_leaves_the_payload_exactly_as_it_was(tools, server):
    """The fused read's last acceptance criterion. An agent that does not ask for content
    must see byte-identical output and must not pay for a second wire call."""
    server.set_response("navigation.navigate", SETTLED_NAV)
    server.set_response("content.read", READ_OK)
    out = await (await _fn(tools, "navigate"))(url="https://example.org/")

    assert out == 'Navigated tab 7 -> https://example.org/ — "Example Domain" (settled in 300ms)'
    assert server.actions == ["navigation.navigate"]


@pytest.mark.asyncio
async def test_a_navigation_that_landed_elsewhere_returns_the_failure_not_the_content(
        tools, server):
    """The whole risk of fusing: content from a page the agent never asked for, arriving in
    the SAME response that reports success. That is the landing-honesty problem in a new
    costume, so the refusal must be strict and must name the call that overrides it."""
    server.set_response("navigation.navigate", {
        "ok": True, "op": "goto", "tabId": 7, "settled": True, "waitedMs": 200,
        "url": "https://en.wikipedia.org/wiki/Nvidia_Tesla", "title": "Nvidia Tesla"})
    server.set_response("content.read", READ_OK)
    out = await (await _fn(tools, "navigate"))(
        url="https://hardwaredirect.pl/tesla-v100", read="markdown")

    assert "READ SKIPPED" in out
    assert "page_read(tab_id=7)" in out                    # the escape hatch, as a call
    assert "This domain is for use in examples." not in out
    assert server.actions == ["navigation.navigate"]       # the read leg never ran


@pytest.mark.asyncio
async def test_a_net_error_suppresses_the_read(tools, server):
    server.set_response("navigation.navigate", {
        "ok": True, "op": "goto", "tabId": 7, "url": "https://down.test/",
        "netError": "NS_ERROR_CONNECTION_REFUSED", "settled": False})
    server.set_response("content.read", READ_OK)
    out = await (await _fn(tools, "navigate"))(url="https://down.test/", read="markdown")
    assert "READ SKIPPED" in out
    assert server.actions == ["navigation.navigate"]


@pytest.mark.asyncio
async def test_an_unusable_read_pair_is_refused_before_the_wire(tools, server):
    """read= needs a loaded page; wait="none" returns before the load starts. Catching it
    locally costs nothing and says exactly what is wrong."""
    fn = await _fn(tools, "navigate")
    out = await fn(url="https://example.org/", read="markdown", wait="none")
    assert 'wait="none"' in out and "read=" in out
    assert await fn(url="https://example.org/", read="pdf") == \
        'Error: unknown read="pdf" (use markdown | text | html | records)'
    assert server.sent == []


@pytest.mark.asyncio
async def test_page_read_url_routes_through_the_same_fused_path(tools, server):
    """Agents passed url= to page_read six times before it existed. Implement it, and make
    it behave identically to navigate(read=) — including the refusals."""
    server.set_response("navigation.navigate", SETTLED_NAV)
    server.set_response("content.read", READ_OK)
    out = await (await _fn(tools, "page_read"))(url="https://example.org/")
    assert "This domain is for use in examples." in out
    assert server.actions == ["navigation.navigate", "content.read"]


# =============================================================================
# Paging past the end, and the silent empty read
# =============================================================================

@pytest.mark.asyncio
async def test_offset_past_the_end_names_the_cause_and_never_the_format_just_passed(
        tools, server):
    """Live-captured on 2026-08-03: reading example.org at offset=3500 answered
    `chars 3500-3500 of 149 (no content extracted — try format="text" ...)`. The response
    ALREADY KNEW the length and still advised changing the format — which was, in the
    corpus's own second instance, the format that had just been passed."""
    server.set_response("content.read", {
        "ok": True, "format": "text", "url": "https://example.org/", "title": "Example",
        "totalChars": 149, "offset": 3500, "content": "",
        "docMetrics": {"nodes": 12, "bodyChars": 149}})
    out = await (await _fn(tools, "page_read"))(format="text", offset=3500)

    assert "offset 3500 is past the end" in out
    assert "only 149 characters" in out
    assert 'try format="text"' not in out


@pytest.mark.asyncio
async def test_a_clamped_offset_says_it_returned_the_tail(tools, server):
    """With the clamping extension the read comes back with the FINAL window rather than
    nothing — but a caller comparing the offset it asked for against the one it got has to
    be told why they differ."""
    server.set_response("content.read", {
        "ok": True, "format": "text", "url": "u", "title": "t", "totalChars": 900,
        "offset": 400, "requestedOffset": 3500, "offsetClamped": True,
        "content": "the tail", "docMetrics": {"nodes": 30, "bodyChars": 900}})
    out = await (await _fn(tools, "page_read"))(format="text", offset=3500)
    assert "offset 3500 is past the end" in out
    assert "returned the LAST window" in out
    assert "the tail" in out


@pytest.mark.asyncio
async def test_an_empty_extraction_on_a_content_rich_page_is_flagged_as_extraction_failure(
        tools, server):
    """The morele.net / sipeed.com shape: a full page reduced to a chat widget. Nothing
    distinguished it from a genuinely empty page, so the agent moved on with 290 characters
    and no products."""
    server.set_response("content.read", {
        "ok": True, "format": "markdown", "url": "u", "title": "t", "totalChars": 0,
        "offset": 0, "content": "",
        "docMetrics": {"nodes": 812, "bodyChars": 4200}})
    out = await (await _fn(tools, "page_read"))()
    assert "EXTRACTION failure, not an empty page" in out
    assert "4200 characters" in out and "812 DOM nodes" in out
    assert 'format="text"' in out          # the move that actually helps here


@pytest.mark.asyncio
async def test_a_page_that_has_not_rendered_yet_is_distinguished_from_an_empty_one(
        tools, server):
    server.set_response("content.read", {
        "ok": True, "format": "markdown", "url": "u", "title": "t", "totalChars": 0,
        "offset": 0, "content": "",
        "docMetrics": {"nodes": 400, "bodyChars": 12, "dialogs": 1,
                       "sample": "Need help? Chat with us"}})
    out = await (await _fn(tools, "page_read"))()
    assert "still rendering or behind a consent/bot wall" in out
    assert "modal dialog" in out
    assert "wait_for" in out


@pytest.mark.asyncio
async def test_a_genuinely_empty_page_says_so_plainly(tools, server):
    """The signal only stays worth reading if it does not fire on a page that really is
    empty."""
    server.set_response("content.read", {
        "ok": True, "format": "markdown", "url": "u", "title": "t", "totalChars": 0,
        "offset": 0, "content": "", "docMetrics": {"nodes": 8, "bodyChars": 0}})
    out = await (await _fn(tools, "page_read"))()
    assert "really is almost empty" in out
    assert "EXTRACTION failure" not in out


@pytest.mark.asyncio
async def test_a_thin_but_NON_empty_extraction_is_flagged_too(tools, server):
    """The corpus cases were not empty, which is why an empty-only check misses them:
    morele.net returned 290 characters of live-chat modal and zero products, sipeed 140 of
    chrome. Each looked like a clean success."""
    server.set_response("content.read", {
        "ok": True, "format": "markdown", "url": "u", "title": "t", "totalChars": 290,
        "offset": 0, "content": "Need help? Chat with us. " * 11,
        "docMetrics": {"nodes": 900, "bodyChars": 9000}})
    out = await (await _fn(tools, "page_read"))()
    assert "the extraction dropped most of the content" in out
    assert "9000" in out


@pytest.mark.asyncio
async def test_a_paginated_read_of_a_long_article_is_NOT_called_thin(tools, server):
    """Caught live against Wikipedia on 2026-08-03: with read_max_chars=400 the returned
    WINDOW is 400 characters while the body holds 20,393 — and an earlier version of this
    check accused a perfectly healthy 40,448-character extraction of having dropped the
    content. Compare the whole extraction, never the slice, or the note fires on every
    paginated read and is therefore ignored on the one that matters."""
    server.set_response("content.read", {
        "ok": True, "format": "markdown", "url": "u", "title": "t", "totalChars": 40448,
        "offset": 0, "content": "x" * 400,
        "docMetrics": {"nodes": 4000, "bodyChars": 20393}})
    out = await (await _fn(tools, "page_read"))(max_chars=400)
    assert "dropped most of the content" not in out
    assert "continue with offset=400" in out       # pagination, working as intended


@pytest.mark.asyncio
async def test_without_extension_metrics_the_diagnosis_says_only_what_is_certain(
        tools, server):
    """An older extension sends no docMetrics. Inventing a cause would be worse than none —
    and this is also the shape every pre-existing mocked test uses."""
    server.set_response("content.read", {
        "ok": True, "format": "markdown", "url": "u", "title": "t", "totalChars": 0,
        "offset": 0, "content": ""})
    out = await (await _fn(tools, "page_read"))()
    assert "no content extracted" in out
    assert "DOM nodes" not in out


# =============================================================================
# Payloads bounded where they actually overflow
# =============================================================================

@pytest.mark.asyncio
async def test_the_full_tab_list_is_bounded_and_names_the_cheaper_primitive(tools, server):
    """tabs(recent=false) on a real profile returned 97,172 characters and exceeded the
    client's token limit outright, leaving only "almost nothing" and "too much to return"."""
    now = 1785762000000
    server.set_response("tabs.list", {
        "tabs": [{"id": i, "windowId": 1, "index": i, "title": f"Tab {i}",
                  "url": f"https://example.com/page/{i}", "lastAccessed": now - i * 1000}
                 for i in range(474)],
        "windows": [{"id": 1, "focused": True}]})
    out = await (await _fn(tools, "tabs"))(action="list", recent=False)

    assert len(out) < 40_000, f"the full list must stay bounded, got {len(out)} chars"
    assert "474 open tabs" in out
    assert 'tabs(action="find"' in out          # the primitive that actually answers this


@pytest.mark.asyncio
async def test_a_small_profile_still_lists_every_tab(tools, server):
    """The bound must not change what a normal profile sees."""
    server.set_response("tabs.list", {
        "tabs": [{"id": i, "windowId": 1, "index": i, "title": f"Tab {i}",
                  "url": f"https://example.com/{i}", "lastAccessed": 1785762000000 - i}
                 for i in range(6)],
        "windows": [{"id": 1, "focused": True}]})
    out = await (await _fn(tools, "tabs"))(action="list", recent=False)
    for i in range(6):
        assert f"id: {i}" in out
    assert "not shown" not in out


# =============================================================================
# An action reports what it CHANGED
# =============================================================================

def _act(results, ok=True):
    return {"ok": ok, "completedSteps": len(results), "totalSteps": len(results),
            "results": results}


@pytest.mark.asyncio
async def test_a_click_that_loaded_more_reports_the_quantity(tools, server):
    """euro.com.pl's "Pokaz wiecej produktow": `OK: 1/1` was returned both for a click that
    loaded 8 products and for a click that matched nothing, so 64 of 101 acts in the corpus
    were followed by a probe. The count must come back with the action."""
    server.set_response("content.act", _act([{
        "step": 0, "stepAction": "click", "ok": True, "domChanged": True,
        "nodesBefore": 300, "nodesAfter": 324, "nodeDelta": 24, "effect": "changed",
        "measureSettled": True,
        "counts": [{"selector": ".product", "before": 40, "after": 48, "delta": 8}]}]))
    out = await (await _fn(tools, "page_act"))(
        steps=[ActStep(action="click", target="Show more", count=[".product"])])

    assert "count .product: 40 -> 48 (+8)" in out
    assert "+24 element nodes" in out
    assert out.startswith("OK: 1/1 steps completed")   # the first line is load-bearing


@pytest.mark.asyncio
async def test_a_rerender_is_distinguished_from_no_effect(tools, server):
    """0 NET nodes with real churn means the list REPLACED its rows. Reading that as
    "nothing happened" is how a working click gets retried — and domChanged could never
    express the difference."""
    server.set_response("content.act", _act([{
        "step": 0, "stepAction": "click", "ok": True, "domChanged": True,
        "nodesBefore": 300, "nodesAfter": 300, "nodeDelta": 0, "effect": "rerendered",
        "nodesAdded": 40, "nodesRemoved": 40, "measureSettled": True}]))
    out = await (await _fn(tools, "page_act"))(steps=[ActStep(action="click", target="Next")])
    assert "re-rendered in place" in out
    assert "40 added / 40 removed, net 0" in out


@pytest.mark.asyncio
async def test_an_unsettled_measurement_is_reported_as_a_lower_bound(tools, server):
    """A carousel never quiesces. Reporting its count as final would be a confident lie."""
    server.set_response("content.act", _act([{
        "step": 0, "stepAction": "click", "ok": True, "domChanged": True,
        "nodesBefore": 10, "nodesAfter": 18, "nodeDelta": 8, "measureSettled": False}]))
    out = await (await _fn(tools, "page_act"))(steps=[ActStep(action="click", target="x")])
    assert "LOWER BOUND" in out


@pytest.mark.asyncio
async def test_a_measurement_that_failed_never_fails_the_step_it_measures(tools, server):
    server.set_response("content.act", _act([{
        "step": 0, "stepAction": "click", "ok": True, "domChanged": True,
        "measureFailed": True}]))
    out = await (await _fn(tools, "page_act"))(steps=[ActStep(action="click", target="x")])
    assert out.startswith("OK: 1/1")
    assert "change NOT measured" in out


@pytest.mark.asyncio
async def test_a_count_that_could_not_be_measured_is_never_rendered_as_zero(tools, server):
    """A silent 0 would read as "the selector matched nothing", which is a different and
    much more actionable fact than "your selector did not parse"."""
    server.set_response("content.act", _act([{
        "step": 0, "stepAction": "click", "ok": True, "domChanged": True,
        "counts": [{"selector": ".prod[", "error": "invalid selector"}]}]))
    out = await (await _fn(tools, "page_act"))(
        steps=[ActStep(action="click", target="x", count=[".prod["])])
    assert "NOT MEASURED — invalid selector" in out
    assert "0 -> 0" not in out


@pytest.mark.asyncio
async def test_watch_reports_the_transition_not_just_the_final_state(tools, server):
    """"visible, enabled" alone does not answer "did the Submit button enable?"."""
    server.set_response("content.act", _act([{
        "step": 0, "stepAction": "fill", "ok": True,
        "watchedBefore": [{"watch": "#submit", "found": True, "visible": True,
                           "enabled": False}],
        "watched": [{"watch": "#submit", "found": True, "ref": "e3", "visible": True,
                     "enabled": True}]}]))
    out = await (await _fn(tools, "page_act"))(
        steps=[ActStep(action="fill", target="#email", value="a@b.co", watch=["#submit"])])
    assert "visible, disabled -> visible, enabled" in out


@pytest.mark.asyncio
async def test_an_element_that_appeared_is_called_out_as_appearing(tools, server):
    server.set_response("content.act", _act([{
        "step": 0, "stepAction": "click", "ok": True,
        "watchedBefore": [{"watch": "#err", "found": False}],
        "watched": [{"watch": "#err", "found": True, "ref": "e9", "visible": True,
                     "enabled": True}]}]))
    out = await (await _fn(tools, "page_act"))(
        steps=[ActStep(action="click", target="Log in", watch=["#err"])])
    assert "APPEARED" in out


@pytest.mark.asyncio
async def test_a_batch_where_nothing_worked_is_an_error_but_a_partial_one_is_not(
        tools, server):
    """The degradation rule, applied to page_act: 0/N is a failure; k/N keeps the
    per-step detail reaching the agent as an ordinary result."""
    server.set_response("content.act", {
        "ok": False, "completedSteps": 0, "totalSteps": 1,
        "results": [{"step": 0, "stepAction": "click", "ok": False,
                     "error": "No element matched"}]})
    out = await (await _fn(tools, "page_act"))(steps=[ActStep(action="click", target="x")])
    assert out.startswith("FAILED: 0/1 steps completed")
    # Read the recorded outcome directly: calling .fn bypasses the middleware that would
    # normally convert it into a protocol error, which is the point of design (B) — the
    # TEXT is unchanged, only the structural record is new.
    assert tool_outcome.read()["failed"] is True

    server.set_response("content.act", {
        "ok": False, "completedSteps": 1, "totalSteps": 2,
        "results": [{"step": 0, "stepAction": "click", "ok": True, "domChanged": True},
                    {"step": 1, "stepAction": "click", "ok": False, "error": "nope"}]})
    tool_outcome.arm()
    out = await (await _fn(tools, "page_act"))(
        steps=[ActStep(action="click", target="a"), ActStep(action="click", target="b")])
    outcome = tool_outcome.read() or {}
    assert not outcome.get("failed")
    assert any(d["kind"] == "partial_steps" for d in outcome.get("degraded") or [])


# =============================================================================
# Steering from the response, where it demonstrably works
# =============================================================================

@pytest.mark.asyncio
async def test_a_click_on_a_checkbox_names_the_purpose_built_step(tools, server):
    """`check` was issued 0 times in 150 steps while every checkbox in the corpus went
    through `click` — nine of which reported success without changing state."""
    server.set_response("content.act", _act([{
        "step": 0, "stepAction": "click", "ok": True, "domChanged": True,
        "target": {"ref": "e5", "role": "checkbox", "name": "Uzywane"},
        "control": {"ref": "e5", "checked": True, "checkedBefore": False,
                    "role": "checkbox"}}]))
    out = await (await _fn(tools, "page_act"))(steps=[ActStep(action="click", target="e5")])
    assert "check" in out.lower()


@pytest.mark.asyncio
async def test_a_clean_ordinary_click_gets_no_hints_at_all(tools, server):
    """A hint that appears on every call is a hint that gets ignored — which is the exact
    failure mode the hint rules exist to remove."""
    server.set_response("content.act", _act([{
        "step": 0, "stepAction": "click", "ok": True, "domChanged": True,
        "nodeDelta": 3, "nodesBefore": 10, "nodesAfter": 13, "measureSettled": True,
        "target": {"ref": "e1", "role": "button", "name": "Save"}}]))
    out = await (await _fn(tools, "page_act"))(steps=[ActStep(action="click", target="Save")])
    assert "check" not in out.lower()
    assert "watch" not in out.lower()


# =============================================================================
# A filtered cookie read must announce that it is filtered
# =============================================================================

@pytest.mark.asyncio
async def test_a_document_cookie_read_warns_that_httponly_is_excluded(tools, server):
    """The incident: three cosmetic cookies came back, the agent matched one on a value
    coincidence, and OVERWROTE a working credential file with them."""
    server.set_response("content.execute_script", {
        "result": "fp=e850; lang=en", "url": "https://site.test/"})
    out = await (await _fn(tools, "page_eval"))(code="document.cookie")
    assert "CANNOT see HttpOnly cookies" in out
    assert "requests()" in out
    assert "do not overwrite a stored credential" in out


@pytest.mark.asyncio
async def test_unrelated_page_eval_is_not_annotated(tools, server):
    server.set_response("content.execute_script", {"result": 4, "url": "https://site.test/"})
    out = await (await _fn(tools, "page_eval"))(code="document.querySelectorAll('a').length")
    assert "HttpOnly" not in out
