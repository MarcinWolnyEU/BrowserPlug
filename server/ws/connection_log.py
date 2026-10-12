#!/usr/bin/env python3
"""Forensics for the extension WebSocket: why a browser left, and what it says it did
while it was away.

A Firefox under memory pressure dropped its socket on 2026-09-21 and the whole server-side
record of it was two lines: `INFO:websockets.server:connection closed`, and, on the second
drop, `ERROR:__main__:Error handling extension connection: no close frame received or
sent`. No browser named, no close code, no duration, nothing about the two reconnect
attempts that followed and failed. The investigation could not get past "connection
closed", so this module exists to make the next one a single grep.

Three renderers, all pure and stdlib-only, each returning a string the caller logs
verbatim (or "" when there is nothing worth a line):

* `disconnect_line()` - one INFO line per departing browser: who, how long it lasted, the
  close code as the SERVER saw it, and a gloss, because 1006 means something specific
  ("the peer vanished") and nobody reads close-code tables during an incident.
* `hello_latency_line()` - the hello arrived, but slowly. A browser that takes 7 s to say
  hello is a browser under load, which is a different problem from one that never says it,
  and the two used to be indistinguishable.
* `journal_line()` - the extension's own account of what happened since its previous
  hello. Its console dies with the browser, and these investigations tend to start after
  a force-kill, so this is the only place its side of the story is readable.

`hello_timeout_close_reason()` builds the close reason that lets the extension tell a
hello timeout from a token problem: it refetches its token on any 4401, which after a
timeout is wasted work on a browser that is already struggling.
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# What the common close codes MEAN, in the words an operator needs mid-incident. Only
# codes that actually reach this server are listed; anything else prints its number
# alone, which is honest, rather than a guess dressed up as a translation.
CLOSE_GLOSS = {
    1000: "normal close",
    1001: "going away: the browser or the extension is shutting down",
    1005: "no status: the peer closed without sending a code",
    1006: "abnormal: no close frame, so the browser or its extension vanished "
          "rather than hanging up",
    1011: "the peer hit an internal error",
    1012: "the peer is restarting",
    1013: "the peer asked us to try again later",
    4401: "unauthorized: this server turned the socket away",
}

# Past this, a hello is worth a line of its own. Under it, every connect would log one,
# and a line that prints on every connect is one nobody reads.
SLOW_HELLO_MS = 1000

# The extension's journal is a diagnostic, not a data feed: it arrives from a possibly
# remote browser and lands verbatim in a log file and in server_info.
MAX_JOURNAL_ENTRIES = 20
MAX_JOURNAL_REASON = 120
MAX_JOURNAL_EVENT = 24
MAX_JOURNAL_LINE = 600


def _duration(seconds) -> str:
    """12s / 4m12s / 1h02m. Compact enough to sit inside a one-line log record."""
    try:
        seconds = int(max(0, float(seconds)))
    except (TypeError, ValueError):
        return "an unknown time"
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def close_phrase(code=None, reason=None) -> str:
    """`close code 1006 (abnormal: ...)`, with the peer's own reason when it sent one.

    A missing code is its own finding here: websockets leaves `close_code` unset when the
    connection ended without either side framing a close, which is exactly the shape of a
    browser that was killed, so it reads as that rather than as a blank.

    Both arguments are read off a socket object, so both are type-checked rather than
    trusted: a test double answers `close_reason` with a Mock, and a diagnostic that
    raises on the teardown path takes the teardown with it.
    """
    if not isinstance(code, int) or isinstance(code, bool):
        code = None
    reason = reason.strip() if isinstance(reason, str) else ""
    if code is None:
        head = "no close code (the connection ended without a close frame either way)"
    else:
        gloss = CLOSE_GLOSS.get(code)
        head = f"close code {code}" + (f" ({gloss})" if gloss else "")
    if reason:
        head += f', peer reason: "{reason[:MAX_JOURNAL_REASON]}"'
    return head


def closed_from_exception(exc) -> Tuple[Optional[int], Optional[str]]:
    """(code, reason) out of a `websockets.exceptions.ConnectionClosed`.

    websockets 17 carries the two Close frames separately: `rcvd` is what the PEER sent
    and `sent` is what we sent, and the peer's account is the one worth logging. Both are
    None on an abnormal close, which leaves (None, None) and `close_phrase` says so.
    Defensive throughout: this runs on an error path and must never raise there.
    """
    for attr in ("rcvd", "sent"):
        frame = getattr(exc, attr, None)
        code = getattr(frame, "code", None)
        if isinstance(code, int):
            reason = getattr(frame, "reason", None)
            return code, reason if isinstance(reason, str) else None
    code = getattr(exc, "code", None)
    reason = getattr(exc, "reason", None)
    return (code if isinstance(code, int) else None,
            reason if isinstance(reason, str) else None)


def disconnect_line(describe: str, *, connected_for=None,
                    code=None, reason=None) -> str:
    """The one INFO line a departing browser gets. `describe` is ExtBrowser.describe()."""
    held = f" after {_duration(connected_for)}" if connected_for is not None else ""
    return f"{describe} disconnected{held} - {close_phrase(code, reason)}"


def hello_latency_line(describe: str, latency_ms, *, window_seconds=None) -> str:
    """"...: hello arrived after 7.3s", or "" when it was prompt.

    Said out loud because a slow hello and an absent one look identical from the outside
    and have opposite remedies: one is a browser to wait for, the other is a browser to
    go and look at.
    """
    try:
        latency_ms = float(latency_ms)
    except (TypeError, ValueError):
        return ""
    if latency_ms < SLOW_HELLO_MS:
        return ""
    window = f" (the window is {window_seconds:g}s)" if window_seconds else ""
    return (f"{describe}: hello arrived after {latency_ms / 1000:.1f}s{window} - the "
            f"browser is slow, not absent")


def hello_timeout_close_reason(seconds, max_bytes: int = 123) -> str:
    """The close reason for a socket that never sent a hello.

    It must START with `hello timeout:` and stay inside the protocol's close-reason cap:
    the extension keys on that prefix to tell this apart from a token rejection, and
    refetching a token it already has is wasted work on a browser that is already
    struggling to answer at all.
    """
    reason = (f"hello timeout: no extension_hello within {seconds:g}s; "
              f"reconnect and send the hello first")
    return reason.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")


def clean_journal(value: Any, max_entries: int = MAX_JOURNAL_ENTRIES) -> List[Dict[str, Any]]:
    """The extension's journal, reduced to entries this server is willing to keep.

    Everything here crossed the WebSocket from a browser that may be remote and may be
    running a build older or newer than this server, so each field is validated on its
    own and an unusable one is dropped rather than failing the entry. Unknown keys are
    dropped too: the contract is `{t, e, code?, reason?, attempt?}` and a journal is not
    a place to smuggle payload through.
    """
    if not isinstance(value, list):
        return []
    entries = []
    for item in value[:max_entries]:
        if not isinstance(item, dict):
            continue
        event = item.get("e")
        if not isinstance(event, str) or not event.strip():
            continue
        kept: Dict[str, Any] = {"e": event.strip()[:MAX_JOURNAL_EVENT]}
        stamp = item.get("t")
        if isinstance(stamp, (int, float)) and not isinstance(stamp, bool) and stamp > 0:
            kept["t"] = int(stamp)
        for key in ("code", "attempt"):
            number = item.get(key)
            if isinstance(number, int) and not isinstance(number, bool):
                kept[key] = number
        reason = item.get("reason")
        if isinstance(reason, str) and reason.strip():
            kept["reason"] = reason.strip()[:MAX_JOURNAL_REASON]
        entries.append(kept)
    return entries


def _entry_text(entry: Dict[str, Any]) -> str:
    parts = [entry.get("e") or "?"]
    stamp = entry.get("t")
    if isinstance(stamp, (int, float)) and not isinstance(stamp, bool):
        try:
            parts.append(datetime.fromtimestamp(stamp / 1000.0).strftime("%H:%M:%S"))
        except (OverflowError, OSError, ValueError):
            pass          # a clock the browser got wrong is not worth losing the entry over
    if isinstance(entry.get("attempt"), int):
        parts.append(f"attempt {entry['attempt']}")
    if isinstance(entry.get("code"), int):
        parts.append(f"code {entry['code']}")
    text = " ".join(parts)
    if entry.get("reason"):
        text += f' "{entry["reason"]}"'
    return text


def journal_line(describe: str, entries, max_chars: int = MAX_JOURNAL_LINE) -> str:
    """"<browser> reports since its last hello: close 23:54:58 code 1006; open 23:56:08; ..."

    Returns "" when there is nothing to report, so a periodic hello from a browser that
    has simply been connected the whole time logs nothing. Overlong journals lose whole
    entries from the END and say how many, because an entry cut in half reads as data.
    """
    entries = [e for e in (entries or []) if isinstance(e, dict)]
    if not entries:
        return ""
    head = f"{describe} reports since its last hello: "
    budget = max(0, max_chars - len(head))
    rendered, used = [], 0
    for index, entry in enumerate(entries):
        try:
            text = _entry_text(entry)
        except Exception:     # a diagnostic must never be the thing that raises
            continue
        cost = len(text) + (2 if rendered else 0)
        if rendered and used + cost > budget:
            return head + "; ".join(rendered) + f" (+{len(entries) - index} more)"
        rendered.append(text)
        used += cost
    if not rendered:
        return ""
    return head + "; ".join(rendered)
