"""The page tools: page_read, page_snapshot, page_act, page_eval, page_screenshot.

Each `make_*_tool(calls)` returns the tool function. The tool registry (tool_registry.py)
reads the name, schema and description from the function, so signatures and docstrings are
the agent-facing contract (`tests/unit/golden/tools_list.json` pins them). The result
formatting that is pure lives in module functions here and in `act.py` and `formatting.py`.

`PAGE_EVAL_SCRIPTS_DOC` is appended to page_eval's description by `mcp_tools.py` only when
$BROWSERPLUG_EXT_SCRIPTS is set server-side.
"""

import base64
import json
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from mcp_types import CallToolResult, ImageContent, TextContent

from server.tools.eval_hints import (arrow_iife_hint, http_only_cookie_note,
                                     isolated_world_blindspot_note, script_error_text,
                                     top_level_return_hint)

from server.tools.response_hints import (FRAMES_LEAD_EMPTY_READ, FRAMES_LEAD_NO_MATCH, frame_id_hint,
                                         frames_hint, snapshot_root_hint)

from server.tools.tool_outcome import degrade, fail, invalid

from server.tools.act import ActStep, format_act_result, plan_act, resolve_navigated_steps
from server.tools.challenge import challenge_note
from server.tools.validation import act_steps_error, enum_value, read_window
from server.tools.browser_calls import BrowserCalls
from server.tools.formatting import (content_changed_note, empty_read_diagnosis, fmt_element,
                                     fmt_records, page_state_warning, screenshot_warning,
                                     thin_read_note, unpainted_capture_cause)
from server.tools.predefined_script import run_predefined_script


# Appended to page_eval's description ONLY when $BROWSERPLUG_EXT_SCRIPTS is set server-side;
# the mode simply errors without it, so nobody else should pay for these words.
PAGE_EVAL_SCRIPTS_DOC = (
    "\n\n"
    "script_name runs a script from $BROWSERPLUG_EXT_SCRIPTS and evaluates its stdout as "
    "the code. script_args is a JSON array of strings such as '[\"a\",\"b\"]', or \"\" for "
    "none, passed without a shell"
)


# The extension's plain "the target resolved to nothing" texts. A stale ref or a missing
# `within` scope has its own, more specific explanation and is deliberately not in here.
_NO_MATCH_ERRORS = frozenset({"No element matched", "not found", "no element matches"})


async def frames_block(calls: BrowserCalls, tab_id: Optional[int], lead: str) -> str:
    """The subframe list under `lead`, or "" (no subframes, or the probe failed).

    Only called on an already-failed branch, and its own failure never replaces the real
    result."""
    try:
        fdata: Dict[str, Any] = {}
        if tab_id is not None:
            fdata["tabId"] = tab_id
        fresp = await calls.request("content.frames", fdata, timeout=10.0)
        if calls.error_of(fresp):
            return ""
        return frames_hint(fresp["data"].get("frames", []), lead=lead)
    except Exception:
        return ""


def act_target_matched_nothing(r: Dict[str, Any]) -> bool:
    """Whether any step of a content.act result failed because its target resolved to nothing."""
    return any(isinstance(res, dict) and res.get("ok") is False
               and res.get("error") in _NO_MATCH_ERRORS
               for res in (r.get("results") or []))


