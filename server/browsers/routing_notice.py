#!/usr/bin/env python3
"""Telling an agent that its calls have started landing in a DIFFERENT browser.

On 2026-09-21 a Firefox extension dropped its socket while an agent was driving it. The
server did everything right: it evicted the browser, dropped the stale selection and
served the next call from the surviving Chrome. What the agent got back was
`No open tab matches "bgap_h3/dispatch" (searched url + title of 4 tabs)` - a
success-shaped answer from a browser it had never asked for. It spent eight minutes
diagnosing a Firefox content-process crash that had not happened.

The routing was right the whole time; the response simply never mentioned it. So:

* `RoutingMemory` remembers which browser instance served each MCP client's last
  browser-bound call, and answers "did that change, and was it the client's own doing?".
  A client's own `browsers(action="use")` goes in through `chose()`, so a switch somebody
  ASKED for is never reported back to them as news.
* `notice()` renders the sentence. Two shapes, because the two situations need different
  next steps: the previous browser LEFT (name it, say when and why, and say that its tab
  ids and refs are gone), or it is still connected and routing simply moved (say where
  calls go now and why).
* `DepartedBrowser` is what the registry keeps about a browser after it goes. It is a
  `str` subclass carrying `describe()`, so every reader that had only ever wanted the
  name - `resolve_target_browser`'s error text formats it straight into a sentence -
  keeps working byte for byte, while the notice can ask it when and how the browser went.

Pure and stdlib-only; `server/tools/browser_calls.py` and `server/server.py` each carry a few lines
of hook. The notice reaches the client through the same seam degradations do: it is
recorded in a ContextVar during the call and `tool_outcome.ToolOutcomePolicy` puts it at
the FRONT of the response, where an agent reads it before the content it was expecting.
"""

import logging
from contextvars import ContextVar
from datetime import datetime
from typing import Any, Dict, Optional

from server.browsers.client_state import CLIENT_TTL_SECONDS, MAX_CLIENT_ENTRIES, ExpiringMap
from server.ws.connection_log import close_phrase

logger = logging.getLogger(__name__)

# One slot per in-flight tool call, for the same reason tool_outcome.py has one: many
# clients share one event loop, each ASGI request runs in its own task, and a task copies
# the context at creation. The slot holds a MUTABLE dict and writers MUTATE it: a `.set()`
# made inside a context copy (a spawned task, or the worker thread a synchronous tool
# runs in) is invisible to the outcome policy waiting outside it.
_NOTICE: ContextVar[Optional[Dict[str, Any]]] = ContextVar(
    "browserplug_routing_notice", default=None)


def arm() -> None:
    """Install a fresh slot. Called by the outcome policy before the tool runs, so one call's
    notice can never be delivered on the back of the next one."""
    _NOTICE.set({})


def record(text: str) -> None:
    """File the notice for the call in progress. Last writer wins: a tool that reaches
    the browser twice reports the routing change once."""
    if not text:
        return
    slot = _NOTICE.get()
    if slot is None:
        slot = {}
        _NOTICE.set(slot)
    slot["text"] = text


def pending() -> str:
    """The notice recorded during this call, or ""."""
    return (_NOTICE.get() or {}).get("text") or ""


def prepend(notice: str, text: str) -> str:
    """The notice, then the answer. This order is the whole point: an agent that reads
    the tool's own output first has already believed it."""
    if not notice:
        return text
    return f"{notice}\n\n{text}" if text else notice


class DepartedBrowser(str):
    """What a browser leaves behind: its `describe()`, plus when and how it went.

    A `str` so the readers that predate this - the "you selected a browser that is gone"
    error formats it into a sentence - are untouched, and so a registry that has only the
    old plain string still works everywhere a DepartedBrowser would.
    """

    def __new__(cls, describe: str, *, instance_id: Optional[str] = None,
                at: Optional[datetime] = None, close_code: Optional[int] = None,
                close_reason: Optional[str] = None, slot: Optional[int] = None):
        obj = super().__new__(cls, describe)
        obj.instance_id = instance_id
        obj.at = at
        obj.close_code = close_code
        obj.close_reason = close_reason
        obj.slot = slot
        return obj

    @property
    def at_hms(self) -> Optional[str]:
        return self.at.strftime("%H:%M:%S") if isinstance(self.at, datetime) else None


