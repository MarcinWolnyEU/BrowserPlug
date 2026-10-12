#!/usr/bin/env python3
"""Fused navigate+read: a navigation may return the page it navigated to, but ONLY that page.

Agent-experience feedback, 2026-08-03. 512 navigations in the call corpus were
immediately followed by a read of the same tab: the single most common two-call
sequence in the data. `navigate` reports the post-settle URL, the title, the
redirect chain, the failed requests, the console errors and the service workers:
everything about the page except the page. Fusing the two makes an N-page
harvest cost N tool calls instead of 2N.

This module is the *policy* half: leg budgets, argument validation, the wire shape
of the read leg, and, at the centre of the module, the decision of whether the read
leg may run at all. The *mechanism* half (two `calls.request` calls chained inside
one tool) lives in `server/tools/navigate.py` and `page.py`, in `navigate(read=)` and `page_read(url=)`.
Those two are the same feature reached from opposite ends and MUST answer
identically, which is why every judgement they share lives here rather than being
written twice.

Pure functions over plain dicts, stdlib only, no I/O, so the whole policy is
unit-testable without a browser, a server, or a socket.

THE RULE THAT MATTERS (acceptance criterion 3): content is NEVER returned for a
page the agent did not ask for. A navigation that failed, or that landed
somewhere else, reports THAT, in preference to content. Getting this wrong is how
an agent ends up confidently summarising an SSO consent wall as if it were the
product page it asked for. `read_block_reason` is that rule, and it fails CLOSED:
anything it cannot confirm, it refuses, naming the one-call escape hatch instead.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlsplit

# Sibling policy modules, also pure and stdlib-only: url_norm owns host/dot-segment
# identity, url_diff the "is this a login wall / route change" judgement, so this guard
# and the landing report cannot disagree about what counts as the same place.
from server.tools.url_diff import diff_urls, landing_wall, state_fragment_changed
from server.tools.url_norm import ascii_host

# The read formats a fused call accepts. The first three ride `content.read` (the
# same wire action page_read uses); "records" rides `content.snapshot`, see
# read_plan, which is the only place that distinction is spelled out.
READ_FORMATS = ("markdown", "text", "html", "records")

# Mirrors page_snapshot's own `max_elements` default, so a fused records read and a
# standalone page_snapshot(mode="records") return the same number of rows.
RECORDS_MAX_DEFAULT = 150

# navigate's historical budget is `timeout_ms / 1000 + 15`; keeping the constant here
# (and the arithmetic in leg_budgets) gives that number ONE owner instead of a literal
# duplicated at every call site.
NAV_LEG_OVERHEAD_SECONDS = 15.0

# The read leg gets its own, SMALLER cap than page_read's standalone 60.0: by the time
# it runs the page has already settled, so a 60 s read budget would only buy worst-case
# latency. This is what keeps a fused call from being twice as slow as an unfused one.
READ_LEG_TIMEOUT_SECONDS = 30.0

# The read block is fenced so an agent can tell the navigation diagnostics from the page
# itself, and so a settle TIMEOUT is labelled ON the content rather than in a note the
# agent may skim past. The content that follows is byte-identical to page_read's output.
SEPARATOR_TEXT = "\n\n--- page content ---\n"
SEPARATOR_TIMEDOUT = "\n\n--- page content (read at settle TIMEOUT — may be incomplete) ---\n"
SEPARATOR_DOM_UNSTABLE = ("\n\n--- page content (read while the DOM was still changing; "
                          "may be incomplete) ---\n")
SEPARATOR_RECORDS = "\n\n--- records ---\n"

# Navigation-result flags that block the read, in the order they are checked. Each
# reason states the CAUSE only; read_skipped_line adds the next action, so the two
# never drift apart.
_BLOCKING_FLAGS = (
    ("netError", "the navigation reported a network error"),
    ("aboutBlank", "the document is still about:blank; nothing rendered"),
    ("superseded", "a newer navigation superseded this one"),
    ("tabClosed", "the tab closed during navigation"),
)


def leg_budgets(timeout_ms: float) -> Tuple[float, float]:
    """(nav_leg_seconds, read_leg_seconds) for a fused call.

    The nav leg is byte-for-byte today's value, so wiring this in cannot change what an
    UNFUSED navigate does. The read leg is a flat cap that does NOT scale with
    `timeout_ms`: `timeout_ms` is the settle budget, and settling is already over by the
    time the read runs. Worst case at defaults is 35 + 30 = 65 s against today's 35 s,
    paid only when content was actually asked for, and never on a failed navigation
    (the read leg is skipped entirely then, so the failure path costs exactly today).
    """
    return (timeout_ms / 1000 + NAV_LEG_OVERHEAD_SECONDS, READ_LEG_TIMEOUT_SECONDS)


def normalize_read(value: Any) -> Optional[str]:
    """None / "" -> None (no read). A known format -> itself. Anything else -> ValueError.

    Forgiving about surrounding whitespace and case ("Markdown" is a format, not a typo),
    in the same spirit as ActStep's `text`->`value` alias: an agent that spelled the
    intent recognisably should not be refused over presentation.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"read must be a string, got {type(value).__name__}")
    normalized = value.strip().lower()
    if not normalized:
        return None
    if normalized not in READ_FORMATS:
        raise ValueError(f"unknown read format {value!r}")
    return normalized


