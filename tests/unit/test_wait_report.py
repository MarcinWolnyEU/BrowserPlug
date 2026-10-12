"""What navigate says about its wait, the read-drift note, and two error paths.

`server/tools/wait_report.py` is pure, so most of this drives it directly; the rest goes
through the real tool functions over the mock server from test_page_tools, because a
helper that is right but never called is the failure this codebase keeps meeting:

* wait="dom" (load, then 100 ms with no DOM change, at most 3 s): the result names the
  wait it ran and says so when it gave up with the DOM still moving;
* the `timing:` line of a slow or timed-out settle;
* the main-frame events printed under a reported abort;
* `content_changed`, when the extension re-extracted a page whose length moved;
* the extension's own refusal code reaching the operator verdict and the grant hint;
* the fix named for a private window Firefox will not open.
"""

import pytest

import test_imports  # noqa: F401
from server.tools import fused_read, tool_outcome, wait_report
from server.tools.mcp_tools import BrowserPlugTools
from server.tools.navigate import render_navigation
from server.tools.response_hints import (INCOGNITO_DISABLED, INCOGNITO_NOT_ALLOWED,
                                         incognito_hint)
from test_page_tools import MockServer, resp

NAV = {"ok": True, "op": "goto", "tabId": 7, "url": "https://example.org/",
       "title": "Example Domain", "settled": True, "waitedMs": 380}


def nav(**over):
    out = dict(NAV)
    out.update(over)
    return out


async def call(server, name, **kw):
    tool_outcome.arm()
    fn = (await BrowserPlugTools(server).mcp.get_tool(name)).fn
    out = await fn(**kw)
    return out, tool_outcome.read() or {}


# ---- which wait ran ----------------------------------------------------------------

def test_each_wait_names_itself():
    assert wait_report.settled_phrase(nav(waitMode="settled"), "settled") == " (settled in 380ms)"
    assert wait_report.settled_phrase(nav(waitMode="load"), "load") == " (load event in 380ms)"
    assert wait_report.settled_phrase(nav(waitMode="dom", domStable=True), "dom") == (
        " (load event, then DOM unchanged for 100ms, in 380ms)")
    assert wait_report.settled_phrase(nav(waitMode="dom", domStable=False, waitedMs=3400),
                                      "dom") == (
        " (load event; returned after 3.4s with the DOM not yet stable)")


def test_an_extension_that_reports_no_wait_ran_its_default_for_dom():
    # 1.0.1 and earlier settle on network quiet for any value they do not know.
    r = nav()
    assert wait_report.wait_mode(r, "dom") == "settled"
    assert wait_report.wait_mode(r, "load") == "load"
    assert wait_report.settled_phrase(r, "dom") == " (settled in 380ms)"
    assert "does not know wait=\"dom\"" in wait_report.dom_note(r, "dom")


def test_dom_notes_fire_only_when_the_dom_wait_fell_short():
    assert wait_report.dom_note(nav(waitMode="dom", domStable=True), "dom") == ""
    assert wait_report.dom_note(nav(waitMode="settled"), "settled") == ""
    unstable = wait_report.dom_note(nav(waitMode="dom", domStable=False), "dom")
    assert "still changing 3 s after the load event" in unstable
    assert 'wait="settled"' in unstable
    blind = wait_report.dom_note(nav(waitMode="dom", domStable=False,
                                     domError="No tab with id: 7"), "dom")
    assert "could not be watched (No tab with id: 7)" in blind
    # A settle that timed out says so on its own; the DOM wait never ran.
    assert wait_report.dom_note(nav(waitMode="dom", settled=False, timedOut=True), "dom") == ""


# ---- the timing line ---------------------------------------------------------------

def test_a_quick_settle_carries_no_timing_line():
    r = nav(waitMode="settled", timing={"commit": 90, "load": 120, "quiet": 380, "settled": 380})
    assert wait_report.timing_line(r, "settled") == ""


def test_a_slow_settle_names_the_phase_that_held_it():
    r = nav(waitMode="settled", waitedMs=4100,
            timing={"commit": 120, "load": 900, "quiet": 4100, "settled": 4100})
    assert wait_report.timing_line(r, "settled") == (
        "\n  timing: commit 120ms, load 900ms, network quiet 4.1s")


def test_a_timed_out_settle_says_which_phase_never_came():
    r = nav(waitMode="settled", settled=False, timedOut=True, waitedMs=20000,
            timing={"commit": 300})
    assert wait_report.timing_line(r, "settled") == (
        "\n  timing: commit 300ms, load never, network quiet never")
    r = nav(waitMode="dom", waitedMs=3600, domStable=False,
            timing={"commit": 300, "load": 600})
    assert wait_report.timing_line(r, "dom") == "\n  timing: commit 300ms, load 600ms, DOM stable never"


