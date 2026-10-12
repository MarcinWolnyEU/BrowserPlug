"""The `navigate` tool, and the read half of `navigate(read=)`.

`make_navigate_tool(calls)` returns the tool function; its signature and docstring are
pinned by `tests/unit/golden/tools_list.json`. The `proxy` parameter, and with it the
proxy text in its `Args:` entry, is pruned from the schema by `mcp_tools.py` unless the
proxy permission is granted. `page_read(url=)` calls this same function, so both
spellings behave identically.
"""

import json
from typing import Any, Callable, Dict, Optional

from server.tools.tool_outcome import degrade, fail, invalid

from server.tools.challenge import challenge_note
from server.tools.url_diff import landing_report

from server.tools import fused_read, wait_report

from server.tools.browser_calls import BrowserCalls, proxy_error
from server.tools import browser_proxy, tor_proxy
from server.tools.response_hints import missing_url_text
from server.tools.validation import enum_value, positive_ms, read_window
from server.tools.formatting import (content_changed_note, digest_suffix, empty_read_diagnosis,
                                     fmt_ms, fmt_records, service_workers_suffix, thin_read_note)


async def read_after_navigation(calls: BrowserCalls, nav: Dict[str, Any], read: str, *,
                                requested_url: Optional[str], scope: Optional[str],
                                max_chars: int, timeout: float, offset: int = 0,
                                frame_id: Optional[int] = None) -> str:
    """The read half of a fused navigate. Returns the block to append.

    512 navigations in the corpus were immediately followed by a read of the same tab;
    the most common two-call sequence in the data, and navigate already returned
    everything about the page EXCEPT the page.

    The whole risk of fusing them is landing-URL drift in a new costume: content
    extracted from a page the agent never asked for, now arriving in the SAME response
    that says the navigation succeeded. So the refusal check runs FIRST and is
    deliberately strict; a netError, a settle timeout, a superseded navigation, a closed
    tab or a landing on another site all suppress the read and say why, naming the
    page_read call that would override the judgement.
    """
    blocked = fused_read.read_block_reason(nav, requested_url)
    tab_id = nav.get("tabId")
    if blocked:
        # A navigation that already failed is recorded as a failure by the caller; this
        # mark covers the rest (a landing on another site), where the call itself is not
        # an error but the content the agent asked for was withheld.
        degrade("read_skipped", tool="navigate", reason=blocked)
        return fused_read.read_skipped_line(blocked, tab_id)

    action, data = fused_read.read_plan(read, tab_id=tab_id, scope=scope,
                                        max_chars=max_chars, offset=offset,
                                        frame_id=frame_id)
    response = await calls.request(action, data, timeout=timeout)
    err = calls.error_of(response)
    if err:
        # The NAVIGATION succeeded; only the read leg failed. Do not turn the whole
        # call into a failure; the agent still learned where it landed.
        degrade("read_leg_failed", tool="navigate", error=str(err))
        return fused_read.read_failed_line(err, tab_id)
    rdata = response.get("data") or {}
    if not rdata.get("ok"):
        degrade("read_leg_failed", tool="navigate", error=rdata.get("error"))
        return fused_read.read_failed_line(rdata.get("error", "unknown error"), tab_id)

    moved = fused_read.read_result_moved(nav, rdata)
    if moved:
        degrade("read_skipped", tool="navigate", reason=moved)
        return fused_read.read_skipped_line(moved, tab_id)

    head = fused_read.separator_for(nav, read)
    if read == "records":
        return head + "\n" + "\n".join(fmt_records(rdata))
    total = rdata.get("totalChars", 0)
    content = rdata.get("content", "") or ""
    end = rdata.get("offset", 0) + len(content)
    meta = f"[{rdata.get('format')}] chars {rdata.get('offset', 0)}-{end} of {total}"
    if end < total:
        # Same continuation vocabulary page_read uses; the one in-response hint the
        # corpus shows agents actually follow. It must be runnable as written: without
        # tab_id the follow-up reads the user's ACTIVE tab (this tab is usually a
        # background one), and without format/scope it re-reads as markdown, whose
        # offsets differ from a text read's.
        args = []
        if tab_id is not None:
            args.append(f"tab_id={tab_id}")
        args.append(f'format="{read}"')
        if scope:
            args.append(f"scope={json.dumps(scope)}")
        if frame_id is not None:
            args.append(f"frame_id={frame_id}")
        args.append(f"offset={end}")
        meta += f"; continue with page_read({', '.join(args)})"
        degrade("truncated", tool="navigate", returned=len(content), total=total,
                next_offset=end)
    changed = content_changed_note(rdata, "navigate")
    if changed:
        meta += " | " + changed
    if not content.strip():
        degrade("empty_extraction", tool="navigate", format=rdata.get("format"))
        return (head + "\n" + meta + "\n(no content extracted"
                + empty_read_diagnosis(rdata, read) + ")")
    thin = thin_read_note(rdata, content)
    if thin:
        degrade("thin_extraction", tool="navigate", returned=len(content))
    return head + "\n" + meta + thin + "\n\n" + content