def read_request_error(read: Any, wait: str) -> Optional[str]:
    """The exact error string navigate must return for an unusable read=/wait= pair.

    None when the pair is fine. Both refusals are decidable without touching the wire,
    so a bad call costs zero round trips, and both name the accepted values or the
    working alternative, because the reader is an agent mid-task, not a log.
    """
    try:
        normalized = normalize_read(read)
    except ValueError:
        return f'Error: unknown read="{read}" (use markdown | text | html | records)'
    if normalized and wait == "none":
        # wait="none" returns before the load even starts, so there is nothing to read.
        # Refusing beats returning the PREVIOUS page's content under the new page's header.
        return ('Error: read= needs a loaded page but wait="none" returns before the load '
                'starts. Use wait="settled" (default) or drop read=.')
    return None


def read_plan(read: str, *, tab_id: Optional[int] = None, scope: Optional[str] = None,
              max_chars: int = 20000, offset: int = 0,
              frame_id: Optional[int] = None) -> Tuple[str, Dict[str, Any]]:
    """(wire_action, data) for the read leg: the ONE place the two shapes are spelled out.

    markdown | text | html -> ("content.read", ...), page_read's own request shape.
    records                -> ("content.snapshot", {"mode": "records", ...}).

    Note the key divergence the caller must not paper over: a text read scopes with
    `scope`, a records read scopes with `root`. One `read_scope` argument on navigate
    routes to whichever the chosen format wants; get it wrong and the selector is
    silently ignored, which reads as "the page has no such subtree".
    """
    if read not in READ_FORMATS:
        raise ValueError(f"unknown read format {read!r}")
    if read == "records":
        # Mirrors page_snapshot's builder exactly (root is sent even when None), so the
        # extension sees a request indistinguishable from a standalone snapshot call.
        action = "content.snapshot"
        data: Dict[str, Any] = {"root": scope, "max": RECORDS_MAX_DEFAULT,
                                "includeHidden": False, "dedupe": True, "mode": "records"}
    else:
        action = "content.read"
        data = {"format": read, "maxChars": max_chars, "offset": offset}
        if scope:
            data["scope"] = scope
    if tab_id is not None:
        data["tabId"] = tab_id
    if frame_id is not None:
        data["frameId"] = frame_id
    return (action, data)


