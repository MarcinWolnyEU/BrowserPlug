#!/usr/bin/env python3
"""Per-browser FIFO admission control for extension requests.

Nothing in this stack bounded concurrency. `send_request_and_wait` registered a future and
put the frame on the wire immediately, so N concurrent agents drove N simultaneous requests
down one WebSocket, into one extension, onto one JS thread. Measured on a real fan-out the
navigate timeout rate went 0.8% -> 39.3% (474 calls returned nothing), and what the agent got
back was the bare "Request timed out after 35.0 seconds": a string that names no cause and
is indistinguishable from "the page did not load". Agents then did the only two things that
string invites: retry immediately and raise timeout_ms. Both make the queue longer.

This module is the *policy* half: a FIFO gate per browser instance_id plus the two
agent-facing strings that make contention nameable. The *mechanism* half is four small hooks
in server/server.py, server/rpc/extension_rpc.py and server/browsers/browser_registry.py (import,
__init__, send_request_and_wait, BrowserRegistry.remove). Stdlib + asyncio only, and it never imports the server back, exactly like request_retry.py.

Four properties are load-bearing:

  * The caller's timeout covers EXECUTION only. Queue wait elapses before the response future
    exists, so a request that waited its turn still gets its whole budget to run in, and
    `Ticket.queue_wait` reports that wait separately instead of quietly eating it.
  * Over-capacity is REFUSED, not absorbed. Past the queue depth the caller gets BROWSER BUSY
    and the browser is never touched: back-pressure an agent can act on, rather than a
    timeout it cannot tell apart from a broken page.
  * The single-agent path is untouched. With nothing else in flight `acquire()` is one integer
    compare and returns WITHOUT EVER AWAITING, so it adds no event-loop turn and cannot
    reorder anything; and `timeout_message` returns the string it returned before this gate
    existed, byte for byte, when there was no contention, so a solo diagnosis never gets
    wordier or less true.
  * Fairness is FIFO by arrival, deliberately. Sub-agents of one Claude Code share one
    launcher and one pid (BrowserCalls.client_key), so per-client weighting would hand a whole
    fan-out a single share while *appearing* fair. client_key is carried for stats only,
    never for routing.

Honest scope: this bounds what the extension is asked to do at once. It does NOT fix an
`executeScript` promise that never settles because its document was torn down mid-injection
(the dominant root cause; that needs the extension-side injection deadline), and no queue
can make N agents writing to the SAME tab correct. What it does fix is the third failure:
nobody could tell that contention was what happened.
"""

from __future__ import annotations

import asyncio
import collections
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Deque, Dict, Optional, Set

logger = logging.getLogger(__name__)

# --- Limits ------------------------------------------------------------------------------
#
# Why 3 and not 1, and not 5. The measured knee is between 1 and 2 concurrent agents, but
# that knee is caused by every tab_id-less call landing on the SAME active tab, which no
# admission policy can fix. The gate's actual job is to bound the working set of the
# extension's single JS thread: 3 admits independent short reads on different tabs
# while keeping the number of simultaneously-injecting documents small. Setting it to 1 fully
# serialises the browser and is the documented escape hatch for an operator who wants
# determinism over throughput, but it also maximises head-of-line blocking, which is why it
# is not the default.
MAX_CONCURRENT_DEFAULT = 3
_MAX_ENV_VAR = "BROWSERPLUG_MAX_CONCURRENT_REQUESTS"

# How many may WAIT behind those. Past this, callers are refused rather than parked, because
# a queue nobody will reach the front of is just a timeout with extra steps.
MAX_QUEUED_DEFAULT = 24
_DEPTH_ENV_VAR = "BROWSERPLUG_MAX_QUEUE_DEPTH"

# Longest a request may sit in the queue before it is refused. Generous on purpose: waiting is
# the desired behaviour, this is only the backstop that turns an indefinite wait into an
# explicit, re-issuable refusal.
MAX_QUEUE_WAIT_DEFAULT = 60.0
_WAIT_ENV_VAR = "BROWSERPLUG_MAX_QUEUE_WAIT"

# Used for the retry-in ETA before this gate has ever timed a request. A guess, and labelled
# as one by the "~" in the message; it only has to be the right order of magnitude.
_ETA_FALLBACK_EXECUTION_SECONDS = 5.0

# How many completed requests feed the mean execution time behind that ETA. Short window on
# purpose: a browser that just got slow should move the ETA, not be averaged against an hour
# of fast reads.
_EXECUTION_SAMPLES = 20