def render_read_header(r: Dict[str, Any], offset: int,
                       tab_id: Optional[int] = None) -> Tuple[str, Optional[str]]:
    """The header lines of a page_read result and the page-state warning (or None)."""
    header = f"{r.get('title') or 'untitled'} — {r.get('url', '?')}"
    meta_bits = []
    for k in ("site", "author", "published"):
        if r.get(k):
            meta_bits.append(f"{k}: {r[k]}")
    if r.get("wordCount"):
        meta_bits.append(f"{r['wordCount']} words")
    total = r.get("totalChars", 0)
    got = len(r.get("content", "") or "")
    start = r.get("offset", 0)
    end = start + got
    range_bit = f"[{r.get('format')}] chars {start}-{end} of {total}"
    if end < total:
        range_bit += f"; continue with offset={end}"
        degrade("truncated", tool="page_read", returned=got, total=total,
                next_offset=end)
    meta_bits.append(range_bit)
    if r.get("fallback"):
        meta_bits.append(f"NOTE: {r['fallback']}")
        degrade("format_fallback", tool="page_read", reason=r["fallback"])
    # The extension clamps an offset past the end back to the final window
    # rather than answering with nothing. Say so, or a caller comparing the offset
    # it asked for against the one it got has to guess why they differ.
    if r.get("offsetClamped"):
        meta_bits.append(
            f"NOTE: offset {r.get('requestedOffset', offset)} is past the end of this "
            f"{total}-character document; returned the LAST window instead of nothing")
    changed = content_changed_note(r, "page_read")
    if changed:
        meta_bits.append(changed)
    warn = page_state_warning(r.get("pageState"), tab_id)
    out = header + "\n" + " | ".join(meta_bits)
    if warn:
        out += f"\nWARNING: {warn}"
    out += challenge_note(r.get("url"), r.get("title"))
    return out, warn


def make_page_read_tool(calls: BrowserCalls, navigate_fn: Callable[..., Any]
                        ) -> Callable[..., Any]:
    """Build `page_read`. `navigate_fn` is the navigate tool function, for `url=`."""

    async def page_read(
        tab_id: Optional[int] = None,
        url: Optional[str] = None,
        format: str = "markdown",
        scope: Optional[str] = None,
        max_chars: int = 20000,
        offset: int = 0,
        frame_id: Optional[int] = None
    ) -> str:
        """Read a page's content. markdown extracts the main content with Defuddle, stripping navigation, ads and page chrome and keeping title, site and author; it falls back to plain text if extraction fails

        Other formats:
            text: full visible text (document.body.innerText)
            html: page HTML source

        For a repeated listing (product grid, search results) prefer
        page_snapshot(mode="records"); it returns one structured row per card
        instead of prose.

        Args:
            tab_id: default: active tab
            url: navigate here first, then read, in one call, same as navigate with read=format; uses tab_id, else the active tab
            format: markdown, text or html
            scope: CSS selector. markdown: treat this subtree as the main content; text, html: read only this subtree
            max_chars: characters per call; continue with offset
            offset: continue reading from this character offset
            frame_id: iframe to read; page_snapshot and an empty read list the ids
        """
        fmt, verr = enum_value(format, "format", ("markdown", "text", "html"))
        if verr:
            return invalid(verr)
        format = fmt
        max_chars, verr = read_window(max_chars, offset)
        if verr:
            return invalid(verr)
        if url:
            # The same fusion from the other direction: agents passed url= to page_read
            # six times unprompted, before it existed. Route it through the same fused
            # path so both spellings behave identically; including refusing to return
            # content for a page the navigation did not actually reach.
            # offset and frame_id ride the side door: a continuation with url= must
            # not restart at 0 and hint to continue again.
            fused = getattr(navigate_fn, "with_read_window", None)
            if fused is not None:
                return await fused(url, tab_id, "goto", "settled", None, 20000, False,
                                   format, scope, max_chars, None,
                                   read_offset=offset, read_frame_id=frame_id)
            return await navigate_fn(url=url, tab_id=tab_id, read=format,
                                     read_scope=scope, read_max_chars=max_chars)
        data: Dict[str, Any] = {"format": format, "maxChars": max_chars, "offset": offset}
        if tab_id is not None:
            data["tabId"] = tab_id
        if scope:
            data["scope"] = scope
        if frame_id is not None:
            data["frameId"] = frame_id
        response = await calls.request("content.read", data, timeout=60.0)
        err = calls.error_of(response)
        if err:
            return fail(f"Error reading page: {err}{frame_id_hint(err, tab_id, frame_id)}")
        r = response["data"]
        if not r.get("ok"):
            err = str(r.get("error", "unknown error"))
            return fail(f"Error reading page: {err}{frame_id_hint(err, tab_id, frame_id)}")
        out, warn = render_read_header(r, offset, tab_id)
        total = r.get("totalChars", 0)
        content = r.get("content", "") or ""
        if not content.strip():
            degrade("empty_extraction", tool="page_read", format=r.get("format"),
                    total_chars=total)
            # Advice like 'try format="text" or a scope selector' would recommend the
            # format that was JUST passed, and never name the actual cause. Diagnose
            # it instead, in the order the causes actually occur.
            out += "\n(no content extracted"
            if warn:
                pass                       # the WARNING above already named the cause
            elif total and offset >= total:
                out += (f"; offset {offset} is past the end: this document is only "
                        f"{total} characters. Re-read with offset=0")
            else:
                out += empty_read_diagnosis(r, format)
            out += ")"
            # Content the reader cannot see may simply be in a frame, and TODAY
            # only page_snapshot ever mentions frames; so an agent whose loop is
            # navigate -> read never learns the parameter exists. Probe only on this
            # already-failed branch, so a successful read pays nothing, and never let
            # the probe's own failure replace the real result.
            fhint = await frames_block(calls, tab_id, FRAMES_LEAD_EMPTY_READ)
            if fhint:
                out += "\n" + fhint
            return out
        thin = "" if warn else thin_read_note(r, content)
        if thin:
            degrade("thin_extraction", tool="page_read", returned=len(content),
                    body_chars=(r.get("docMetrics") or {}).get("bodyChars"))
        return out + thin + "\n\n" + content

    return page_read


