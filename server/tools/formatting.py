"""Text formatting shared by the MCP tools.

Everything here is a pure function of an extension response (or of a few numbers): no
server, no I/O, no registration. The per-tool modules own the parts that only one tool
uses; this module holds what two or more of them render the same way (records, elapsed
times, the network and console digests, the thin-read notes) and the helpers a test can
call without a browser.

Agent-facing wording is a contract (`.tmp/dev-docs/tool-surface.md`); `tests/integration/` pins a
lot of it. Reword only after grepping there.
"""

import json
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from server.tools.tool_outcome import degrade


# Thin-read thresholds. A read at or above the absolute floor is never questioned;
# 1,200 characters is a usable answer whatever the page around it looks like. Below it, an
# extraction holding under this fraction of the body's own text has dropped the content
# rather than trimmed it.
_THIN_READ_ABSOLUTE = 1200
_THIN_READ_RATIO = 0.15


def thin_read_note(r: Dict[str, Any], content: str) -> str:
    """A read that SUCCEEDED but returned suspiciously little. "" when it is fine.

    The corpus cases were not empty, which is why an empty-only check misses them: morele
    returned 290 characters containing a live-chat modal and zero products; sipeed 140
    characters of chrome; two reddit URLs the literal string "5.24.10". Each looked like a
    clean success. Two independent signals, both needing the extension's docMetrics:

      * extracted << the body's own text  -> the EXTRACTION dropped the content
      * many nodes, almost no text        -> the page has not rendered it yet, or a wall

    Deliberately conservative. A markdown extraction is SUPPOSED to be much smaller than
    the raw page, so only a severe gap counts, and only while the result is small enough in
    absolute terms to be useless. A note that fires on healthy reads would be ignored on the
    unhealthy ones too.
    """
    m = r.get("docMetrics") or {}
    nodes, body_chars = m.get("nodes"), m.get("bodyChars")
    if not isinstance(nodes, int) or not isinstance(body_chars, int):
        return ""
    # The whole EXTRACTION, not this window of it. Comparing the returned slice would
    # accuse every paginated read of being thin; a 400-character first window of a 40,000
    # character article is the pagination working, not the extraction failing.
    got = r.get("totalChars")
    if not isinstance(got, int):
        got = len(content)
    if got >= _THIN_READ_ABSOLUTE:
        return ""
    if body_chars >= 1000 and got < body_chars * _THIN_READ_RATIO:
        return (f"\nNOTE: this is {got} characters out of {body_chars} the page's own body "
                f"holds; the extraction dropped most of the content. Re-read with "
                f'format="text", or scope=<css> on the container you want.')
    if nodes >= 250 and body_chars < 200:
        note = (f"\nNOTE: {got} characters against {nodes} DOM nodes; the page has "
                f"structure but almost no text yet, so it is still rendering or behind a "
                f"consent/bot wall.")
        if m.get("dialogs"):
            note += f" {m['dialogs']} modal dialog(s) are open."
        return note + " Re-read after navigate(wait_for=<selector>)."
    return ""


def empty_read_diagnosis(r: Dict[str, Any], requested_format: str) -> str:
    """Why did this read come back with nothing?

    Advice like 'try format="text" or a scope selector' fires even when `text` was
    exactly what had been passed, and never names a cause. Meanwhile the corpus is full of
    reads that returned 290 characters of chat widget on a page with 400 product nodes, or
    the literal string "5.24.10", and NOTHING distinguished those from a genuinely empty
    page. The extension now measures the document, so this can answer accurately.

    Returns a clause to append (it always starts mid-sentence), or a bare full stop's worth
    of nothing when there is no evidence to reason from; an invented diagnosis would be
    worse than none.
    """
    m = r.get("docMetrics") or {}
    nodes, body_chars = m.get("nodes"), m.get("bodyChars")
    if not isinstance(nodes, int) or not isinstance(body_chars, int):
        # No measurements (an older extension, or a mocked response). Say only what is
        # certainly true rather than guessing at a cause.
        if requested_format != "text":
            return '; try format="text", which does not depend on article detection'
        return "; the page reported no extractable text"

    if body_chars > 200:
        # The document HAS text; the extraction is what failed. This is the morele.net /
        # sipeed.com shape: a full page reduced to a chat widget or a nav bar.
        clause = (f"; but the page's own body holds {body_chars} characters across "
                  f"{nodes} DOM nodes, so this is an EXTRACTION failure, not an empty page")
        if requested_format != "text":
            clause += '. Re-read with format="text" to get what is actually there'
        else:
            clause += ". Narrow with scope=<css> to the container you want"
        return clause
    if nodes > 150:
        # Plenty of structure, no text yet: rendered late, or behind a wall.
        clause = (f"; the document has {nodes} DOM nodes but almost no text, so it is "
                  f"either still rendering or behind a consent/bot wall")
        if m.get("dialogs"):
            clause += f" ({m['dialogs']} modal dialog(s) are open; that is the wall)"
        clause += (". Re-read after navigate(wait_for=<selector>), or page_screenshot to "
                   "see what is on screen")
        if m.get("sample"):
            clause += f'. Body begins: "{m["sample"][:120]}"'
        return clause
    return f"; the page really is almost empty ({nodes} DOM nodes, {body_chars} characters)"


