"""Arrival order between one client's browser switch and its other calls.

An agent that sends `browsers(action="use")` and, in the same turn, a call such as
`tabs(action="open")` usually has the two run at once: pi, for one, runs a turn's tool
calls in parallel. Unordered, the other call can pick its browser before the switch
lands, and the tab opens in the browser the agent just left. In the 2026-10-08 eval, 5
of the 17 tabs opened alongside a switch went to the previous browser.

The rule this module enforces, per client key (the key the browser selection is stored
under, `routing.client_key`):

  * a switch waits for that client's calls already in flight, so each of them finishes
    in the browser it started in;
  * a call that arrives after a switch waits for the switch to finish, so it runs in
    the browser the switch chose;
  * calls that do not switch never wait for each other. Parallel reads stay parallel
    and pay one list scan.

"Arrives" means reaching the dispatcher. Two requests a client sends at the same
instant can still reach it in either order; what this guarantees is that whichever order
they arrive in is the order they take effect in, and the switch result names the calls
that were ahead of it. A switch waits at most `SWITCH_WAIT_CAP_SECONDS` for the calls
ahead of it, so one stuck call cannot hold the client's later calls for its whole
timeout.

Pure asyncio, stdlib only. The hook is one `async with` in `mcp_server._on_call_tool`;
`mcp_tools.BrowserPlugTools.call_gate` decides the key and which calls switch.
"""

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any, AsyncIterator, Dict, List, Mapping, Optional, Tuple

# How long a switch waits for the calls already in flight. Above a navigate's default
# 20 s budget would hold a client's next calls for as long as its slowest call; below a
# couple of seconds a switch would overtake an ordinary read. A call still running after
# the cap is named in the switch result as possibly still using the previous browser.
SWITCH_WAIT_CAP_SECONDS = 15.0

# The labels of the calls that were in flight when the current switch arrived, for the
# `browsers` tool to name in its result. Empty outside a switch.
_AHEAD: ContextVar[Tuple[str, ...]] = ContextVar("browserplug_calls_ahead_of_switch",
                                                 default=())


def calls_ahead_of_switch() -> Tuple[str, ...]:
    """Inside a switch: the calls of the same client that reached the server first and
    were still running when it arrived, as `tool(action="x")` labels. Else ()."""
    return _AHEAD.get()


def call_label(name: str, arguments: Optional[Mapping[str, Any]]) -> str:
    """`tabs(action="open")`, or the bare tool name when the call names no action."""
    action = (arguments or {}).get("action")
    if isinstance(action, str) and action.strip():
        return f'{name}(action="{action.strip()}")'
    return name


class _Entry:
    __slots__ = ("label", "switches", "done")

    def __init__(self, label: str, switches: bool):
        self.label = label
        self.switches = switches
        self.done = asyncio.Event()


class CallOrder:
    """The per-client lanes. One instance per tool surface (`BrowserPlugTools`)."""

    def __init__(self, switch_wait_cap: float = SWITCH_WAIT_CAP_SECONDS):
        self.switch_wait_cap = switch_wait_cap
        self._lanes: Dict[str, List[_Entry]] = {}

    def in_flight(self, key: str) -> int:
        """How many calls of `key` are admitted or waiting (for tests and diagnostics)."""
        return len(self._lanes.get(key, ()))

    @asynccontextmanager
    async def admitted(self, key: str, *, switches: bool, label: str) -> AsyncIterator[None]:
        """Hold the call until the calls it must follow have finished.

        Everything up to the first wait runs synchronously when the `async with` is
        entered, so the entry is filed in the order the dispatcher started the calls.
        """
        lane = self._lanes.setdefault(key, [])
        ahead = list(lane) if switches else [e for e in lane if e.switches]
        entry = _Entry(label, switches)
        lane.append(entry)
        token = None
        try:
            if ahead:
                waits = asyncio.gather(*(e.done.wait() for e in ahead))
                if switches:
                    try:
                        await asyncio.wait_for(waits, self.switch_wait_cap)
                    except asyncio.TimeoutError:
                        pass
                else:
                    await waits
            if switches:
                token = _AHEAD.set(tuple(e.label for e in ahead if not e.switches))
            yield
        finally:
            if token is not None:
                _AHEAD.reset(token)
            entry.done.set()
            lane.remove(entry)
            if not lane:
                self._lanes.pop(key, None)