def format_snapshot(r: Dict[str, Any], max_elements: int,
                    tab_id: Optional[int] = None) -> List[str]:
    """The lines of a page_snapshot result (interactive, forms or records mode)."""
    ps = r.get("pageState") or {}
    lines = [f"Page: {ps.get('title', '?')} — {ps.get('url', '?')}"]
    warn = page_state_warning(ps, tab_id)
    if warn:
        lines.append(f"WARNING: {warn}")
    note = challenge_note(ps.get("url"), ps.get("title"), indent="")
    if note:
        lines.append(note)

    if r.get("mode") == "records":
        lines.extend(fmt_records(r))
    elif r.get("mode") == "forms":
        for g in r.get("radioGroups", []):
            sel = ", ".join(g.get("selected") or []) or "none"
            lines.append(f"Radio group \"{g['name']}\" (selected: {sel}):")
            for o in g.get("options", []):
                mark = "(x)" if o.get("checked") else "( )"
                dis = "" if o.get("enabled", True) else " [disabled]"
                lines.append(f"  {mark} {o['ref']} value={o.get('value')} \"{o.get('label', '')}\"{dis}")
        for g in r.get("checkboxGroups", []):
            lines.append(f"Checkboxes \"{g['name']}\":")
            for o in g.get("options", []):
                mark = "[x]" if o.get("checked") else "[ ]"
                dis = "" if o.get("enabled", True) else " [disabled]"
                lines.append(f"  {mark} {o['ref']} value={o.get('value')} \"{o.get('label', '')}\"{dis}")
        if r.get("inputs"):
            lines.append("Inputs:")
            for i in r["inputs"]:
                req = " [required]" if i.get("required") else ""
                dis = "" if i.get("enabled", True) else " [disabled]"
                lines.append(f"  {i['ref']} {i.get('type', 'text')} \"{i.get('label', '')}\" value=\"{i.get('value', '')}\"{req}{dis}")
        if r.get("selects"):
            lines.append("Selects:")
            for s in r["selects"]:
                dis = "" if s.get("enabled", True) else " [disabled]"
                opts = ", ".join(s.get("options", [])[:15])
                lines.append(f"  {s['ref']} \"{s.get('label', '')}\" selected={s.get('selected')}{dis} options: {opts}")
        if r.get("buttons"):
            lines.append("Buttons:")
            for b in r["buttons"]:
                dis = " [disabled]" if not b.get("enabled", True) else ""
                lines.append(f"  {b['ref']} \"{b.get('label', '')}\"{dis}")
    else:
        count = r.get("count", 0)
        bits = [f"{count} elements"]
        if r.get("dedupedDuplicates"):
            bits.append(f"{r['dedupedDuplicates']} duplicates collapsed")
        if r.get("truncated"):
            bits.append(f"TRUNCATED at {max_elements}")
        lines.append(f"Interactive elements ({', '.join(bits)}):")
        if r.get("truncated"):
            # Generic advice ("scope with root= or raise max_elements") is correct, yet
            # it arrived 19 times and was followed 11 times in 165 calls, because it
            # names a parameter without naming a VALUE, and it arrives
            # nested inside the header parenthesis after the payload was already
            # paid for. Derive a concrete root= from the elements actually returned.
            lines.append(snapshot_root_hint(r.get("elements", []), count,
                                            max_elements))
            degrade("truncated", tool="page_snapshot", returned=count,
                    limit=max_elements)
        if count == 0:
            degrade("empty_extraction", tool="page_snapshot", count=0)
            hint = ("none found; page may still be loading, content may be in an iframe "
                    "(see frames below), or try include_hidden=true / a different root")
            if warn:
                hint = "none found; see WARNING above"
            lines.append(f"  ({hint})")
        for el in r.get("elements", []):
            lines.append(fmt_element(el))
    return lines