class RoutingMemory:
    """Which browser instance served each MCP client's last browser-bound call.

    Per client, never global: several agents share one server, and a notice built from a
    global "last browser" would fire on every client whenever any one of them switched.
    In memory only - a restart forgets, and a forgotten client simply gets no notice on
    its next call, which is the same silence a first call gets. The table is bounded the
    same way: a client not heard from for CLIENT_TTL_SECONDS is forgotten, and past
    MAX_CLIENT_ENTRIES the least recently heard from goes first.
    """

    def __init__(self, *, ttl: float = CLIENT_TTL_SECONDS,
                 max_entries: int = MAX_CLIENT_ENTRIES, clock=None):
        kwargs = {} if clock is None else {"clock": clock}
        self._served: Dict[str, str] = ExpiringMap(ttl=ttl, max_entries=max_entries,
                                                   **kwargs)

    def drop_clients(self, client_keys) -> None:
        """Drop these clients (their selection expired, so their history goes with it)."""
        for key in client_keys:
            self._served.pop(key, None)

    def previous(self, client_key: str) -> Optional[str]:
        return self._served.get(client_key)

    def served(self, client_key: str, instance_id: str) -> Optional[str]:
        """Record who served this call. Returns the PREVIOUS instance id when the browser
        changed, else None - which covers both the client's first call (nothing to
        compare against, and a notice there would be noise) and the ordinary case."""
        if not client_key or not instance_id:
            return None
        previous = self._served.get(client_key)
        self._served[client_key] = instance_id     # also marks the client as seen
        return previous if previous and previous != instance_id else None

    def chose(self, client_key: str, instance_id: str) -> None:
        """The client picked this browser itself (`browsers(action="use")`). Its next call
        must not be told about a move it just made."""
        if client_key and instance_id:
            self._served[client_key] = instance_id


PINNED_REASON = "it is the browser pinned as the default"
FIRST_REASON = "it is first in the roster, which is where a session that has not chosen lands"

_REFS_GONE = ("Tab ids and page_snapshot refs from the browser that left are gone - "
              "re-run tabs (and page_snapshot) here before acting on anything you read "
              "earlier.")
_REFS_ELSEWHERE = ("Tab ids and page_snapshot refs do not carry between browsers - "
                   "re-run tabs (and page_snapshot) here before acting on anything you "
                   "read earlier.")
_SWITCH = 'Choose for this session with browsers(action="use", target=<slot or name>).'


def notice(*, now_serving: str, previous: str, departed: bool,
           departed_hms: Optional[str] = None, close_code: Optional[int] = None,
           close_reason: Optional[str] = None, connected_count: int = 1,
           reason: str = "") -> str:
    """The routing-change sentence, or "" when there is nothing to name.

    Kept to three lines. A notice that has to be scrolled is one an agent skims past, and
    this one has to be read the first time: it is the difference between "this browser has
    no such tab" and "you are looking at the wrong browser".
    """
    now_serving = (now_serving or "").strip()
    previous = (previous or "").strip()
    if not now_serving or not previous:
        return ""

    if departed:
        when = f" at {departed_hms}" if departed_hms else ""
        lines = [f"ROUTING CHANGED: {previous} disconnected{when} - "
                 f"{close_phrase(close_code, close_reason)}. This call was served by "
                 f"{now_serving} instead.",
                 _REFS_GONE]
        if connected_count > 1:
            lines.append(f"{connected_count} browsers are connected. {_SWITCH}")
        else:
            lines.append("It is the only browser connected; if the one that left starts "
                         "again it reconnects under the same identity and calls route "
                         "back to it.")
        return "\n".join(lines)

    why = f" Routing moved because {reason}." if reason else ""
    return "\n".join([
        f"ROUTING CHANGED: this call was served by {now_serving}; your previous call "
        f"went to {previous}, which is still connected.{why}",
        _REFS_ELSEWHERE,
        _SWITCH,
    ])