# Below this, a queue wait is not worth naming in the timeout message; it is scheduler noise
# from a slot that was handed over in the same tick, not a wait an agent should act on.
_REPORTABLE_WAIT_SECONDS = 0.05


def _int_env(name: str, default: int, minimum: int = 1) -> int:
    """Read a positive int from the environment at CALL time, warning (never raising) on
    anything unusable: a mistyped knob must degrade to the default, not brick the bridge."""
    raw = os.environ.get(name)
    if raw is not None:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            logger.warning("Ignoring non-numeric %s=%r; using %d", name, raw, default)
        else:
            if value >= minimum:
                return value
            logger.warning("Ignoring out-of-range %s=%r (minimum %d); using %d",
                           name, raw, minimum, default)
    return default


def max_concurrent_requests() -> int:
    """Requests allowed to execute against ONE browser at once.
    `BROWSERPLUG_MAX_CONCURRENT_REQUESTS` (read at call time) overrides the module default;
    an unparseable or <1 value is ignored with a warning."""
    return _int_env(_MAX_ENV_VAR, MAX_CONCURRENT_DEFAULT)


def max_queued_requests() -> int:
    """How many may wait behind the executing set before admission is refused.
    `BROWSERPLUG_MAX_QUEUE_DEPTH`, same read-at-call-time rules."""
    return _int_env(_DEPTH_ENV_VAR, MAX_QUEUED_DEFAULT)


def max_queue_wait_seconds() -> float:
    """Longest a request may sit in the queue before it is refused with BROWSER BUSY.
    `BROWSERPLUG_MAX_QUEUE_WAIT`, same read-at-call-time rules."""
    raw = os.environ.get(_WAIT_ENV_VAR)
    if raw is not None:
        try:
            return float(raw)
        except (TypeError, ValueError):
            logger.warning("Ignoring non-numeric %s=%r; using %.1fs",
                           _WAIT_ENV_VAR, raw, MAX_QUEUE_WAIT_DEFAULT)
    return MAX_QUEUE_WAIT_DEFAULT


class QueueRejected(Exception):
    """Admission refused: nothing was sent and the browser was not touched.

    `.message` is the whole agent-facing string (the caller returns it verbatim as the tool's
    error); `.stats` is the gate snapshot it was built from; `.reason` is "depth" (the queue
    was already full) or "wait" (this request queued but never reached the front).
    """

    def __init__(self, message: str, stats: Dict[str, Any], reason: str = "depth"):
        super().__init__(message)
        self.message = message
        self.stats = stats
        self.reason = reason


@dataclass(eq=False)
class Ticket:
    """One admitted (or waiting) request's accounting. Identity-hashed on purpose: two
    requests with identical fields are still two distinct slots."""

    browser_key: str
    action: Optional[str] = None
    client_key: Optional[str] = None
    enqueued_at: float = 0.0
    admitted_at: Optional[float] = None
    # Seconds actually spent queued. EXACTLY 0.0 on the fast path; timeout_message keys the
    # byte-identical no-contention string off that, so it must never be a rounding artefact.
    queue_wait: float = 0.0
    inflight_on_arrival: int = 0
    queued_on_arrival: int = 0
    # Most requests seen executing at once while THIS one was executing. It is what the
    # timeout message names: "N other request(s) were executing while this one ran".
    peak_inflight: int = 0
    limit: int = 0
    # True once ExtensionRpc has taken over releasing this slot after a timeout or cancel
    # (the browser may still be executing it); the request path's own `finally` then
    # leaves it alone.
    lingering: bool = False


@dataclass
class _Waiter:
    future: "asyncio.Future"
    ticket: Ticket