def unpainted_text(tab_id: Optional[int] = None) -> str:
    """What an unlaid-out page means and what to do about it.

    Reads, snapshots, page_act and page_eval all work from the DOM of a background tab,
    so the text says that first and keeps activation as a last resort, naming its cost.
    A remedy that leads with activation gets followed: in the eval of 2026-10-08 Qwen,
    DeepSeek and Sonnet each brought their tab to the front of the user's window for a
    snapshot that was already complete.
    """
    tab = tab_id if tab_id is not None else "<this tab's id>"
    return ("Page is not laid out (document measures 0x0): its window is in the background, "
            "where Firefox defers layout. Results read from the DOM still hold: text, snapshot "
            "elements, page_act steps and page_eval; element sizes, visibility checks and "
            "screenshots may not. Stay in this background tab. Last resort, only if a step "
            f'fails because an element has no size: tabs(action="activate", tab_id={tab}) '
            "lays the page out, but it brings the tab to the front of the user's window.")


def page_state_warning(ps: Optional[Dict[str, Any]],
                       tab_id: Optional[int] = None) -> Optional[str]:
    """Human-readable warning when a page is unpainted or CSS-hidden.

    Worded here from the flags, not from the extension's own `warning`: older builds
    send one that tells the agent to activate the tab."""
    if not ps:
        return None
    if ps.get("painted") is False:
        if ps.get("hiddenByCss"):
            return ("Page content exists but is hidden by CSS (display:none on body/html; "
                    "e.g. an anti-FOUC guard). The DOM is intact; this is NOT an empty page.")
        return unpainted_text(tab_id)
    if ps.get("warning"):
        return ps["warning"]
    return None


# The screenshot warnings the extensions attach (background.js, tabs.screenshot), matched
# by their openings. Builds before 2026-10-08 ended both in an activation remedy that takes
# the user's focus; current ones word it as below, and the server adds the tab id.
_UNPAINTED_SHOT = ("Page is not painted", "Page is not laid out")
_UNIFORM_SHOT_TAIL = re.compile(
    r"\s*If the tab is in an unfocused or minimized window, tabs action=\"activate\" it "
    r"and retry\.\s*$")


def unpainted_capture_cause(tab_id: Optional[int] = None) -> str:
    """Appended to a failed capture of a page that measures 0x0: the cause and what works."""
    tab = tab_id if tab_id is not None else "<this tab's id>"
    return (" Cause: the page is not laid out (it measures 0x0) because its window is in the "
            "background, so the browser has nothing to draw. page_snapshot and page_read read "
            f'this page without focus. Last resort for a picture: tabs(action="activate", '
            f"tab_id={tab}), then capture again; it brings the tab to the front of the user's "
            "window.")


def screenshot_warning(text: str, tab_id: Optional[int] = None) -> str:
    """A screenshot's pageWarning, with the focus-taking remedy demoted to a last resort."""
    if not text:
        return text
    tab = tab_id if tab_id is not None else "<this tab's id>"
    last_resort = (f'Last resort for a picture: tabs(action="activate", tab_id={tab}), then '
                   "capture again; it brings the tab to the front of the user's window.")
    if text.startswith(_UNPAINTED_SHOT):
        return ("Page is not laid out (its window is in the background), so this image is "
                "likely blank. page_snapshot and page_read read the page without focus. "
                + last_resort)
    if _UNIFORM_SHOT_TAIL.search(text):
        return (_UNIFORM_SHOT_TAIL.sub("", text) + " If its window is in the background, "
                "page_snapshot and page_read read the page without focus. " + last_resort)
    return text