# Markers `url_diff.landing_report` prints, mapped to the outcome they mean. The text is
# the contract here (tests/unit/test_url_diff.py pins it), so the check reads the report
# rather than re-deriving the URL comparison a second time.
_LANDING_MARKERS = (
    ("BLOCKED —", "blocked"),
    ("NAVIGATION DID NOT TAKE EFFECT", "no_effect"),
    ("LANDED ELSEWHERE", "landed_elsewhere"),
)


def _record_landing(r: Dict[str, Any], landing: str) -> str:
    """Record what `landing_report` said and hand the report back unchanged.

    HTTP >= 400 is a degradation, not a failure: a 404 page is a page the agent may
    want to read (and the fused read still returns it). Landing on an interstitial is
    the opposite, the requested document was never loaded, so `navigation_failure`
    treats it as a failure.
    """
    for marker, kind in _LANDING_MARKERS:
        if marker in landing:
            if kind != "blocked":
                degrade(kind, tool="navigate", landed=r.get("url"))
            break
    status = r.get("httpStatus")
    if isinstance(status, int) and not isinstance(status, bool) and status >= 400:
        degrade("http_error", tool="navigate", status=status)
    return landing


def navigation_failure(r: Dict[str, Any], text: str) -> bool:
    """True when the navigation did not put the requested page in the tab.

    Same causes `fused_read.read_block_reason` refuses a read for (net error, about:blank,
    superseded, closed tab), plus a landing on an interstitial (`BLOCKED`).
    """
    if any(r.get(flag) for flag, _ in fused_read._BLOCKING_FLAGS):
        return True
    return "BLOCKED —" in text


