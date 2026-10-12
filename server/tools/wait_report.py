"""What a `navigate` result says about its wait.

A pure, stdlib-only policy module (the pattern in `.tmp/dev-docs/tool-surface.md`):
`navigate.render_navigation` calls it, and `tests/unit/test_wait_report.py` drives it
without a browser. It decides four things from the `navigation.navigate` data:

* which wait the extension ran, in the words the result line uses;
* the note `wait="dom"` owes when it returned with the DOM still changing;
* the `timing:` line, shown only for a settle that was slow or timed out, so the agent
  sees which phase held it (a late load event, or a network that would not go quiet);
* the main-frame events behind a reported abort, in the order the browser sent them.
"""

from typing import Any, Dict

from server.tools.formatting import fmt_ms

WAITS = ("settled", "load", "dom")

# Mirrors DOM_STABLE_MS and DOM_STABLE_MAX_MS in both background.js files.
DOM_STABLE_MS = 100
DOM_STABLE_MAX_MS = 3000

# A settle shorter than this needs no breakdown: the total already says it was quick.
TIMING_LINE_MIN_MS = 2000

_PHASES = {
    "settled": (("commit", "commit"), ("load", "load"), ("quiet", "network quiet")),
    "load": (("commit", "commit"), ("load", "load")),
    "dom": (("commit", "commit"), ("load", "load"), ("dom", "DOM stable")),
}

_EVENT_WORDS = {"commit": "commit", "error": "abort", "done": "load complete"}

# An abort's events are worth showing; other net errors already name their cause.
_ABORT_MARK = "aborted"


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def wait_mode(r: Dict[str, Any], requested: str) -> str:
    """The wait the extension ran. One that does not report it predates `wait="dom"`, so
    it ran its default settle for that value and the requested wait for the others."""
    mode = r.get("waitMode")
    if mode in WAITS:
        return mode
    return "settled" if requested == "dom" else requested


def settled_phrase(r: Dict[str, Any], requested: str) -> str:
    """The parenthesis after the title of a navigation that met its wait."""
    ms = fmt_ms(r.get("waitedMs"))
    mode = wait_mode(r, requested)
    if mode == "load":
        return f" (load event in {ms})"
    if mode == "dom":
        if r.get("domStable"):
            return f" (load event, then DOM unchanged for {DOM_STABLE_MS}ms, in {ms})"
        return f" (load event; returned after {ms} with the DOM not yet stable)"
    return f" (settled in {ms})"


def dom_note(r: Dict[str, Any], requested: str) -> str:
    """The NOTE for a `wait="dom"` that returned before the DOM stopped changing, or that
    could not watch it, and for an extension too old to have run that wait at all."""
    if requested == "dom" and r.get("settled") and r.get("waitMode") not in WAITS:
        return ('\n  NOTE: the connected extension does not know wait="dom" and waited for '
                "network quiet instead; reload the extension to get the DOM wait.")
    if wait_mode(r, requested) != "dom" or not r.get("settled") or r.get("domStable"):
        return ""
    if r.get("domError"):
        return (f"\n  NOTE: the DOM could not be watched ({r['domError']}), so this is the "
                "page at its load event and may still be rendering. Re-read it before "
                "relying on it.")
    return (f"\n  NOTE: the DOM was still changing {DOM_STABLE_MAX_MS // 1000} s after the load "
            'event, when wait="dom" stops waiting; this may be the page mid-render. Re-read '
            'it, or use wait="settled".')


def dom_unstable(r: Dict[str, Any], requested: str) -> bool:
    """True when the result is a `wait="dom"` that returned with the DOM still moving."""
    return (wait_mode(r, requested) == "dom" and bool(r.get("settled"))
            and r.get("waitMode") == "dom" and not r.get("domStable"))


def timing_line(r: Dict[str, Any], requested: str) -> str:
    """When each phase of a slow (TIMING_LINE_MIN_MS or more) or timed-out settle first
    held, in ms after the navigation began; a phase that never came says so."""
    timing = r.get("timing")
    if not isinstance(timing, dict):
        return ""
    waited = r.get("waitedMs")
    if not (r.get("timedOut") or (_number(waited) and waited >= TIMING_LINE_MIN_MS)):
        return ""
    parts = []
    for key, label in _PHASES[wait_mode(r, requested)]:
        value = timing.get(key)
        parts.append(f"{label} {fmt_ms(value)}" if _number(value) else f"{label} never")
    return "\n  timing: " + ", ".join(parts)


def nav_events_line(r: Dict[str, Any]) -> str:
    """The main-frame commit, abort and load-complete events behind a reported abort, in
    arrival order, as ms after the navigation began. A URL is shown only where it differs
    from the page the tab landed on. "" for any other result."""
    events = r.get("navEvents")
    if not isinstance(events, list) or _ABORT_MARK not in str(r.get("netError") or ""):
        return ""
    landed = r.get("url")
    parts = []
    for e in events[:8]:
        if not isinstance(e, dict) or not _number(e.get("ms")):
            continue
        bit = f"{_EVENT_WORDS.get(e.get('ev'), str(e.get('ev')))} {int(e['ms']):+d}ms"
        if e.get("url") and e.get("url") != landed:
            bit += f" ({e['url']})"
        parts.append(bit)
    return ("\n  main-frame events: " + ", ".join(parts)) if parts else ""