def fmt_element(el: Dict[str, Any]) -> str:
    """One compact line per snapshot element."""
    ref = el.get("ref", "?")
    role = el.get("role", "generic")
    name = (el.get("name") or "").replace("\n", " ")
    line = f"- {ref} {role} \"{name}\""
    val = el.get("value")
    if val is not None:
        if isinstance(val, bool):
            line += " checked" if val else " unchecked"
        elif val != "":
            sval = str(val).replace("\n", " ")
            if len(sval) > 80:
                sval = sval[:80] + "…"
            line += f' value="{sval}"'
    if el.get("enabled") is False:
        line += " [disabled]"
    if el.get("visible") is False:
        line += " [hidden]"
    if el.get("expanded") is not None:
        line += f" [expanded={el['expanded']}]"
    if el.get("dupCount"):
        line += f" (x{el['dupCount']} identical)"
    sel = el.get("selector")
    if sel:
        line += f" sel={sel}"
    return line


def price_str(p: Optional[Dict[str, Any]]) -> str:
    """A price as the page wrote it, with the parsed number when it is unambiguous.

    The verbatim text is the primary form on purpose: it is what a human sees in
    the screenshot, and thousands/decimal separators differ by locale.
    """
    if not p:
        return ""
    text = str(p.get("text") or "").strip()
    val = p.get("value")
    if val is None:
        return text
    return f"{text} (={val})"


def fmt_records(r: Dict[str, Any]) -> List[str]:
    """Render mode="records"; one line per repeated card.

    Ambiguity is spelled out per row rather than resolved: a card carrying two
    live prices has no single correct answer, and quietly printing the first is
    exactly the failure this mode exists to remove.
    """
    lines: List[str] = []
    det = r.get("detection") or {}
    count = r.get("count", 0)
    total = r.get("totalItems", count)
    bits = [f"{count} of {total} records — TRUNCATED, raise max_elements"
            if total > count else f"{count} records"]
    if total > count:
        degrade("truncated", tool="records", returned=count, total=total)
    if r.get("withPrice") is not None:
        bits.append(f"{r['withPrice']} with a single clear price")
    if r.get("ambiguousCount"):
        bits.append(f"{r['ambiguousCount']} AMBIGUOUS")
    how = det.get("method", "?")
    sel = det.get("itemSelector")
    detail = f"detected as {sel}" if sel else "detected"
    if how == "item_selector":
        detail = f"item_selector {sel}"
    elif det.get("containerSelector"):
        detail += f" under {det['containerSelector']}"
    lines.append(f"Records ({', '.join(bits)}) — {detail}")
    if det.get("lowConfidence"):
        lines.append(f"  LOW CONFIDENCE: {det['lowConfidence']}")
    if how == "auto" and det.get("runnerUp"):
        ru = det["runnerUp"]
        lines.append(f"  (runner-up pattern: {ru.get('count')}x {ru.get('selector')}; "
                     f'pass item_selector="..." if that is the one you meant)')
    for rec in r.get("records", []):
        i = rec.get("index", "?")
        title = (rec.get("title") or "").replace("\n", " ").strip()
        if not title:
            title = (rec.get("text") or "")[:80].strip() or "(no title)"
        parts = [f'{i} "{title}"']
        if rec.get("ambiguous"):
            # Each candidate with the words beside it on the page, so the agent can pick
            # the one it needs (an item price against a delivery-inclusive total).
            cands = ", ".join(price_str(p) + (f' "{p["label"]}"' if p.get("label") else "")
                              for p in (rec.get("prices") or []) if not p.get("struck"))
            parts.append(f"AMBIGUOUS price ({cands})")
        elif rec.get("price"):
            parts.append(price_str(rec["price"]))
        else:
            parts.append("no price found")
        if rec.get("priceWas"):
            parts.append(f"was {price_str(rec['priceWas'])}")
        if rec.get("link"):
            parts.append(rec["link"])
        lines.append("  " + " | ".join(parts))
        if rec.get("linkUnwrappedFrom"):
            lines.append("    (link unwrapped from a tracking redirector)")
    if r.get("ambiguousCount"):
        lines.append(
            "NOTE: an AMBIGUOUS row holds more than one live price inside the SAME card, so no "
            "price was picked for it. Each candidate is followed by the words printed beside it "
            "on the page, in quotes (a delivery-inclusive total, a variant, a unit price): choose "
            "the one the task asks for, and open the row's own link only when those words do "
            "not decide it. Scraping the same cards again with page_eval finds the same prices.")
    return lines