def make_page_snapshot_tool(calls: BrowserCalls) -> Callable[..., Any]:
    """Build `page_snapshot`."""

    async def page_snapshot(
        tab_id: Optional[int] = None,
        mode: str = "interactive",
        root: Optional[str] = None,
        max_elements: int = 150,
        include_hidden: bool = False,
        frame_id: Optional[int] = None,
        item_selector: Optional[str] = None
    ) -> str:
        """Map a page's interactive elements, one line each: ref role "name" value/state selector. Pass a ref such as e5 or the selector as a page_act target. Runs of same-name controls collapse to one line with a count; iframes are listed at the end with their frame_ids. forms mode: radio and checkbox groups with their options and current selection, inputs with values, buttons with enabled state. records mode: one row per repeated card (product grid, search results) with title, link, prices and image, each read from inside that card. A card with more than one live price is AMBIGUOUS and lists the candidates, each with the words beside it; struck-through prices are reported separately; tracking redirects are unwrapped. With no confident pattern it refuses; then pass item_selector

        Args:
            tab_id: default: active tab
            mode: interactive, forms or records
            root: CSS selector of the subtree to map
            max_elements: element or record cap
            include_hidden: include hidden and off-screen elements
            frame_id: snapshot inside this iframe
            item_selector: records only: CSS selector matching one card
        """
        args = {"root": root, "max": max_elements, "includeHidden": include_hidden,
                "dedupe": True, "mode": mode}
        data = dict(args)
        if item_selector:
            data["item_selector"] = item_selector
        if tab_id is not None:
            data["tabId"] = tab_id
        if frame_id is not None:
            data["frameId"] = frame_id
        response = await calls.request("content.snapshot", data)
        err = calls.error_of(response)
        if err:
            return fail(f"Error taking snapshot: {err}{frame_id_hint(err, tab_id, frame_id)}")
        r = response["data"]
        if not r.get("ok"):
            err = str(r.get("error", "unknown error"))
            return fail(f"Error taking snapshot: {err}{frame_id_hint(err, tab_id, frame_id)}")
        lines = format_snapshot(r, max_elements, tab_id)

        # frames (absorbs the old page_frames tool); only when interesting
        try:
            fdata: Dict[str, Any] = {}
            if tab_id is not None:
                fdata["tabId"] = tab_id
            fresp = await calls.request("content.frames", fdata, timeout=10.0)
            if not calls.error_of(fresp):
                frames = fresp["data"].get("frames", [])
                subframes = [f for f in frames if f.get("frameId", 0) != 0]
                if subframes:
                    lines.append("Frames (pass frame_id to page_* tools to reach inside):")
                    for f in subframes:
                        lines.append(f"  frame_id={f['frameId']} {f.get('url', '?')}")
                    ps = r.get("pageState") or {}
                    wall = challenge_note(ps.get("url"), ps.get("title"), [f.get("url") for f in subframes], indent="")
                    if wall and wall not in lines:
                        lines.append(wall)
        except Exception:
            pass
        return "\n".join(lines)

    return page_snapshot


