"""Bounded in-memory maps for per-client and per-browser routing state.

Three tables grow with the number of clients and browsers the server has ever seen:
the browser each client selected, the browser that served each client's last call, and the
record of browsers that left. A server that runs for weeks would keep all of them for
every launcher pid that ever connected. `ExpiringMap` bounds each one:

  * `ttl`         an entry not written or touched for this many seconds is dropped
  * `max_entries` past this many, the entry least recently written or touched goes first

It is a real `dict` subclass, so the code and tests that read `selection[key]`, `key in
selection` or compare it with a dict literal keep working. Pure stdlib; the clock is a
constructor argument (and a public attribute) so tests move time without sleeping.
"""

import time
from collections import OrderedDict
from typing import Callable, Iterable, Optional

#: A client not heard from in this long has moved on; its selection and its
#: "who served me last" entry go with it.
CLIENT_TTL_SECONDS = 24 * 3600

#: Ceiling on the client tables. Far above any realistic count of live MCP clients, so
#: it only bites when pid keys pile up.
MAX_CLIENT_ENTRIES = 512

#: How many departed browsers are remembered, oldest evicted first.
MAX_DEPARTED = 64

# Sweeping walks the table, so it runs at most this often (in clock seconds).
_SWEEP_INTERVAL = 60.0


class ExpiringMap(dict):
    """A dict that forgets: by age (`ttl`) and by count (`max_entries`).

    Writing a key or calling `touch` marks it as seen. Reading does not, because the
    read paths (the connection map, health) must not keep a dead client alive.
    `on_drop`, when set, receives the keys removed by expiry or eviction (never those the
    caller deleted itself).
    """

    def __init__(self, *, ttl: Optional[float] = None, max_entries: Optional[int] = None,
                 clock: Callable[[], float] = time.monotonic,
                 on_drop: Optional[Callable[[Iterable[str]], None]] = None):
        super().__init__()
        self.ttl = ttl
        self.max_entries = max_entries
        self.clock = clock
        self.on_drop = on_drop
        # key -> last seen, least recently seen first.
        self._seen: "OrderedDict[str, float]" = OrderedDict()
        self._last_sweep = clock()

    # --- writes: keep _seen in step ---------------------------------------------------

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        self._mark(key)
        self._enforce()

    def __delitem__(self, key):
        super().__delitem__(key)
        self._seen.pop(key, None)

    def pop(self, key, *default):
        self._seen.pop(key, None)
        return super().pop(key, *default)

    def popitem(self):
        key, value = super().popitem()
        self._seen.pop(key, None)
        return key, value

    def clear(self):
        super().clear()
        self._seen.clear()

    def setdefault(self, key, default=None):
        if key not in self:
            self[key] = default
        return self[key]

    def update(self, *args, **kwargs):
        for key, value in dict(*args, **kwargs).items():
            self[key] = value

    # --- the bound --------------------------------------------------------------------

    def _mark(self, key):
        self._seen[key] = self.clock()
        self._seen.move_to_end(key)

    def touch(self, key) -> None:
        """This key's owner is still around: restart its clock. Also the moment an
        overdue table is swept, so the work rides on traffic instead of a timer."""
        if key in self:
            self._mark(key)
        self._sweep_if_due()

    def _sweep_if_due(self) -> None:
        if self.ttl is None:
            return
        now = self.clock()
        if now - self._last_sweep >= min(_SWEEP_INTERVAL, self.ttl):
            self.sweep()

    def sweep(self) -> list:
        """Drop every entry older than `ttl`. Returns the dropped keys."""
        now = self.clock()
        self._last_sweep = now
        if self.ttl is None:
            return []
        stale = []
        for key, seen in self._seen.items():   # least recently seen first
            if now - seen < self.ttl:
                break
            stale.append(key)
        self._drop(stale)
        return stale

    def _enforce(self) -> None:
        self._sweep_if_due()
        if self.max_entries is None or len(self) <= self.max_entries:
            return
        excess = len(self) - self.max_entries
        self._drop(list(self._seen)[:excess])

    def _drop(self, keys) -> None:
        for key in keys:
            self._seen.pop(key, None)
            dict.pop(self, key, None)
        if keys and self.on_drop is not None:
            try:
                self.on_drop(list(keys))
            except Exception:
                pass    # bookkeeping must never fail the call that triggered it