def compact(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), default=str, ensure_ascii=False)


def content_changed_note(r: Dict[str, Any], tool: str) -> str:
    """The note for a read the extension extracted again because the page changed since its
    previous read under the same format, scope and URL, and came out a different length.
    "" otherwise. The content is the live page (the extension never pages an old copy), so
    what an agent must hear is that offsets from its earlier reads now point into other
    text. Records the `content_changed` degradation when it fires."""
    changed = r.get("contentChanged")
    prev = changed.get("previousTotal") if isinstance(changed, dict) else None
    total = r.get("totalChars")
    if not isinstance(prev, int) or isinstance(prev, bool) or prev == total:
        return ""
    degrade("content_changed", tool=tool, previous_total=prev, total=total)
    return (f"NOTE: the page changed since the previous read of it ({prev} -> {total} chars); "
            "this is the current page, and offsets from earlier reads may not line up")


def fmt_ms(ms: Optional[float]) -> str:
    if ms is None:
        return "?"
    return f"{ms/1000:.1f}s" if ms >= 1000 else f"{int(ms)}ms"


def fmt_last_access(ms: Optional[int], now_ms: float) -> str:
    """Render a tab's `lastAccessed` (epoch ms) as local time + a relative age."""
    if not ms:
        return "unknown"
    stamp = datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M:%S")
    delta = max(0.0, (now_ms - ms) / 1000)
    if delta < 60:
        rel = f"{int(delta)}s ago"
    elif delta < 3600:
        rel = f"{int(delta // 60)}m ago"
    elif delta < 86400:
        rel = f"{int(delta // 3600)}h ago"
    else:
        rel = f"{int(delta // 86400)}d ago"
    return f"{stamp} ({rel})"


CONSOLE_UNFLUSHED_NOTE = ("console errors may be incomplete: the page did not hand over its "
                          "latest console entries in time (its main thread may be busy)")


def digest_suffix(rdata: Dict[str, Any]) -> str:
    """Render network/console digests attached to action results."""
    bits = []
    net = rdata.get("network")
    if net:
        if net.get("failed"):
            fails = "; ".join(
                f"{f.get('method','?')} {f.get('url','?')} -> {f.get('status') or f.get('error')}"
                for f in net["failed"])
            bits.append(f"FAILED requests: {fails}")
        if net.get("pending"):
            bits.append(f"{net['pending']} request(s) still in flight")
    cons = rdata.get("consoleErrors")
    if cons and cons.get("errors"):
        first = "; ".join(cons.get("first", [])[:2])
        bits.append(f"{cons['errors']} console error(s): {first}")
    if rdata.get("consoleFlushTimedOut"):
        # The extension reads its console buffer only after the page has handed over what
        # it emitted; a page too busy to answer leaves that read possibly short.
        degrade("console_unflushed")
        bits.append(CONSOLE_UNFLUSHED_NOTE)
    return ("\n  " + "\n  ".join(bits)) if bits else ""


def service_workers_suffix(rdata: Dict[str, Any]) -> str:
    """Render the current-site Service Worker registrations attached to a
    navigate result (present only when the loaded document's origin has
    one). `controlled` distinguishes a SW that is actively serving THIS
    document from one that is merely registered (typical on a first visit,
    where the SW activates but only controls the page on the next load)."""
    sw = rdata.get("serviceWorkers")
    if not sw:
        return ""
    regs = sw.get("registrations") or []
    ctrl = sw.get("controller")
    if sw.get("controlled") and ctrl:
        head = f"service worker: document controlled by {ctrl.get('scriptURL')} ({ctrl.get('state')})"
    elif regs:
        head = "service worker: registered but NOT controlling this document (typical on first load)"
    else:
        return ""
    lines = ["\n  " + head]
    for reg in regs:
        for phase in ("active", "waiting", "installing"):
            worker = reg.get(phase)
            if worker:
                detail = f" — {phase}: {worker.get('scriptURL')} ({worker.get('state')})"
                break
        else:
            detail = ""
        lines.append(f"\n    - scope {reg.get('scope')}{detail}")
    return "".join(lines)