class BrowserGate:
    """The FIFO admission gate for ONE browser instance_id.

    Not an asyncio.Semaphore: a semaphore knows how many slots are free but nothing about who
    waited, how long, or what they competed with, and every one of those is needed to answer
    an agent honestly after a timeout. It is also strictly LIFO-unsafe under cancellation,
    whereas the whole point here is arrival order.
    """

    def __init__(self, key: str, limit: Optional[int] = None):
        self.key = key
        # A fixed limit pins this gate (tests, and any future per-browser override); None
        # means "whatever the env says right now", so the knob takes effect without a restart.
        self._fixed_limit = limit
        self._inflight = 0
        self._waiters: Deque[_Waiter] = collections.deque()
        self._live: Set[Ticket] = set()
        # Counters behind snapshot(); every one of them answers a question an operator or an
        # agent actually asks ("is this browser saturated / was it ever / did it refuse me").
        self._admitted = 0
        self._rejected_busy = 0
        self._contention_timeouts = 0
        self._queue_wait_total = 0.0
        self._queue_wait_max = 0.0
        self._peak_inflight_ever = 0
        self._executions: Deque[float] = collections.deque(maxlen=_EXECUTION_SAMPLES)

    # --- state ---------------------------------------------------------------------------

    @property
    def limit(self) -> int:
        return self._fixed_limit if self._fixed_limit is not None else max_concurrent_requests()

    @property
    def inflight(self) -> int:
        return self._inflight

    @property
    def queued(self) -> int:
        return len(self._waiters)

    def mean_execution(self) -> Optional[float]:
        """Mean seconds a request spent EXECUTING (post-admission), over the last few, or
        None before any completed, so callers can say "unknown" instead of inventing one."""
        if not self._executions:
            return None
        return sum(self._executions) / len(self._executions)

    # --- admission -----------------------------------------------------------------------

    async def acquire(self, action: Optional[str] = None,
                      client_key: Optional[str] = None) -> Ticket:
        """Admit one request against this browser, waiting its turn if others hold the slots.

        FIFO by arrival. Raises QueueRejected when the queue is already full ("depth") or when
        this request waited the whole queue budget without reaching the front ("wait"); in
        both cases NOTHING has been sent.

        Deliberately NOT bounded by the caller's own timeout: that budget is reserved for
        execution, so a request that queued still gets its full time to actually run.
        """
        limit = self.limit
        now = time.monotonic()
        ticket = Ticket(browser_key=self.key, action=action, client_key=client_key,
                        enqueued_at=now, inflight_on_arrival=self._inflight,
                        queued_on_arrival=len(self._waiters), limit=limit)

        # --- fast path: room to run, and nobody already waiting for it ------------------
        # One integer compare and a return. No future, no deque entry, and critically no
        # await, so a solo agent's call reaches the wire in exactly the same event-loop turn
        # it always did. `not self._waiters` is the FIFO half: capacity can open without a
        # release (the limit was widened, a waiter is still unwinding a cancellation), and a
        # newcomer walking into that gap would overtake a request that had been queued for
        # seconds, making arrival order hold only while the gate was saturated.
        if self._inflight < limit and not self._waiters:
            self._admit(ticket, now=now, queued=False)
            return ticket

        depth = max_queued_requests()
        if len(self._waiters) >= depth:
            self._rejected_busy += 1
            raise QueueRejected(overflow_message(self, action, "depth"),
                                self.snapshot(), reason="depth")

        waiter = _Waiter(future=asyncio.get_running_loop().create_future(), ticket=ticket)
        self._waiters.append(waiter)
        budget = max_queue_wait_seconds()
        try:
            await asyncio.wait_for(waiter.future, timeout=budget)
        except asyncio.TimeoutError:
            # A release may have handed this waiter the slot in the very tick the wait
            # expired. Dropping it there would leak that slot forever (the gate degrades
            # monotonically: leak `limit` of them and the browser is bricked with no error
            # anywhere), so an admission that already happened is honoured, late or not.
            if self._took_slot(waiter):
                return ticket
            self._drop_waiter(waiter)
            self._rejected_busy += 1
            waited = max(0.0, time.monotonic() - ticket.enqueued_at)
            raise QueueRejected(overflow_message(self, action, "wait", waited=waited),
                                self.snapshot(), reason="wait") from None
        except asyncio.CancelledError:
            # Same race, other cause: if the slot was already ours, pass it straight to the
            # next waiter rather than taking it to the grave.
            if self._took_slot(waiter):
                self.release(ticket)
            else:
                self._drop_waiter(waiter)
            raise
        return ticket

    def release(self, ticket: Optional[Ticket]) -> None:
        """Return `ticket`'s slot and hand it to the LEFT-most waiter.

        Idempotent, and a no-op for None: every caller wraps this in a `finally` that also
        covers paths where admission never happened, and a double release that decremented
        twice would over-admit forever.
        """
        if ticket is None or ticket not in self._live:
            return
        self._live.discard(ticket)
        self._inflight -= 1
        if ticket.admitted_at is not None:
            self._executions.append(max(0.0, time.monotonic() - ticket.admitted_at))
        self._pump()

    def note_contention_timeout(self) -> None:
        """Count a timeout that happened while this browser was contended (stats only)."""
        self._contention_timeouts += 1

    def snapshot(self) -> Dict[str, Any]:
        """Live depth + lifetime counters for server_info / /health. Milliseconds because
        that is the unit the rest of those payloads report latency in."""
        mean = self.mean_execution()
        return {
            "limit": self.limit,
            "inflight": self._inflight,
            "queued": len(self._waiters),
            "peak_inflight_ever": self._peak_inflight_ever,
            "admitted": self._admitted,
            "rejected_busy": self._rejected_busy,
            "contention_timeouts": self._contention_timeouts,
            "queue_wait_max_ms": round(self._queue_wait_max * 1000),
            "queue_wait_avg_ms": (round(self._queue_wait_total / self._admitted * 1000)
                                  if self._admitted else 0),
            "mean_execution_ms": round(mean * 1000) if mean is not None else None,
        }

    # --- internals -----------------------------------------------------------------------

    def _admit(self, ticket: Ticket, now: Optional[float] = None, queued: bool = True) -> None:
        now = time.monotonic() if now is None else now
        self._inflight += 1
        ticket.admitted_at = now
        # Exactly 0.0 (not a sub-microsecond measurement) when nothing was waited for, so the
        # no-contention timeout string stays byte-identical to the pre-gate one.
        ticket.queue_wait = max(0.0, now - ticket.enqueued_at) if queued else 0.0
        ticket.limit = self.limit
        self._live.add(ticket)
        self._admitted += 1
        self._queue_wait_total += ticket.queue_wait
        self._queue_wait_max = max(self._queue_wait_max, ticket.queue_wait)
        self._peak_inflight_ever = max(self._peak_inflight_ever, self._inflight)
        # Everything executing has just gained a sibling. peak_inflight is per-ticket because
        # the timeout message must name what THIS request actually competed with, not what
        # the browser happened to be doing at some other moment.
        for live in self._live:
            if live.peak_inflight < self._inflight:
                live.peak_inflight = self._inflight

    def _pump(self) -> None:
        """Hand free slots to waiters, strictly left to right."""
        while self._waiters and self._inflight < self.limit:
            waiter = self._waiters.popleft()
            if waiter.future.done():
                continue  # already cancelled/expired; its slot was never taken
            # Transfer the slot NOW rather than when the waiter wakes: the handover has to be
            # atomic, or a later arrival could slip into the gap between the two.
            self._admit(waiter.ticket)
            waiter.future.set_result(None)

    def _took_slot(self, waiter: _Waiter) -> bool:
        """True if `waiter` was admitted before its wait ended (see the races in acquire)."""
        return (waiter.future.done() and not waiter.future.cancelled()
                and waiter.future.exception() is None and waiter.ticket in self._live)

    def _drop_waiter(self, waiter: _Waiter) -> None:
        try:
            self._waiters.remove(waiter)
        except ValueError:
            pass


