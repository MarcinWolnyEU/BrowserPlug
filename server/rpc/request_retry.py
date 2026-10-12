"""Transient-disconnect retry policy for WebSocket requests.

When a browser extension drops its socket mid-request, `ExtensionRpc.fail_pending_requests`
settles every in-flight future immediately (a deliberate fix: otherwise each call burned
its full timeout and then misreported "Request timed out"). That fast-fail is correct, but
the drop is very often purely transport-transient: the extension reconnects within a second
or two (Firefox reloaded the extension, a service worker cycled, a flaky socket blipped) and
a manual retry succeeds. This module makes that retry automatic: ONCE, for read-only actions
only, within a short grace window, so an idempotent read/screenshot survives a blip while a
real, lasting breakage is still surfaced as an error.

This module is the *policy* half (which actions are safe to re-send, how long to wait for a
reconnect, how to strip the private plumbing keys off an envelope). The *mechanism* half is a
couple of minimal hooks that lives in `server/rpc/extension_rpc.py` (`fail_pending_requests` tags the
failure; `send_request_and_wait` consults `is_idempotent` + `wait_for_reconnect` and re-sends
once). Kept dependency-free (stdlib + asyncio only) so it never imports the server back.
"""

import asyncio
import logging
import os

logger = logging.getLogger(__name__)

# --- Which wire actions are safe to re-send automatically ---------------------------------
#
# ONLY read-only actions belong here: an auto-retry re-sends the SAME request, so anything
# that mutates browser/tab/window state, navigates, runs page code, or arms/disarms a monitor
# must NEVER be in this set (re-sending it could double-apply the side effect). These are the
# `calls.request("<action>")` call sites in server/tools/, filtered to the reads, plus a
# few legacy read-only actions the protocol tests still exercise directly.
IDEMPOTENT_ACTIONS = frozenset({
    # -- reads reached by the v2 tool surface (server/tools/) -------------------------
    "tabs.list",                 # tabs(action="list")
    "tabs.active",               # active-tab read
    "windows.get_last_focused",  # window lookup (populate=False)
    "content.read",              # page_read (markdown / text / html)
    "content.snapshot",          # page_snapshot
    "content.frames",            # frame enumeration for page_snapshot
    "tabs.screenshot",           # page_screenshot capture  <-- the action that prompted this
    "console.read",              # console buffer (no tool since 2026-08-03;
                                 # navigate/page_act carry console errors themselves)
    "requests.recent",           # requests(action="recent")
    "requests.list_captured",    # requests(action="list"): captured-request listing
    "requests.get_content",      # requests(action="get"): read a captured body
    # -- legacy read-only actions still handled by background.js (protocol-level tests) -----
    "tabs.captureVisibleTab",    # screenshot twin of tabs.screenshot
    "content.text",              # legacy plain-text read
    "content.get_text",
    "content.html",              # legacy HTML read
    "content.get_html",
    "windows.list",
    "windows.get",
    "windows.get_current",
})
# Deliberately EXCLUDED (mutating / code-running / monitor-arming; never auto-retried):
#   navigation.navigate, navigation.back/forward/reload/go/go_to_url,
#   content.act, content.execute_script, tabs.create/close/switch/update/routing,
#   windows.create/close/update/focus, requests.start_monitoring/stop_monitoring.
# (The history.* actions left this list on 2026-08-03 with the history tool and its
#  browser permission; content.read_clipboard went with the clipboard-read removal.)

# Failure codes the RPC layer stamps on an error envelope. Same strings as the constants in
# tool_outcome.py (a unit test pins the match); duplicated because this module stays
# stdlib-only and must not import the tool layer.
CODE_TIMEOUT = "TIMEOUT"
CODE_DISCONNECTED = "EXTENSION_DISCONNECTED"
CODE_SEND_FAILED = "EXTENSION_SEND_FAILED"
CODE_RESTARTING = "SERVER_RESTARTING"

# Keys the server attaches to a transient-disconnect envelope so this layer can recognize it.
# They are internal plumbing and MUST be stripped before an envelope reaches tool code.
PRIVATE_ENVELOPE_KEYS = ("transient_disconnect", "instance_id")

# How long to wait for the dropped browser to reconnect before giving up and returning the
# original error. Short, scoped, and never a long hang (the whole point of the fast-fail it
# rides on). Overridable via env for operators/tests; read at call time by
# reconnect_grace_seconds() so a monkeypatch of either this attribute or the env var takes.
RECONNECT_GRACE_SECONDS = 8.0
_GRACE_ENV_VAR = "BROWSERPLUG_RECONNECT_GRACE"


def is_idempotent(action):
    """True if `action` is a read-only wire action safe to re-send exactly once."""
    return action in IDEMPOTENT_ACTIONS


# What the agent is told after a dropped socket. The drop can land after the page already
# ran the request, so "safe to retry" is true only for the reads above. Anything else may
# have clicked, typed, navigated or run a script: the agent has to look before repeating.
_READ_ADVICE = "This call only reads, so it is safe to retry."
_MUTATING_ADVICE = ("The action may have already run in the page before the connection "
                    "dropped. Check the page state (page_snapshot, page_read or tabs) "
                    "before repeating it.")


def disconnect_advice(action):
    """The sentence that closes a disconnect message, chosen by what `action` can do."""
    return _READ_ADVICE if is_idempotent(action) else _MUTATING_ADVICE


def with_disconnect_advice(envelope, action):
    """Copy of a disconnect `envelope` with the advice appended to its error text and a
    `retryable` flag that only a read-only action gets. Anything else passes through."""
    if not isinstance(envelope, dict) or "error" not in envelope:
        return envelope
    out = dict(envelope)
    out["error"] = f"{envelope['error']} {disconnect_advice(action)}"
    out["retryable"] = is_idempotent(action)
    return out


def reconnect_grace_seconds():
    """The grace window in seconds. `BROWSERPLUG_RECONNECT_GRACE` (read at call time)
    overrides the module default; an unparseable value is ignored with a warning."""
    raw = os.environ.get(_GRACE_ENV_VAR)
    if raw is not None:
        try:
            return float(raw)
        except (TypeError, ValueError):
            logger.warning("Ignoring non-numeric %s=%r; using %.1fs",
                           _GRACE_ENV_VAR, raw, RECONNECT_GRACE_SECONDS)
    return RECONNECT_GRACE_SECONDS


def strip_private_keys(envelope):
    """Return a copy of `envelope` with the transient-disconnect plumbing keys removed, so
    tool code never sees them. Non-dicts pass through untouched."""
    if isinstance(envelope, dict):
        return {k: v for k, v in envelope.items() if k not in PRIVATE_ENVELOPE_KEYS}
    return envelope


async def wait_for_reconnect(lookup, grace_seconds, poll_seconds=0.2):
    """Poll `lookup()` until it returns a truthy value or `grace_seconds` elapses.

    `lookup` is a zero-arg callable that returns the reconnected live browser (truthy) or
    None; the caller keeps all registry knowledge, so this stays dependency-free. Returns
    True on reconnect, False on timeout. A non-positive grace does exactly one check.
    """
    if lookup():
        return True
    if grace_seconds <= 0:
        return False
    loop = asyncio.get_event_loop()
    deadline = loop.time() + grace_seconds
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(poll_seconds, remaining))
        if lookup():
            return True