# ---- the events behind an abort ------------------------------------------------------

ABORT_TEXT = ("request aborted - usually a newer navigation replaced it, or the tab was "
              "stopped/closed [NS_BINDING_ABORTED = 2152398850]")


def test_a_reported_abort_lists_the_main_frame_events_in_arrival_order():
    r = nav(netError=ABORT_TEXT, navEvents=[
        {"ev": "commit", "url": "https://example.org/", "ms": 586},
        {"ev": "error", "url": "https://example.org/", "ms": 601, "error": "Error code 2152398850"},
        {"ev": "commit", "url": "about:blank", "ms": -12},
    ])
    assert wait_report.nav_events_line(r) == (
        "\n  main-frame events: commit +586ms, abort +601ms, commit -12ms (about:blank)")


def test_other_net_errors_and_clean_results_carry_no_events():
    events = [{"ev": "error", "url": "https://x.invalid/", "ms": 40}]
    assert wait_report.nav_events_line(nav(netError="unknown host [NS_ERROR_UNKNOWN_HOST]",
                                           navEvents=events)) == ""
    assert wait_report.nav_events_line(nav(navEvents=events)) == ""


def test_render_puts_the_events_under_the_net_error():
    tool_outcome.arm()
    out = render_navigation(nav(netError=ABORT_TEXT, navEvents=[
        {"ev": "commit", "url": "https://example.org/", "ms": 586}]),
        action="goto", url="https://example.org/", tab_id=7, wait="settled")
    lines = out.splitlines()
    i = next(n for n, line in enumerate(lines) if line.startswith("  net error:"))
    assert lines[i + 1] == "  main-frame events: commit +586ms"


# ---- through the navigate tool -------------------------------------------------------

@pytest.mark.asyncio
async def test_navigate_sends_wait_dom_and_reports_an_unstable_dom():
    server = MockServer()
    server.set_response("navigation.navigate", resp(nav(waitMode="dom", domStable=False,
                                                        waitedMs=3500)))
    out, outcome = await call(server, "navigate", url="https://example.org/", tab_id=7,
                              wait=" DOM ")
    assert server.sent[-1]["data"]["wait"] == "dom"
    assert "(load event; returned after 3.5s with the DOM not yet stable)" in out
    assert "NOTE: the DOM was still changing" in out
    assert not outcome.get("failed")
    assert any(d.get("kind") == "dom_unstable" for d in outcome.get("degraded", [])), outcome


@pytest.mark.asyncio
async def test_navigate_wait_load_says_load_event():
    server = MockServer()
    server.set_response("navigation.navigate", resp(nav(waitMode="load", waitedMs=151)))
    out, _ = await call(server, "navigate", url="https://example.org/", tab_id=7, wait="load")
    assert '"Example Domain" (load event in 151ms)' in out


@pytest.mark.asyncio
async def test_an_unknown_wait_lists_dom_among_the_valid_values():
    server = MockServer()
    out, outcome = await call(server, "navigate", url="https://example.org/", wait="idle")
    assert outcome.get("code") == tool_outcome.INVALID_ARGUMENT
    assert "dom" in out and server.sent == []


def test_a_fused_read_under_an_unstable_dom_is_labelled():
    assert fused_read.separator_for(nav(waitMode="dom", domStable=False), "markdown") == (
        fused_read.SEPARATOR_DOM_UNSTABLE)
    assert fused_read.separator_for(nav(waitMode="dom", domStable=True), "markdown") == (
        fused_read.SEPARATOR_TEXT)


# ---- reads whose page changed under them ---------------------------------------------

READ = {"ok": True, "format": "markdown", "url": "https://en.wikipedia.org/wiki/Firefox",
        "title": "Firefox", "totalChars": 248130, "offset": 20000,
        "content": "x" * 20000, "docMetrics": {"nodes": 9000, "bodyChars": 250000}}


@pytest.mark.asyncio
async def test_page_read_says_the_page_changed_and_records_it():
    server = MockServer()
    server.set_response("content.read", resp(dict(READ, contentChanged={"previousTotal": 249500})))
    out, outcome = await call(server, "page_read", tab_id=7, offset=20000)
    assert ("NOTE: the page changed since the previous read of it (249500 -> 248130 chars); "
            "this is the current page, and offsets from earlier reads may not line up") in out
    changed = [d for d in outcome.get("degraded", []) if d.get("kind") == "content_changed"]
    assert changed and changed[0]["previous_total"] == 249500 and changed[0]["total"] == 248130