def render_navigation(r: Dict[str, Any], *, action: str, url: Optional[str],
                      tab_id: Optional[int], wait: str) -> str:
    """The one-paragraph report of a finished navigation (`navigation.navigate` data)."""
    verb = {"goto": "Navigated", "back": "Went back", "forward": "Went forward",
            "reload": "Reloaded"}.get(action, action)
    where = f"tab {r.get('tabId', tab_id if tab_id is not None else '?')}"
    line = f"{verb} {where} -> {r.get('url', url or '?')}"
    if r.get("title"):
        line += f' — "{r["title"]}"'
    if wait != "none":
        if r.get("settled"):
            line += wait_report.settled_phrase(r, wait)
        elif r.get("timedOut"):
            line += (f"\n  TIMED OUT after {fmt_ms(r.get('waitedMs'))} waiting to settle"
                     f" (status={r.get('status')}"
                     + (f", waited for selector {r.get('waitedForSelector')}" if r.get('waitedForSelector') else "")
                     + "). The page may still be loading; re-check with page_read or raise timeout_ms.")
        elif r.get("tabClosed"):
            line += "\n  NOTE: the tab closed during navigation."
        if r.get("timedOut"):
            degrade("unsettled", tool="navigate", waited_ms=r.get("waitedMs"),
                    status=r.get("status"))
        line += wait_report.timing_line(r, wait)
        line += wait_report.dom_note(r, wait)
        if wait_report.dom_unstable(r, wait):
            degrade("dom_unstable", tool="navigate", waited_ms=r.get("waitedMs"),
                    error=r.get("domError"))
    # Compare the URL that was REQUESTED to the one that actually
    # loaded. Everything needed was already in this response and nothing looked at
    # it, so a captcha redirect, a 404 with a normal-looking body, and a dropped
    # filter parameter all rendered as plain success; the last of those produced a
    # wrong price answer delivered to a user with full confidence. Suppressed on the
    # about:blank path, where the failure is already loud.
    if wait != "none" and not r.get("aboutBlank"):
        landing = landing_report(
            url if action == "goto" else None,
            r.get("url"),
            title=r.get("title"),
            http_status=r.get("httpStatus"),
            url_before=r.get("urlBefore"),
            redirects=r.get("redirects"),
            op=action,
        )
        line += _record_landing(r, landing)
        line += challenge_note(r.get("url"), r.get("title"))
    # Failure diagnostics: name the cause, not just the symptom.
    if r.get("superseded"):
        line += ("\n  NOTE: a request was aborted because a NEWER navigation superseded it on "
                 "this tab (self-inflicted, not a page/network error).")
    if r.get("aboutBlank"):
        line += f"\n  document is still about:blank — nothing rendered (attempted {r.get('pendingUrl')})."
    if r.get("netError"):
        line += f"\n  net error: {r['netError']}"
        line += wait_report.nav_events_line(r)
    if r.get("dialedAddress"):
        line += f"\n  dialed address: {r['dialedAddress']}"
    if r.get("redirects"):
        chain = " -> ".join(f"{h.get('status')} {h.get('to')}" for h in r["redirects"])
        line += f"\n  redirect chain: {chain}"
    line += digest_suffix(r)
    line += service_workers_suffix(r)
    return line