def make_page_act_tool(calls: BrowserCalls) -> Callable[..., Any]:
    """Build `page_act`."""

    async def page_act(
        steps: List[ActStep],
        tab_id: Optional[int] = None,
        frame_id: Optional[int] = None
    ) -> str:
        """Run interaction steps on a page in one call. Steps run in order and stop at the first failure unless a step sets continue_on_error. A login is one call: fill #email, click Log in, then wait for url_contains /dashboard. Each step reports what it changed (DOM, URL, dialog), the network requests it triggered and console errors. fill drives React and Vue controlled inputs and select elements

        Args:
            tab_id: default: active tab
            frame_id: act inside this iframe
        """
        verr = act_steps_error(steps)
        if verr:
            return invalid(verr)
        step_dicts, act_timeout, data = plan_act(steps, tab_id, frame_id)
        response = await calls.request("content.act", data, timeout=act_timeout)
        err = calls.error_of(response)
        if err:
            if "timed out" in err.lower():
                err += await calls.tab_state_after_timeout(tab_id)
            return fail(f"Error executing steps: {err}{frame_id_hint(err, tab_id, frame_id)}")
        r = resolve_navigated_steps(response["data"])
        out, completed, total_steps = format_act_result(r, step_dicts)
        # A target that matched nothing in the top document may live in an iframe, and
        # the act loop never calls page_snapshot, which is the only other place frames
        # are listed. Inside a frame already (frame_id given) the list would mislead.
        if frame_id is None and act_target_matched_nothing(r):
            fhint = await frames_block(calls, tab_id, FRAMES_LEAD_NO_MATCH)
            if fhint:
                out += "\n" + fhint
        # Nothing worked at all is a FAILURE; some-but-not-all is a degraded
        # success, which must stay a success so the per-step detail still reaches the
        # agent as a normal result.
        if not r.get("ok") and completed == 0:
            return fail(out, code="PAGE_ERROR", tool="page_act")
        if not r.get("ok") or completed < total_steps:
            degrade("partial_steps", tool="page_act", completed=completed,
                    total=total_steps,
                    not_run=[s.get("step") for s in (r.get("notRunSteps") or [])]
                    + list(r.get("stoppedAfterNavigation") or []))
        for res in r.get("results") or []:
            if isinstance(res, dict) and res.get("navigatedDuringStep"):
                degrade("step_report_lost", tool="page_act", step=res.get("step"))
        return out

    return page_act


def eval_result_note(rdata: Dict[str, Any], code: str, world: str, result: Any) -> str:
    """The NOTE lines appended to a page_eval result (replaced document, blind spots)."""
    # An eval fired mid-navigation runs against an early or TRANSIENT
    # document (Firefox swaps in an empty placeholder while a reload is
    # in flight; right URL, no content). The result is truthful but not
    # final; say so, or the agent mistakes half-state for final state.
    note = ""
    # Sharper than the tab-status probe below and checked first: documentId proves
    # the document was REPLACED between the script starting and finishing, which a
    # navigation that begins and completes inside the eval's own window leaves
    # tab.status "complete" on both sides of. Here the value is not early, it is
    # about a page that no longer exists, so re-running is not optional.
    if rdata.get("documentChanged"):
        note = ("\nNOTE: the document was REPLACED while this script ran (the page "
                "navigated or reloaded mid-eval), so the value describes a document "
                "that no longer exists. Re-run it against the current page.")
    elif rdata.get("readyState") == "loading" or rdata.get("tabLoading"):
        note = ("\nNOTE: the tab was still loading; this ran against a "
                "transient/early document (a reload/navigation was in flight); "
                "re-run after the page settles for the final state.")
    # An empty isolated-world read of a page-JS expando/framework
    # internal is INVISIBLE-from-this-world, not absent; flag it distinctly.
    blindspot = isolated_world_blindspot_note(code, world, result)
    if blindspot:
        note += "\n" + blindspot
    # A document.cookie read silently omits HttpOnly cookies; i.e. the
    # session/auth ones. Unqualified, that reads as the complete set; it destroyed a
    # working credential once. Fires on the READ regardless of what came back,
    # because the omission is structural, not conditional.
    cookies = http_only_cookie_note(code)
    if cookies:
        note += "\n" + cookies
    return note


