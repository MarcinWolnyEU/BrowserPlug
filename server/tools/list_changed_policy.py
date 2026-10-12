"""When a tools/list_changed is owed, and which sessions it goes to: the pure half of
list_changed.py.

Stdlib only, no SDK import and no clock of its own (callers pass `now`), so the debounce
and the session bookkeeping are unit-tested without a server, an event loop or a sleep.
"""

import threading
from collections import OrderedDict
from typing import Any, List, Optional, Tuple

# A change is announced QUIET_SECONDS after the last change of a burst, and never later
# than MAX_WAIT_SECONDS after the first one. The stdio relay polls /health every 3 s, so
# an HTTP client never hears of a change later than a relay client does.
QUIET_SECONDS = 0.3
MAX_WAIT_SECONDS = 3.0

# How many handshake-era sessions the book keeps. A client killed without a DELETE never
# says goodbye (the SDK keeps its session forever as well: session_idle_timeout=None), so
# past this many the least recently active session is dropped. A push to a dead session
# is a silent no-op, so the bound is about memory only.
MAX_SESSIONS = 512


class ChangeGate:
    """The debounce of one transport binding. Confined to that binding's event loop, so it
    has no lock.

    Every armed change is announced, including one undone inside the window: a client
    that listed the intermediate surface holds a stale list until a notification makes
    it list again. A signature equal to the last one seen arms nothing, so a recompute
    that reshaped nothing pushes nothing."""

    def __init__(self, quiet: float = QUIET_SECONDS, max_wait: float = MAX_WAIT_SECONDS):
        self.quiet = quiet
        self.max_wait = max_wait
        self._seen: Any = None
        self._first: Optional[float] = None

    def reset(self, signature: Any) -> None:
        """A new binding: `signature` is the surface its clients will list first."""
        self._seen = signature
        self._first = None

    def change(self, signature: Any, now: float) -> Optional[float]:
        """The surface is `signature` after a possible change at `now`.

        Returns the seconds until the announcement is due, or None when the signature is
        the last one seen (an armed deadline then stands as it was)."""
        if signature == self._seen:
            return None
        self._seen = signature
        if self._first is None:
            self._first = now
        return max(0.0, min(self.quiet, self._first + self.max_wait - now))

    @property
    def pending(self) -> bool:
        """Whether a change is armed and not yet settled."""
        return self._first is not None

    def settle(self) -> bool:
        """At the deadline: True when a change was armed since the last settle."""
        owed = self._first is not None
        self._first = None
        return owed


class SessionBook:
    """Live handshake-era sessions by key, each with the event loop that recorded it.

    The key is the Mcp-Session-Id over HTTP and the connection token on stdio and
    in-process connections. A broadcast sends only to the sessions recorded on its own
    loop: the SDK's streams belong to the loop that serves them. Thread-safe, because the
    middleware notes from whichever loop serves a session (uvicorn's, the --stdio loop, a
    test's own) while a broadcast reads from its binding's loop."""

    def __init__(self, cap: int = MAX_SESSIONS):
        self.cap = cap
        self._lock = threading.Lock()
        self._entries: "OrderedDict[str, Tuple[Any, Any]]" = OrderedDict()

    def note(self, key: str, session: Any, loop: Any) -> None:
        """Record `key`'s latest session object and make it the most recently active."""
        with self._lock:
            self._entries[key] = (session, loop)
            self._entries.move_to_end(key)
            while len(self._entries) > self.cap:
                self._entries.popitem(last=False)

    def forget(self, key: str) -> bool:
        """Drop `key`; False when it was not recorded."""
        with self._lock:
            return self._entries.pop(key, None) is not None

    def forget_loop(self, loop: Any) -> int:
        """Drop every session recorded on `loop`; returns how many."""
        with self._lock:
            keys = [key for key, (_session, at) in self._entries.items() if at is loop]
            for key in keys:
                del self._entries[key]
            return len(keys)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def on_loop(self, loop: Any) -> List[Tuple[str, Any]]:
        """[(key, session)] for the sessions recorded on `loop`, oldest first."""
        with self._lock:
            return [(key, session) for key, (session, at) in self._entries.items()
                    if at is loop]

    def keys(self) -> List[str]:
        with self._lock:
            return list(self._entries)

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)