class RequestQueue:
    """Registry of per-browser gates. One gate per instance_id: two browsers are two
    independent bridges, and making them share a limit would let a busy Chrome throttle an
    idle Firefox."""

    def __init__(self):
        self._gates: Dict[str, BrowserGate] = {}

    def gate_for(self, browser_key: Any) -> BrowserGate:
        """The gate for `browser_key`, created on first use."""
        key = str(browser_key)
        gate = self._gates.get(key)
        if gate is None:
            gate = self._gates[key] = BrowserGate(key)
        return gate

    def drop_browser(self, browser_key: Any) -> None:
        """Forget a departed browser's gate. Its in-flight callers are already being failed by
        `ExtensionRpc.fail_pending_requests`, and each still runs its own `finally: release(...)` against
        the gate object it captured, so dropping the registry entry here reclaims the stats
        without stranding anyone."""
        self._gates.pop(str(browser_key), None)

    def move_browser(self, old_key: Any, new_key: Any) -> None:
        """Follow a browser through a re-key (provisional -> instance_id). The gate moves
        with it, so callers already queued or in flight keep the depth they were counted
        in; if `new_key` already has a gate (a merge into an established record) the old
        one is dropped instead, as `drop_browser` does."""
        old_key, new_key = str(old_key), str(new_key)
        if old_key == new_key:
            return
        gate = self._gates.pop(old_key, None)
        if gate is not None and new_key not in self._gates:
            gate.key = new_key
            self._gates[new_key] = gate

    def note_contention_timeout(self, ticket: Optional[Ticket]) -> None:
        """Record that `ticket` timed out while its browser was contended."""
        if ticket is None:
            return
        gate = self._gates.get(str(ticket.browser_key))
        if gate is not None:
            gate.note_contention_timeout()

    def snapshot(self) -> Dict[str, Any]:
        """{instance_id: gate snapshot} for server_info / /health."""
        return {key: gate.snapshot() for key, gate in self._gates.items()}