def make_page_eval_tool(calls: BrowserCalls) -> Callable[..., Any]:
    """Build `page_eval`. `mcp_tools.py` registers it with the script arguments gated."""

    async def page_eval(
        code: Optional[str] = None,
        tab_id: Optional[int] = None,
        world: str = "isolated",
        frame_id: Optional[int] = None,
        script_name: Optional[str] = None,
        script_args: str = ""
    ) -> str:
        """Run JavaScript in a tab and return the value of the last expression; a returned Promise is awaited. isolated world: full DOM access, immune to page CSP, but page JS globals and the expandos page scripts set on DOM nodes (React fiber keys, __vue internals) are invisible; on Firefox read globals via window.wrappedJSObject. main world: the page's own world, which sees everything; a strict page CSP can block it, and the result says so

        Args:
            code: JavaScript to run
            tab_id: default: active tab
            world: isolated or main
            frame_id: run inside this iframe
        """
        world, verr = enum_value(world, "world", ("isolated", "main"))
        if verr:
            return invalid(verr)
        if script_name:
            generated = await run_predefined_script(script_name, script_args)
            if not generated.ok:
                return generated.text
            code = generated.text
        if not code:
            return invalid("Error: provide code (or script_name)")
        data: Dict[str, Any] = {"script": code, "world": world}
        if tab_id is not None:
            data["tabId"] = tab_id
        if frame_id is not None:
            data["frameId"] = frame_id
        response = await calls.request("content.execute_script", data, timeout=45.0)
        err = calls.error_of(response)
        if err:
            err = script_error_text(err)
            return fail(f"Error executing script: {err}{top_level_return_hint(err)}"
                        f"{arrow_iife_hint(err, code)}{frame_id_hint(err, tab_id, frame_id)}")
        rdata = response["data"]
        result = rdata.get("result")
        url = rdata.get("url", "unknown URL")
        note = eval_result_note(rdata, code, world, result)
        if result is None:
            return f"Script executed ({url}); no return value{note}"
        rendered = result if isinstance(result, str) else json.dumps(result, indent=2, default=str)
        return f"Script result ({url}):\n{rendered}{note}"

    return page_eval


def screenshot_summary(rdata: Dict[str, Any], *, method: str, captured_format: str,
                       image_bytes: bytes, tab_id: Optional[int],
                       selector: Optional[str]) -> List[str]:
    """The pieces of the text line that accompanies a screenshot."""
    summary_bits = [
        f"Screenshot ({method}, {captured_format}, {len(image_bytes)} bytes)",
    ]
    if tab_id is not None:
        summary_bits.append(f"tab {tab_id}")
    if rdata.get("pages") and rdata["pages"] > 1:
        summary_bits.append(f"{rdata['pages']} stitched pages")
    if rdata.get("truncated"):
        summary_bits.append("truncated (page exceeded capture height limit)")
    if rdata.get("selector"):
        eb = rdata.get("elementBox") or {}
        box = f" {eb.get('w')}x{eb.get('h')}px" if eb else ""
        summary_bits.append(f"element '{rdata['selector']}'{box}")
    if rdata.get("clippedWidth"):
        summary_bits.append("element wider than viewport; captured the left portion")
    if rdata.get("pageWarning"):
        summary_bits.append(f"WARNING: {screenshot_warning(rdata['pageWarning'], tab_id)}")
    if rdata.get("settleWarning"):
        summary_bits.append(f"NOTE: {rdata['settleWarning']}")
    # A silent captureTab -> captureVisibleTab downgrade is how a blank
    # background-tab capture once passed as a clean success; name the
    # fallback and its cause.
    if rdata.get("captureTabError"):
        summary_bits.append(
            f"NOTE: direct captureTab failed ({rdata['captureTabError']}); "
            "fell back to captureVisibleTab (briefly activates the tab; "
            "needs a painted window)")

    # page_screenshot is 1.7% of calls and 73.4% of ALL payload returned into
    # context; 12.0 MB of base64 against 4.4 MB for every other tool combined, mean
    # 240 KB a shot, nearly all of it full-viewport captures answering a yes/no
    # question about page state. `quality` is NOT the lever (PNG and 71% JPEG measured
    # identical in tokens), so say what the shot actually COST and name the two things
    # that do help. Only when it is big enough to be worth an agent's attention.
    if not selector and len(image_bytes) > 120_000:
        summary_bits.append(
            f"NOTE: this image is {len(image_bytes) // 1024} KB of context. For a "
            f"question about ONE element pass selector=<css|ref> to capture just it; "
            f"for a question about page STATE, page_snapshot or page_read answers in "
            f"a fraction of the tokens (quality/format do not help; they change the "
            f"bytes but not the token cost)")
    return summary_bits