def host_of(url: Any) -> Optional[str]:
    """A URL's comparable host: lowercased, port dropped, a leading "www." removed.

    None when there is no host to read: an empty string, a bare path, "about:blank",
    anything urlsplit cannot find a netloc in. Callers treat that None as "unknown",
    never as "matches".
    """
    if not isinstance(url, str) or not url.strip():
        return None
    try:
        host = urlsplit(url.strip()).hostname
    except ValueError:
        return None  # malformed IPv6 literal etc.: unknowable, not equal
    if not host:
        return None
    host = ascii_host(host.lower())
    # Strip "www." only when something dotted survives: "www.pl" must stay "www.pl",
    # or it would collapse to the bare TLD and match every *.pl host on the suffix rule.
    if host.startswith("www.") and "." in host[4:]:
        host = host[4:]
    return host or None


def same_site(requested_url: Any, landed_url: Any) -> bool:
    """Did `landed_url` stay on the site `requested_url` asked for?

    Host comparison only. IDN hosts compare by their punycode form (via url_norm), and a
    same-host login wall is caught separately, by `landing_wall` in read_block_reason.
    Scheme and port are IGNORED, because an http->https upgrade or a :8080 dev port is
    not a different site. True iff the two normalized hosts are
    equal or one is a suffix of the other ON A LABEL BOUNDARY: allegro.pl ~
    www.allegro.pl ~ edge.allegro.pl, but allegro.pl is NOT notallegro.pl (which a naive
    endswith would wave through) and NOT accounts.google.com.

    Deliberately no Public Suffix List: the cost of a false refusal is exactly one extra
    call (the refusal names `page_read(tab_id=N)`), while the cost of a false ACCEPT is
    an agent reading a login wall as if it were the page it asked for. So an unreadable
    host on either side returns False: fail CLOSED.
    """
    a, b = host_of(requested_url), host_of(landed_url)
    if not a or not b:
        return False
    if a == b:
        return True
    return a.endswith("." + b) or b.endswith("." + a)


def _display_host(url: Any) -> str:
    """What to CALL a URL in a refusal: its host, else the URL itself, else "unreported".
    Unlike host_of this keeps "www.", quoting the agent's own spelling back at it."""
    if isinstance(url, str) and url.strip():
        text = url.strip()
        try:
            host = urlsplit(text).hostname
        except ValueError:
            host = None
        return host.lower() if host else text
    return "unreported"


def read_block_reason(nav: Any, requested_url: Optional[str]) -> Optional[str]:
    """AC-3 from the module docstring. Why the read leg must NOT run, or None to let it run.

    `nav` is the `navigation.navigate` result dict; `requested_url` is the URL the agent
    asked for, or None for back/forward/reload (there is no "you asked for X" to compare
    a history move against, so the cross-site rule correctly cannot fire on one).

    Checked in order: netError, aboutBlank, superseded, tabClosed, then the landing
    host, so the FIRST cause is the one reported, not the last symptom.

    DELIBERATELY NOT a blocker: `timedOut`. A settle timeout is not a page the agent did
    not ask for; it is the page it asked for, possibly incomplete. Withholding it would
    repeat the mistake page_act's timeout report was built to fix (a timeout reported as
    "nothing happened" was wrong every time it was checked). The read proceeds and the
    content is labelled with SEPARATOR_TIMEDOUT so the agent knows what it is holding.
    """
    if not isinstance(nav, dict):
        nav = {}
    for flag, reason in _BLOCKING_FLAGS:
        if nav.get(flag):
            return reason
    if requested_url:
        landed = nav.get("url")
        if same_site(requested_url, landed):
            # Same host is not the same page: a login path, a changed hash route or a
            # captcha path on the landed URL still means the asked-for page was not served.
            wall = landing_wall(requested_url, landed)
            if wall:
                return (f"landed on {wall} at {_display_host(landed)}, "
                        f"not the page you asked for")
            return None
        if host_of(requested_url) and host_of(landed):
            return (f"landed on {_display_host(landed)}, "
                    f"not the {_display_host(requested_url)} you asked for")
        # One side has no readable host, so an honest mismatch cannot be claimed, but
        # neither can a match. Refuse, and say precisely that rather than inventing a
        # comparison the URLs do not support.
        return (f"could not confirm the landing URL ({_display_host(landed)}) is the "
                f"{_display_host(requested_url)} you asked for")
    return None