@pytest.mark.asyncio
async def test_a_cached_or_unchanged_read_carries_no_note():
    server = MockServer()
    server.set_response("content.read", resp(dict(READ, cached=True)))
    out, outcome = await call(server, "page_read", tab_id=7, offset=20000)
    assert "page changed" not in out
    assert not any(d.get("kind") == "content_changed" for d in outcome.get("degraded", []))


@pytest.mark.asyncio
async def test_the_fused_read_leg_says_it_too():
    server = MockServer()
    server.set_response("navigation.navigate", resp(nav(waitMode="settled",
                                                        url=READ["url"], title="Firefox")))
    server.set_response("content.read", resp(dict(READ, offset=0,
                                                  contentChanged={"previousTotal": 249500})))
    out, _ = await call(server, "navigate", url=READ["url"], tab_id=7, read="markdown")
    assert "NOTE: the page changed since the previous read of it (249500 -> 248130 chars)" in out


# ---- the extension's own refusal code ------------------------------------------------

GECKO_REFUSAL = ('SOCKS proxy routing needs the optional "proxy" permission (Control browser '
                 'proxy settings). Enable it in about:addons ▸ BrowserPlug ▸ Permissions, '
                 'then retry.')


def test_the_code_on_an_extension_error_frame_is_read():
    frame = {"type": "error", "data": {"code": "PROXY_PERMISSION_REQUIRED", "message": "x"}}
    assert tool_outcome.extension_code(frame) == "PROXY_PERMISSION_REQUIRED"
    assert tool_outcome.extension_code(
        {"type": "error", "data": {"code": "API_ERROR", "message": "x"}}) is None
    assert tool_outcome.extension_code({"type": "error", "data": "bare"}) is None
    assert tool_outcome.extension_code({"type": "response", "data": {}}) is None


@pytest.mark.asyncio
async def test_a_proxy_refusal_is_classified_and_gets_the_grant_hint():
    # The frame's message never spells PROXY_PERMISSION_REQUIRED, so the hint keyed on the
    # text and the classification keyed on the text both missed it, and a real refusal
    # was filed as PAGE_ERROR.
    server = MockServer()
    server.extension_proxy_access = {"has_proxy_permission": True}
    server.set_response("navigation.navigate", {
        "type": "error", "data": {"code": "PROXY_PERMISSION_REQUIRED", "message": GECKO_REFUSAL}})
    out, outcome = await call(server, "navigate", url="https://example.org/", tab_id=7,
                              proxy="socks5://127.0.0.1:9050")
    assert outcome["failed"] and outcome["code"] == "PROXY_PERMISSION_REQUIRED"
    assert outcome["retryable"] is False
    assert "Proxy routing is not allowed in this browser yet" in out
    assert GECKO_REFUSAL in out, "the extension's own sentence survives behind the hint"


@pytest.mark.asyncio
async def test_other_extension_codes_still_classify_from_the_text():
    server = MockServer()
    server.set_response("content.read", {
        "type": "error", "data": {"code": "API_ERROR", "message": "Content API error: boom"}})
    out, outcome = await call(server, "page_read", tab_id=7)
    assert outcome["code"] == tool_outcome.PAGE_ERROR
    assert out == "Error reading page: Content API error: boom"


# ---- a private window Firefox will not open ------------------------------------------

def test_the_incognito_refusals_name_their_fix():
    assert "Run in Private Windows > Allow" in incognito_hint(
        f"Windows API error: {INCOGNITO_NOT_ALLOWED}")
    assert "Private browsing is switched off" in incognito_hint(
        f"Windows API error: {INCOGNITO_DISABLED}")
    assert incognito_hint("Windows API error: Invalid window ID") == ""
    assert incognito_hint(None) == ""


@pytest.mark.asyncio
async def test_tabs_open_incognito_carries_the_hint():
    server = MockServer()
    server.set_response("windows.create", {"type": "error", "data": {
        "code": "API_ERROR", "message": f"Windows API error: {INCOGNITO_NOT_ALLOWED}"}})
    out, outcome = await call(server, "tabs", action="open", url="https://example.org/",
                              incognito=True)
    assert out.startswith(f"Error opening window: Windows API error: {INCOGNITO_NOT_ALLOWED}")
    assert "about:addons > BrowserPlug > Details > Run in Private Windows > Allow" in out
    assert outcome["failed"]