def make_navigate_tool(calls: BrowserCalls) -> Callable[..., Any]:
    """Build the `navigate` tool function over `calls`."""

    async def _navigate(url, tab_id, action, wait, wait_for, timeout_ms, bypass_cache,
                        read, read_scope, read_max_chars, proxy, read_offset=0,
                        read_frame_id=None) -> str:
        action, verr = enum_value(action, "action", ("goto", "back", "forward", "reload"))
        if verr:
            return invalid(verr)
        if action == "goto" and not url:
            # Refused here, before the wire and before proxy="tor" could start anything,
            # with a complete call to copy (response_hints.missing_url_text).
            return invalid(missing_url_text("navigate", action, tab_id))
        wait, verr = enum_value(wait, "wait", ("settled", "load", "dom", "none"))
        if verr:
            return invalid(verr)
        # One clamped number feeds both the wire (timeoutMs) and the server's own deadline
        # (leg_budgets), so the server can never give up before the extension does.
        timeout_ms, verr = positive_ms(timeout_ms, "timeout_ms")
        if verr:
            return invalid(verr)
        read_max_chars, verr = read_window(read_max_chars, read_offset)
        if verr:
            return invalid(verr)
        perr = proxy_error(proxy)
        if perr:
            return fail(f"Error: {perr}")
        cerr = calls.proxy_capability_error(proxy)
        if cerr:
            return fail(f"Error: {cerr}")
        # Validate the read pair BEFORE the wire, so an unusable combination
        # costs nothing and says exactly what is wrong.
        rerr = fused_read.read_request_error(read, wait)
        if rerr:
            return invalid(rerr)
        read_fmt = fused_read.normalize_read(read)
        via_tor = tor_proxy.is_tor(proxy)
        refused = await tor_proxy.refuse_before_start(calls, proxy)
        if refused:
            return refused
        proxy, terr = await tor_proxy.resolve(proxy)
        if terr:
            return fail(f"Error: {terr}")
        data: Dict[str, Any] = {"op": action, "wait": wait, "timeoutMs": timeout_ms,
                                "bypassCache": bypass_cache}
        if url:
            data["url"] = url
        if tab_id is not None:
            data["tabId"] = tab_id
        if wait_for:
            data["waitFor"] = wait_for
        if proxy is not None:
            data["proxy"] = proxy
        nav_budget, read_budget = fused_read.leg_budgets(timeout_ms)
        response = await calls.request("navigation.navigate", data, timeout=nav_budget)
        err = calls.error_of(response)
        if err:
            return fail(f"Error navigating: {err}")
        r = response["data"]
        line = render_navigation(r, action=action, url=url, tab_id=tab_id, wait=wait)
        # The browser WAS switched (Chromium), whatever the navigation then did: say so first.
        line += browser_proxy.change_notice(calls.target_browser_name(),
                                            r.get("browserProxyChange"), via_tor=via_tor)
        target = url or r.get("pendingUrl") or r.get("url")
        line += await tor_proxy.onion_hint(
            calls, url=target, error_text=r.get("netError"), proxy=proxy,
            tab_id=r.get("tabId", tab_id),
            retry_call=f'navigate(url="{target}", tab_id={r.get("tabId", tab_id)}, proxy="tor")')
        if read_fmt is not None:
            line += await read_after_navigation(
                calls, r, read_fmt, requested_url=url if action == "goto" else None,
                scope=read_scope, max_chars=read_max_chars, timeout=read_budget,
                offset=read_offset, frame_id=read_frame_id)
        if navigation_failure(r, line):
            # The report stays the whole text (diagnostics and READ SKIPPED line
            # included); `fail` only records that this call did not succeed.
            return fail(line)
        return line

    async def navigate(
        tab_id: Optional[int] = None,
        action: str = "goto",
        wait: str = "settled",
        wait_for: Optional[str] = None,
        timeout_ms: int = 20000,
        bypass_cache: bool = False,
        read: Optional[str] = None,
        read_scope: Optional[str] = None,
        read_max_chars: int = 20000,
        proxy: Optional[str] = None,
        url: Optional[str] = None
    ) -> str:
        """Load url in a tab, or go back, forward or reload, and wait until the page is ready. Reports the final URL and title, failed requests, console errors and Service Worker registrations

        Args:
            tab_id: default: active tab
            action: goto, back, forward or reload
            wait: settled: committed, loaded, no request in flight, 250 ms of network quiet. dom: load, then DOM unchanged for 100 ms, max 3 s; a page that fetches content after load may return as a skeleton. load: load event only. none: return now
            wait_for: CSS selector that must also be present before returning
            timeout_ms: settle budget in ms
            bypass_cache: reload only
            read: also return the page in this call: markdown (main content), text, html or records (one row per listing card). A failed, timed-out, superseded or off-site navigation returns the failure, not content
            read_scope: CSS selector scoping the read, as page_read scope
            read_max_chars: character cap for the read
            proxy: only when the user asks for a proxy; omit it otherwise, which keeps the tab's current routing. Sets this tab's SOCKS routing before navigating: tor (the built-in Tor client; first use downloads it, allow a minute) or a socks5, socks5h, socks4 or socks4a URL such as socks5://127.0.0.1:9050. NOT supported: HTTP, HTTPS, PAC or authenticated proxies. DNS resolves at the proxy, so .onion hosts load; fail-closed. "" clears it. Chromium has no per-tab routing: it switches the WHOLE browser until cleared
            url: required for goto, the default action: the address to load, with scheme
        """
        return await _navigate(url, tab_id, action, wait, wait_for, timeout_ms,
                               bypass_cache, read, read_scope, read_max_chars, proxy)

    # `page_read(url=)` continues a read part-way through a document. The public
    # signature is pinned by the tools/list golden, so the offset and frame window
    # travel through this side door rather than new navigate parameters.
    navigate.with_read_window = _navigate

    return navigate