# --- Agent-facing strings -----------------------------------------------------------------
#
# These are read by an AI agent in the middle of a task, and the failure mode they exist to
# remove is an agent concluding the wrong thing and acting on it. So each one names the CAUSE
# and the NEXT ACTION, and explicitly rules out the two reflexes that made the original
# incident worse (retry immediately, raise timeout_ms).

def is_contended(ticket: Optional[Ticket]) -> bool:
    """True when `timeout_message` will name contention (and so tell the agent NOT to
    retry). The one place that decides it, so a caller marking the failure retryable
    cannot disagree with the words."""
    return not (ticket is None or (ticket.peak_inflight <= 1 and ticket.queue_wait <= 0.0))


def timeout_message(ticket: Optional[Ticket], timeout: Any) -> str:
    """The error for a request that ran out of budget: contention-aware.

    With no contention (or no ticket at all, i.e. the internal retry re-send) this is BYTE
    IDENTICAL to the string this function returned before the admission gate existed. That is
    load-bearing twice over: every existing test and every honest solo diagnosis stays exactly
    as it was, and the contended wording stays *meaningful* precisely because it is not what a
    solo agent sees.
    """
    if not is_contended(ticket):
        return f"Request timed out after {timeout} seconds"
    others = max(0, ticket.peak_inflight - 1)
    waited = (f", and it waited {ticket.queue_wait:.1f}s in the queue before it started"
              if ticket.queue_wait > _REPORTABLE_WAIT_SECONDS else "")
    return (
        f"Request timed out after {timeout} seconds — the browser bridge was CONTENDED: "
        f"{others} other request(s) were executing against this browser while this one ran"
        f"{waited}"
        f" (concurrency limit {ticket.limit}). This is NOT evidence that the page failed to "
        f"load: under contention the extension can be too busy to answer at all. Do NOT retry "
        f"immediately and do NOT raise timeout_ms — both make the queue longer. Reduce the "
        f"number of agents/threads driving this browser, or serialize them. Current depth: "
        f"server_info > browser_concurrency."
    )


def overflow_message(gate: BrowserGate, action: Optional[str], reason: str,
                     waited: float = 0.0) -> str:
    """The BROWSER BUSY refusal: back-pressure EXPRESSED rather than absorbed as a timeout.

    reason "depth": the queue was already full, so this never even joined it.
    reason "wait":  it queued but never reached the front within the queue budget.
    Either way nothing was sent, which is the single most useful fact in the string: it makes
    re-issuing the identical call unambiguously safe.
    """
    inflight, queued, limit = gate.inflight, gate.queued, gate.limit
    depth = max_queued_requests()
    mean = gate.mean_execution() or _ETA_FALLBACK_EXECUTION_SECONDS
    eta = queued / max(1, limit) * mean
    what = action or "request"
    if reason == "wait":
        opening = (f"this request waited {waited:.0f}s in this browser's request queue "
                   f"without reaching the front ({inflight} executing, {queued} still ahead)")
    else:
        opening = (f"this browser already has {inflight} request(s) executing and "
                   f"{queued} queued")
    return (
        f"BROWSER BUSY: {opening} (limits: {limit} concurrent, {depth} queued). "
        f"Your {what} was NEVER SENT — the browser was not touched and nothing changed, so "
        f"this is safe to re-issue as-is. Wait ~{eta:.0f}s, then retry the SAME call; do not "
        f"fan out more calls, that is what filled the queue. See server_info > "
        f"browser_concurrency for the live depth and the limit."
    )


def concurrency_info(queue: RequestQueue) -> Dict[str, Any]:
    """The `browser_concurrency` block for get_connections_info() / get_health_info().

    Discoverability (criterion (e)) with ZERO tool-description bytes: server_info already
    json.dumps() that payload, so the limit, the knobs and the live depth become visible to an
    agent without spending a single byte of the description budget.
    """
    return {
        "max_concurrent_per_browser": max_concurrent_requests(),
        "max_queued_per_browser": max_queued_requests(),
        "max_queue_wait_seconds": max_queue_wait_seconds(),
        "env_vars": {"concurrency": _MAX_ENV_VAR,
                     "queue_depth": _DEPTH_ENV_VAR,
                     "queue_wait": _WAIT_ENV_VAR},
        "note": ("Several agents sharing one browser queue here, FIFO by arrival. "
                 "Requests beyond the limit WAIT (they are not dropped); beyond the "
                 "queue depth they are refused with BROWSER BUSY and never sent."),
        "browsers": queue.snapshot(),
    }