def read_result_moved(nav: Any, read_data: Any) -> Optional[str]:
    """Why the content the read leg returned is NOT from the page navigation settled on.

    The read runs after settle, and a script can still redirect in between (a client-side
    auth check, a delayed router push). The read result carries `location.href` as `url`;
    comparing it with the navigation's landed URL is the only way to notice. Host, path,
    scheme and hash-route differences count, and so do query changes (a dropped, added or
    rewritten parameter, as `url_diff` reports them) and a changed key=value fragment such
    as `#tab=billing`. Tracking parameters arriving or leaving stay quiet (`url_diff`
    drops them from the diff), and so do a re-encoded query, a reordered one and a plain
    anchor (`#reviews`), none of which changes the page. A missing URL on either side
    proves nothing, so it does not block: the navigation-side guard already passed.
    """
    if not isinstance(nav, dict) or not isinstance(read_data, dict):
        return None
    nav_url, read_url = nav.get("url"), read_data.get("url")
    if not (isinstance(nav_url, str) and nav_url.strip()
            and isinstance(read_url, str) and read_url.strip()):
        return None
    diff = diff_urls(nav_url, read_url)
    query_moved = any(c.kind != "reencoded" for c in diff.query_changes)
    if (diff.host_changed or diff.path_changed or diff.route_changed
            or diff.scheme_downgraded or query_moved
            or state_fragment_changed(nav_url, read_url)):
        return (f"the tab moved to {read_url} after the navigation settled on {nav_url} "
                f"(a script redirect), so the content read is not the page navigated to")
    return None


def separator_for(nav: Any, read: str) -> str:
    """The fence that labels the read block: records, plain, read at a settle timeout, or
    read while a wait="dom" still saw the DOM changing."""
    if read == "records":
        return SEPARATOR_RECORDS
    if isinstance(nav, dict) and nav.get("timedOut"):
        return SEPARATOR_TIMEDOUT
    # wait="dom" that gave up with the DOM still moving read a page that may be mid-render.
    if (isinstance(nav, dict) and nav.get("waitMode") == "dom" and nav.get("settled")
            and nav.get("domStable") is False):
        return SEPARATOR_DOM_UNSTABLE
    return SEPARATOR_TEXT


def _page_read_call(tab_id: Optional[int]) -> str:
    """The escape hatch, spelled as a call the agent can paste. No tab id means the
    active tab, and `page_read(tab_id=None)` would be advice that does not run."""
    return f"page_read(tab_id={tab_id})" if tab_id is not None else "page_read()"


def read_skipped_line(reason: str, tab_id: Optional[int]) -> str:
    """The line appended to the navigation result when the read was refused.

    Names the cause, states that the navigation result above still stands (it is not a
    failed call), and hands over the exact ONE call that reads the page the tab is
    actually on, so no argument has to be spent on an override.
    """
    return (f"\n  READ SKIPPED: {reason} — the navigation result above is what happened. "
            f"To read the page you actually landed on: {_page_read_call(tab_id)}.")


def read_failed_line(err: Any, tab_id: Optional[int]) -> str:
    """The line appended when the navigation SUCCEEDED but the read leg errored.

    The navigation result is never erased by a read failure: the tab did move, and an
    agent told only "Error reading page" would conclude nothing happened and navigate
    again, which is the mistake page_act's timeout report already exists to prevent.
    """
    return (f"\n  READ FAILED after a successful navigation: {err} — the tab DID move "
            f"(see above); retry the read alone with {_page_read_call(tab_id)}.")