# Capture failures that already name their cause: a selector that matched zero or several
# elements, a privileged page, a window the compositor is not painting.
_EXPLAINED_CAPTURE_ERRORS = ("element screenshot:", "privileged page", "not being painted",
                             "screenshot stalled")


async def capture_failure_cause(calls: BrowserCalls, tab_id: Optional[int], err: str) -> str:
    """Why a capture failed, when the page can say: the unlaid-out case, found by asking the
    tab for its page state. "" otherwise, and when the probe itself fails.

    Firefox answers a capture of a page with no layout with a bare "An unexpected error
    occurred" (DeepSeek, three times in a row, eval of 2026-10-08), while the description
    promises background captures. Probed only on this failed branch."""
    if any(marker in err for marker in _EXPLAINED_CAPTURE_ERRORS):
        return ""
    try:
        pdata: Dict[str, Any] = {}
        if tab_id is not None:
            pdata["tabId"] = tab_id
        presp = await calls.request("content.page_state", pdata, timeout=10.0, _note_wire=False)
        if calls.error_of(presp):
            return ""
        ps = (presp.get("data") or {}).get("pageState") or {}
    except Exception:
        return ""
    if ps.get("painted") is False and not ps.get("hiddenByCss"):
        lead = "" if err.rstrip().endswith(".") else "."
        return lead + unpainted_capture_cause(tab_id)
    return ""


def make_page_screenshot_tool(calls: BrowserCalls) -> Callable[..., Any]:
    """Build `page_screenshot`."""

    async def page_screenshot(
        tab_id: Optional[int] = None,
        full_page: bool = False,
        selector: Optional[str] = None,
        format: str = "png",
        quality: int = 90,
        settle_ms: Optional[int] = None,
    ) -> Union[CallToolResult, str]:
        """Capture a tab as an inline image with a short text summary. Firefox captures background tabs without taking focus, except a page left with no layout (0x0) by a minimized or background window; Chromium briefly activates the tab. Each shot first waits briefly for web fonts, image decode and a quiet animation frame

        Args:
            tab_id: default: active tab
            full_page: capture the entire scrollable page
            selector: capture one element by snapshot ref, CSS selector or visible text, cropped to its box; overrides full_page. Zero or several matches is an error
            format: png or jpeg
            quality: JPEG quality, 1 to 100
            settle_ms: cap in ms on the pre-shot wait, default about 1200; 0 skips it
        """
        data: Dict[str, Any] = {"format": format, "quality": quality, "fullPage": full_page}
        if tab_id is not None:
            data["tabId"] = tab_id
        if selector:
            data["selector"] = selector
        if settle_ms is not None:
            data["settleMs"] = settle_ms
        response = await calls.request("tabs.screenshot", data, timeout=60.0)
        err = calls.error_of(response)
        if err:
            cause = await capture_failure_cause(calls, tab_id, err)
            return fail(f"Error capturing screenshot: {err}{cause}")

        rdata = response["data"]
        data_url = rdata.get("dataUrl", "")
        captured_format = rdata.get("format", format)
        method = rdata.get("method", "captureVisibleTab")
        if not data_url or "base64," not in data_url:
            return fail(f"No screenshot data received (method={method})")

        header, b64 = data_url.split("base64,", 1)
        mime = header[5:].rstrip(";") or f"image/{captured_format}"

        try:
            image_bytes = base64.b64decode(b64)
        except Exception as e:
            return fail(f"Screenshot decode failed: {e}")

        summary_bits = screenshot_summary(
            rdata, method=method, captured_format=captured_format,
            image_bytes=image_bytes, tab_id=tab_id, selector=selector)
        return CallToolResult(content=[
            TextContent(type="text", text="; ".join(summary_bits)),
            ImageContent(type="image", data=b64, mime_type=mime),
        ])

    return page_screenshot
