"""
Unit tests for the per-browser FIFO admission gate (server/browsers/request_queue.py).

What went wrong without it: nothing in the stack bounded concurrency, so N agents sharing one
browser put N simultaneous requests through one extension on one JS thread. Measured, the
navigate timeout rate went 0.8% -> 39.3% (474 calls returned nothing) and the answer was the
bare "Request timed out after 35.0 seconds" — which names no cause and reads exactly like "the
page did not load", so agents retried and raised timeout_ms, which made it worse.

These tests pin the four things the fix has to be true about:
  (a) contention QUEUES, it does not fail — and the caller's timeout covers EXECUTION only, so
      time spent waiting a turn is accounted separately instead of eating the budget;
  (b) a timeout under contention SAYS SO, distinctly from the solo string, which stays byte
      identical (that byte-identity is the regression fence for every existing test);
  (c) past the queue depth, back-pressure is RETURNED (BROWSER BUSY, nothing sent) rather than
      absorbed as another unattributable timeout;
  (d) the single-agent path does not regress: acquiring an idle gate never awaits;
  (e) the live depth and the limit are discoverable in the server_info payload.

The dose-response test is the acceptance table: drive N = 1, 2, 5, 10 through
the gate against a fake single-JS-thread extension and demand a FLAT failure rate with RISING
latency. `test_dose_response_bites_without_the_gate` is its bite drill — remove the gate and
the same assertions must fail, or the table proves nothing.
"""

import asyncio
import json
import logging
import math
import time
from dataclasses import dataclass
from typing import Optional

import pytest

import server.browsers.request_queue as request_queue
from server.browsers.request_queue import (BrowserGate, QueueRejected, RequestQueue, Ticket,
                                           concurrency_info, max_concurrent_requests,
                                           max_queue_wait_seconds, max_queued_requests,
                                           overflow_message, timeout_message)


async def settle(turns=4):
    """Let every already-scheduled task run its next step. Ordering, not timing: the tests
    assert who was admitted in what order, never how many milliseconds it took."""
    for _ in range(turns):
        await asyncio.sleep(0)


# --- the model of a single-JS-thread extension --------------------------------------------

class FakeExtension:
    """Answers each request after `latency`, but never services more than `max_parallel` at
    once — everything past that waits INSIDE the extension, which is precisely the resource
    the gate exists to protect. `observed_peak` is how many it was ever asked to hold at once.

    This is what makes the dose-response deterministic: it reproduces the corpus condition (an
    extension that cannot absorb N at a time) without needing a browser.
    """

    def __init__(self, latency=0.04, max_parallel=2):
        self.latency = latency
        self.max_parallel = max_parallel
        self._sem = asyncio.Semaphore(max_parallel)
        self.in_service = 0
        self.observed_peak = 0
        self.served = 0

    async def handle(self):
        async with self._sem:
            self.in_service += 1
            self.observed_peak = max(self.observed_peak, self.in_service)
            try:
                await asyncio.sleep(self.latency)
                self.served += 1
                return {"type": "response"}
            finally:
                self.in_service -= 1


@dataclass
class Outcome:
    wall: float
    ticket: Optional[Ticket] = None
    error: Optional[str] = None
    timed_out: bool = False
    rejected: bool = False


async def drive(gate, ext, action="navigation.navigate", timeout=1.0):
    """The exact shape server.send_request_and_wait takes with the gate hooked in: acquire
    OUTSIDE the caller's budget, execute INSIDE it, release in a `finally` that covers every
    exit (a missed release leaks a slot permanently — the gate degrades monotonically)."""
    started = time.monotonic()
    ticket = None
    try:
        ticket = await gate.acquire(action)
    except QueueRejected as rejected:
        return Outcome(wall=time.monotonic() - started, error=rejected.message, rejected=True)
    try:
        await asyncio.wait_for(ext.handle(), timeout=timeout)
        return Outcome(wall=time.monotonic() - started, ticket=ticket)
    except asyncio.TimeoutError:
        return Outcome(wall=time.monotonic() - started, ticket=ticket,
                       error=timeout_message(ticket, timeout), timed_out=True)
    finally:
        gate.release(ticket)


# --- (d) the single-agent path ------------------------------------------------------------

@pytest.mark.asyncio
async def test_idle_acquire_never_awaits():
    """Criterion (d), machine-checkable. An idle gate must admit without yielding: the first
    step of the coroutine has to run to completion, so no extra event-loop turn is introduced
    and nothing downstream of a solo call can be reordered by the gate's presence."""
    gate = BrowserGate("ff-solo")
    coro = gate.acquire("navigation.navigate")
    with pytest.raises(StopIteration) as stop:
        coro.send(None)  # a single step; anything awaited here would yield instead
    ticket = stop.value.value
    assert isinstance(ticket, Ticket)
    assert ticket.queue_wait == 0.0, "the fast path must pin the wait at exactly 0.0"
    assert ticket.inflight_on_arrival == 0 and ticket.queued_on_arrival == 0
    assert gate.inflight == 1


@pytest.mark.asyncio
async def test_solo_request_reports_no_contention():
    """The other half of (d): a lone request's ticket must never look contended, or every
    solo timeout would start claiming a crowd that was not there."""
    gate = BrowserGate("ff-solo")
    ticket = await gate.acquire("navigation.navigate")
    assert ticket.peak_inflight == 1
    assert timeout_message(ticket, 35.0) == "Request timed out after 35.0 seconds"
    gate.release(ticket)
    assert gate.inflight == 0


# --- fairness -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_admission_is_strict_arrival_order():
    """FIFO, and no starvation: later arrivals queue BEHIND an earlier waiter, never past it.
    Asserted on the admission order, never on timing."""
    gate = BrowserGate("ff-x", limit=1)
    admitted = []

    async def waiter(name):
        ticket = await gate.acquire(f"tabs.{name}")
        admitted.append(name)
        return ticket

    holder = await gate.acquire("tabs.list")
    tasks = {}
    for name in ("b", "c", "d"):
        tasks[name] = asyncio.create_task(waiter(name))
        await settle()
    assert gate.queued == 3 and admitted == []

    # Two MORE arrivals after b has been waiting: they must not be able to overtake it.
    for name in ("e", "f"):
        tasks[name] = asyncio.create_task(waiter(name))
        await settle()
    assert gate.queued == 5

    gate.release(holder)
    await settle()
    assert admitted == ["b"], "the left-most waiter is admitted, not the newest arrival"

    for _ in range(4):
        gate.release(await tasks[admitted[-1]])
        await settle()
    assert admitted == ["b", "c", "d", "e", "f"]
    gate.release(await tasks["f"])
    assert gate.inflight == 0 and gate.queued == 0


@pytest.mark.asyncio
async def test_a_late_arrival_cannot_take_a_free_slot_past_a_waiter(monkeypatch):
    """The `not self._waiters` guard on the fast path. Without it a newcomer would take any
    slot that happened to be free, past a request that had been queued for seconds — FIFO
    would then hold only while the gate was saturated, i.e. never when it matters."""
    monkeypatch.setenv("BROWSERPLUG_MAX_CONCURRENT_REQUESTS", "1")
    gate = BrowserGate("ff-x")
    admitted = []

    async def waiter(name):
        ticket = await gate.acquire(f"tabs.{name}")
        admitted.append(name)
        return ticket

    holder = await gate.acquire("tabs.list")
    early = asyncio.create_task(waiter("early"))
    await settle()
    assert gate.queued == 1

    # A slot opens without anything being released (the operator widened the limit) while
    # `early` is still queued. The newcomer must queue BEHIND it, not walk into the gap.
    monkeypatch.setenv("BROWSERPLUG_MAX_CONCURRENT_REQUESTS", "2")
    late = asyncio.create_task(waiter("late"))
    await settle()
    assert gate.queued == 2, "the newcomer took a free slot past a waiter"
    assert admitted == []

    gate.release(holder)
    await settle()
    assert admitted == ["early", "late"], "both fit now, but in arrival order"
    gate.release(await asyncio.wait_for(early, 1.0))
    gate.release(await asyncio.wait_for(late, 1.0))
    assert gate.inflight == 0


# --- (a) queued, not failed; and the budget is for execution -------------------------------

@pytest.mark.asyncio
async def test_limit_is_enforced_and_the_excess_is_queued_not_dropped():
    """The core of (a): over-limit requests WAIT. Sampled from inside each holder, so the
    limit is observed at the moment it matters, not inferred afterwards."""
    gate = BrowserGate("ff-x", limit=3)
    ext = FakeExtension(latency=0.02, max_parallel=10)
    outcomes = await asyncio.gather(*[drive(gate, ext, timeout=2.0) for _ in range(10)])

    assert [o for o in outcomes if o.error] == [], "nothing may fail merely for being Nth"
    assert ext.served == 10
    assert ext.observed_peak <= 3, "the gate must bound what the extension is asked to hold"
    queued = [o for o in outcomes if o.ticket.queue_wait > 0.0]
    assert len(queued) == 7, "10 requests at limit 3 means 3 admitted at once and 7 queued"
    assert gate.inflight == 0 and gate.queued == 0


@pytest.mark.asyncio
async def test_queue_wait_does_not_consume_the_execution_budget():
    """Criterion (a)'s sharp edge. A request that queued for LONGER than its own timeout must
    still succeed, because the budget starts when it is admitted. If the wait were inside the
    budget, the gate would have converted contention into exactly the timeouts it exists to
    remove."""
    gate = BrowserGate("ff-x", limit=1)
    ext = FakeExtension(latency=0.01, max_parallel=1)
    blocker = await gate.acquire("navigation.navigate")

    budget = 0.12
    call = asyncio.create_task(drive(gate, ext, timeout=budget))
    await settle()
    assert gate.queued == 1
    await asyncio.sleep(0.25)  # it sits in the queue for twice its own budget
    gate.release(blocker)

    outcome = await asyncio.wait_for(call, 2.0)
    assert outcome.error is None, "the caller's timeout must cover EXECUTION, not the wait"
    assert outcome.wall > budget, "and it really did outlive its own budget in wall time"
    assert outcome.ticket.queue_wait > budget
    assert outcome.ticket.admitted_at is not None


@pytest.mark.asyncio
async def test_peak_inflight_records_what_each_request_competed_with():
    """The number the contended timeout message names. It is per-ticket because an agent asks
    "what was happening while MY call ran", not "how busy was the browser at some point"."""
    gate = BrowserGate("ff-x", limit=3)
    tickets = [await gate.acquire("navigation.navigate") for _ in range(3)]
    assert [t.peak_inflight for t in tickets] == [3, 3, 3]

    gate.release(tickets[0])
    gate.release(tickets[1])
    gate.release(tickets[2])
    solo = await gate.acquire("tabs.list")
    assert solo.peak_inflight == 1, "a later, uncontended request must not inherit the peak"
    gate.release(solo)


# --- (c) back-pressure is expressed, not absorbed -------------------------------------------

@pytest.mark.asyncio
async def test_overflow_is_refused_immediately_and_nothing_is_sent(monkeypatch):
    """Criterion (c). Past the depth the caller is REFUSED — and refused without awaiting, so
    the answer is instant and the browser is provably untouched."""
    monkeypatch.setenv("BROWSERPLUG_MAX_QUEUE_DEPTH", "2")
    gate = BrowserGate("ff-x", limit=1)
    holder = await gate.acquire("navigation.navigate")
    waiters = [asyncio.create_task(gate.acquire("tabs.list")) for _ in range(2)]
    await settle()
    assert gate.queued == 2

    coro = gate.acquire("content.read")
    with pytest.raises(QueueRejected) as raised:
        coro.send(None)  # one step: the refusal must not even reach an await
    rejected = raised.value
    assert rejected.reason == "depth"
    message = rejected.message
    assert "BROWSER BUSY" in message
    assert "1 request(s) executing and 2 queued" in message
    assert "limits: 1 concurrent, 2 queued" in message
    assert "NEVER SENT" in message and "safe to re-issue as-is" in message
    assert "do not fan out more calls" in message
    assert "server_info > browser_concurrency" in message
    assert rejected.stats["rejected_busy"] == 1

    gate.release(holder)
    await settle()
    for task in waiters:
        gate.release(await asyncio.wait_for(task, 1.0))
        await settle()
    assert gate.inflight == 0


@pytest.mark.asyncio
async def test_queue_wait_expiry_says_wait_not_depth(monkeypatch):
    """A request that joined the queue but never reached the front gets a DIFFERENT first
    clause from one the queue was too full to accept — the two need different next actions
    (wait and retry vs. stop fanning out), so they must not read alike."""
    monkeypatch.setenv("BROWSERPLUG_MAX_QUEUE_WAIT", "0.05")
    gate = BrowserGate("ff-x", limit=1)
    holder = await gate.acquire("navigation.navigate")

    with pytest.raises(QueueRejected) as raised:
        await gate.acquire("navigation.navigate")
    rejected = raised.value
    assert rejected.reason == "wait"
    assert "BROWSER BUSY" in rejected.message
    assert "waited 0s in this browser's request queue" in rejected.message
    assert "without reaching the front" in rejected.message
    assert "NEVER SENT" in rejected.message
    assert "already has" not in rejected.message, "that is the depth wording, not this one"

    assert gate.queued == 0, "an expired waiter must leave the queue behind it"
    gate.release(holder)
    assert gate.inflight == 0, "and must not have taken a slot with it"


def test_overflow_eta_falls_back_before_any_sample_and_then_uses_the_mean():
    """The retry-in ETA is a guess until the gate has timed something; it must be a stated
    guess, not a fabricated precision, and it must move once there is evidence."""
    gate = BrowserGate("ff-x", limit=2)
    gate._waiters.extend([object(), object(), object(), object()])  # 4 queued
    assert gate.mean_execution() is None
    assert "Wait ~10s" in overflow_message(gate, "navigate", "depth")  # 4/2 * 5s fallback

    gate._executions.extend([1.0, 1.0])
    assert "Wait ~2s" in overflow_message(gate, "navigate", "depth")  # 4/2 * 1s measured
    gate._waiters.clear()


# --- (b) the timeout says what happened ----------------------------------------------------

def test_timeout_message_is_byte_identical_without_contention():
    """THE REGRESSION FENCE. Every existing test (and every honest solo diagnosis) asserts
    this exact string; the contended wording only means something because it is not what a
    lone caller sees. `None` is the internal retry re-send, which holds its parent's ticket."""
    solo = Ticket(browser_key="ff-x", peak_inflight=1, limit=3)
    assert timeout_message(solo, 35.0) == "Request timed out after 35.0 seconds"
    assert timeout_message(None, 35.0) == "Request timed out after 35.0 seconds"
    assert timeout_message(solo, 3.0) == "Request timed out after 3.0 seconds"
    assert timeout_message(Ticket(browser_key="ff-x"), 30) == "Request timed out after 30 seconds"


def test_timeout_message_names_the_contention():
    """Criterion (b): how many others were running, how long this one queued, and — because
    the corpus shows agents doing both — that retrying and raising timeout_ms make it worse."""
    ticket = Ticket(browser_key="ff-x", peak_inflight=4, queue_wait=2.5, limit=3)
    message = timeout_message(ticket, 35.0)
    assert message.startswith("Request timed out after 35.0 seconds — ")
    for fragment in (
            "the browser bridge was CONTENDED",
            "3 other request(s) were executing against this browser while this one ran",
            "it waited 2.5s in the queue before it started",
            "(concurrency limit 3)",
            "This is NOT evidence that the page failed to load",
            "Do NOT retry immediately and do NOT raise timeout_ms",
            "Reduce the number of agents/threads driving this browser, or serialize them",
            "server_info > browser_concurrency"):
        assert fragment in message, f"missing from the contended timeout: {fragment!r}"


def test_timeout_message_omits_a_queue_wait_it_cannot_honestly_report():
    """Contention with a negligible wait still says CONTENDED, but must not report "0.0s in
    the queue" — a slot handed over in the same tick is scheduler noise, not something an
    agent should act on."""
    ticket = Ticket(browser_key="ff-x", peak_inflight=3, queue_wait=0.004, limit=3)
    message = timeout_message(ticket, 20.0)
    assert "CONTENDED" in message and "2 other request(s)" in message
    assert "in the queue before it started" not in message


@pytest.mark.asyncio
async def test_contention_timeout_is_counted_for_the_right_browser():
    queue = RequestQueue()
    gate = queue.gate_for("ff-x")
    queue.gate_for("chrome-y")
    ticket = await gate.acquire("navigation.navigate")
    queue.note_contention_timeout(ticket)
    queue.note_contention_timeout(None)  # the no-ticket path must be a silent no-op
    snapshot = queue.snapshot()
    assert snapshot["ff-x"]["contention_timeouts"] == 1
    assert snapshot["chrome-y"]["contention_timeouts"] == 0
    gate.release(ticket)


# --- the measured dose-response ------------------------------------------------------------

_LIMIT = 2
# Single-digit-ms sleeps are unusable as a latency yardstick on Windows: the loop's clock
# resolution is ~15.6ms, so a sleep both overshoots (measured: +11ms) AND can fire a tick
# EARLY (measured: asyncio.sleep(0.04) returning in 31ms — asyncio wakes a timer that is
# within one clock resolution of due). 50ms keeps the batch structure legible through that
# while keeping the whole table well under a second.
_LATENCY = 0.05
_CLOCK_SLOP = 0.016  # one clock resolution: the most a batch can come back early


def _batch_of_p50(n):
    """Which service batch the p50 request lands in, at `_LIMIT` concurrent."""
    return (n // 2) // _LIMIT + 1


def _p50(values):
    return sorted(values)[len(values) // 2]


@pytest.mark.asyncio
async def test_dose_response_queues_instead_of_dropping(capsys):
    """The acceptance table. Drive N = 1, 2, 5, 10 concurrent requests at one browser and
    demand the required shape: a FLAT failure rate (zero, at every N — nothing may
    fail just for being Nth) with RISING latency. Rising latency is the positive evidence that
    queuing, not dropping, absorbed the load; without it, "no failures" could just mean the
    load never arrived."""
    table = {}
    for n in (1, 2, 5, 10):
        gate = BrowserGate("ff-x", limit=_LIMIT)
        ext = FakeExtension(latency=_LATENCY, max_parallel=_LIMIT)
        outcomes = await asyncio.gather(
            *[drive(gate, ext, timeout=2.0) for _ in range(n)])
        table[n] = {
            "calls": n,
            "timeouts": sum(1 for o in outcomes if o.timed_out),
            "refusals": sum(1 for o in outcomes if o.rejected),
            "queued": sum(1 for o in outcomes if o.ticket and o.ticket.queue_wait > 0.0),
            "p50": _p50([o.wall for o in outcomes]),
            "p95": sorted(o.wall for o in outcomes)[max(0, math.ceil(n * 0.95) - 1)],
            "peak": ext.observed_peak,
        }
        assert ext.served == n, f"N={n}: every request must actually reach the extension"
        assert gate.inflight == 0 and gate.queued == 0, f"N={n}: the gate leaked a slot"

    print("\n  n | calls | timeouts |  rate | queued |   p50 |   p95 | peak_inflight")
    for n, row in table.items():
        print(f" {n:2d} | {row['calls']:5d} | {row['timeouts']:8d} | "
              f"{row['timeouts'] / row['calls']:5.1%} | {row['queued']:6d} | "
              f"{row['p50'] * 1000:5.0f} | {row['p95'] * 1000:5.0f} | {row['peak']:d}")

    # Flat failure rate: ZERO, not "low". A request refused or timed out purely for arriving
    # Nth is the collapse the gate exists to remove.
    assert [table[n]["timeouts"] for n in table] == [0, 0, 0, 0]
    assert [table[n]["refusals"] for n in table] == [0, 0, 0, 0]

    # The excess QUEUED — deterministic counts, so this half of the claim cannot flake.
    assert [table[n]["queued"] for n in table] == [0, 0, 3, 8]
    # ...and the gate really did gate: the extension was never asked to hold more than limit.
    assert all(table[n]["peak"] <= _LIMIT for n in table)

    # Rising latency. Each N's p50 lands in a known service batch, so its floor is that many
    # batches of extension work — minus one clock resolution per batch, which is the most the
    # platform's timers can hand back early.
    for n in table:
        batches = _batch_of_p50(n)
        assert table[n]["p50"] >= batches * (_LATENCY - _CLOCK_SLOP), \
            f"N={n}: p50 below the {batches}-batch floor — the load did not actually queue"
    assert table[10]["p50"] > table[5]["p50"] > table[2]["p50"], \
        "latency must RISE with concurrency; flat latency would mean work was shed"
    # N=1 vs N=2 are both a single batch (the queued counts above already prove nothing
    # queued there), so comparing them any more finely than "not dramatically faster" would
    # be measuring this box's ~15ms timer granularity rather than the gate.
    assert table[2]["p50"] >= table[1]["p50"] - _LATENCY, \
        "a second concurrent request must not come back sooner than a solo one"


@pytest.mark.asyncio
async def test_dose_response_bites_without_the_gate(monkeypatch):
    """Bite drill. Effectively remove the gate (limit 1000) and drive the SAME fan-out at the
    same extension: the timeouts come back. Without this, the table above would pass on a
    build that had no admission control at all — which is precisely the state that produced
    39.3%."""
    monkeypatch.setenv("BROWSERPLUG_MAX_CONCURRENT_REQUESTS", "1000")
    gate = BrowserGate("ff-x")  # env-driven limit, i.e. no meaningful gate
    assert gate.limit == 1000
    ext = FakeExtension(latency=0.05, max_parallel=2)

    outcomes = await asyncio.gather(*[drive(gate, ext, timeout=0.15) for _ in range(10)])
    timeouts = [o for o in outcomes if o.timed_out]
    assert timeouts, "an ungated fan-out MUST still time out — otherwise the table proves nothing"
    assert ext.observed_peak <= 2  # the extension, not the gate, was the thing that bounded it
    # And those timeouts are still named honestly: they were contended, and the message says so.
    assert all("CONTENDED" in o.error for o in timeouts)


# --- env knobs ------------------------------------------------------------------------------

def test_env_knobs_are_read_at_call_time(monkeypatch):
    """Read at CALL time, like request_retry.reconnect_grace_seconds: an operator changing the
    knob must not have to restart, and a test must be able to monkeypatch either the env var or
    the module attribute."""
    assert max_concurrent_requests() == request_queue.MAX_CONCURRENT_DEFAULT == 3
    assert max_queued_requests() == request_queue.MAX_QUEUED_DEFAULT == 24
    assert max_queue_wait_seconds() == request_queue.MAX_QUEUE_WAIT_DEFAULT == 60.0

    monkeypatch.setenv("BROWSERPLUG_MAX_CONCURRENT_REQUESTS", "7")
    monkeypatch.setenv("BROWSERPLUG_MAX_QUEUE_DEPTH", "50")
    monkeypatch.setenv("BROWSERPLUG_MAX_QUEUE_WAIT", "1.5")
    assert (max_concurrent_requests(), max_queued_requests(), max_queue_wait_seconds()) \
        == (7, 50, 1.5)

    monkeypatch.delenv("BROWSERPLUG_MAX_CONCURRENT_REQUESTS")
    monkeypatch.setattr(request_queue, "MAX_CONCURRENT_DEFAULT", 5)
    assert max_concurrent_requests() == 5, "the module attribute is patchable too"


def test_unusable_env_values_fall_back_with_a_warning(monkeypatch, caplog):
    """A mistyped knob must degrade to the default, loudly — never brick the bridge with a
    limit of 0 and never raise from inside the admission path."""
    monkeypatch.setenv("BROWSERPLUG_MAX_CONCURRENT_REQUESTS", "banana")
    monkeypatch.setenv("BROWSERPLUG_MAX_QUEUE_DEPTH", "0")
    monkeypatch.setenv("BROWSERPLUG_MAX_QUEUE_WAIT", "soon")
    with caplog.at_level(logging.WARNING, logger="server.browsers.request_queue"):
        assert max_concurrent_requests() == 3
        assert max_queued_requests() == 24
        assert max_queue_wait_seconds() == 60.0
    text = caplog.text
    assert "banana" in text and "soon" in text
    assert "out-of-range" in text and "minimum 1" in text


@pytest.mark.asyncio
async def test_a_gate_follows_the_env_limit_without_a_restart(monkeypatch):
    """The limit is consulted per admission, so tightening the knob takes effect on the next
    request rather than on the next server start."""
    gate = BrowserGate("ff-x")
    monkeypatch.setenv("BROWSERPLUG_MAX_CONCURRENT_REQUESTS", "1")
    first = await gate.acquire("tabs.list")
    blocked = asyncio.create_task(gate.acquire("tabs.list"))
    await settle()
    assert gate.queued == 1, "the tightened limit applied immediately"

    monkeypatch.setenv("BROWSERPLUG_MAX_CONCURRENT_REQUESTS", "4")
    gate.release(first)  # the release re-reads the limit and drains what now fits
    await settle()
    gate.release(await asyncio.wait_for(blocked, 1.0))
    assert gate.inflight == 0


# --- leak guards ----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_release_is_idempotent():
    """A double release that decremented twice would over-admit forever, and every caller
    wraps release in a `finally` that also runs on paths where admission never happened."""
    gate = BrowserGate("ff-x", limit=1)
    ticket = await gate.acquire("tabs.list")
    queued = asyncio.create_task(gate.acquire("tabs.list"))
    await settle()

    gate.release(ticket)
    gate.release(ticket)  # again
    gate.release(None)    # and the never-admitted path
    await settle()
    assert gate.inflight == 1, "exactly the one waiter got in, not two"

    second = await asyncio.wait_for(queued, 1.0)
    gate.release(second)
    assert gate.inflight == 0 and gate.queued == 0


@pytest.mark.asyncio
async def test_a_cancelled_waiter_leaves_no_slot_behind():
    """A caller can vanish while queued (the MCP client went away, the task was cancelled).
    Its slot must go to the next waiter; leak `limit` of them and the browser is bricked until
    a restart, with no error anywhere to say why."""
    gate = BrowserGate("ff-x", limit=1)
    holder = await gate.acquire("navigation.navigate")
    doomed = asyncio.create_task(gate.acquire("tabs.list"))
    survivor = asyncio.create_task(gate.acquire("tabs.list"))
    await settle()
    assert gate.queued == 2

    doomed.cancel()
    await settle()
    with pytest.raises(asyncio.CancelledError):
        await doomed
    assert gate.queued == 1

    gate.release(holder)
    await settle()
    ticket = await asyncio.wait_for(survivor, 1.0)
    assert gate.inflight == 1
    gate.release(ticket)
    assert gate.inflight == 0


@pytest.mark.asyncio
async def test_drop_browser_reclaims_the_gate_without_stranding_its_callers():
    """A departed browser's in-flight callers are failed by ExtensionRpc.fail_pending_requests, but each
    still runs its own `finally: release(...)` against the gate object it captured — so
    dropping the registry entry must be safe while those are still unwinding."""
    queue = RequestQueue()
    gate = queue.gate_for("ff-x")
    ticket = await gate.acquire("navigation.navigate")
    assert "ff-x" in queue.snapshot()

    queue.drop_browser("ff-x")
    assert "ff-x" not in queue.snapshot()

    gate.release(ticket)  # the straggler unwinds against the dropped gate
    assert gate.inflight == 0
    assert queue.gate_for("ff-x") is not gate, "a returning browser starts with fresh stats"


@pytest.mark.asyncio
async def test_gates_are_per_browser():
    """Two browsers are two independent bridges: a saturated Chrome must not throttle an idle
    Firefox."""
    queue = RequestQueue()
    firefox, chrome = queue.gate_for("ff-x"), queue.gate_for("cr-y")
    assert firefox is not chrome
    assert queue.gate_for("ff-x") is firefox

    busy = [await chrome.acquire("navigation.navigate") for _ in range(chrome.limit)]
    coro = firefox.acquire("navigation.navigate")
    with pytest.raises(StopIteration):
        coro.send(None)  # Firefox still takes the fast path
    assert firefox.inflight == 1 and chrome.inflight == len(busy)


# --- (e) discoverability ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_concurrency_info_exposes_the_limit_and_the_live_depth():
    """Criterion (e). server_info json.dumps() this block, so the limit, the knobs and the live
    depth become discoverable without spending a byte of the tool-description budget."""
    queue = RequestQueue()
    gate = queue.gate_for("ff-x")
    held = [await gate.acquire("navigation.navigate") for _ in range(gate.limit)]
    waiting = asyncio.create_task(gate.acquire("tabs.list"))
    await settle()

    info = concurrency_info(queue)
    assert info["max_concurrent_per_browser"] == 3
    assert info["max_queued_per_browser"] == 24
    assert info["env_vars"] == {"concurrency": "BROWSERPLUG_MAX_CONCURRENT_REQUESTS",
                                "queue_depth": "BROWSERPLUG_MAX_QUEUE_DEPTH",
                                "queue_wait": "BROWSERPLUG_MAX_QUEUE_WAIT"}
    assert "FIFO by arrival" in info["note"] and "BROWSER BUSY" in info["note"]

    row = info["browsers"]["ff-x"]
    assert row["inflight"] == 3 and row["queued"] == 1
    assert row["limit"] == 3 and row["peak_inflight_ever"] == 3
    assert row["admitted"] == 3 and row["rejected_busy"] == 0
    assert row["mean_execution_ms"] is None, "no completed request yet — say so, don't guess"

    # It has to survive the exact rendering server_info uses.
    assert json.loads(json.dumps(info, indent=1, default=str))["browsers"]["ff-x"]["queued"] == 1

    for ticket in held:
        gate.release(ticket)
        await settle()
    gate.release(await asyncio.wait_for(waiting, 1.0))
    after = concurrency_info(queue)["browsers"]["ff-x"]
    assert after["inflight"] == 0 and after["queued"] == 0
    assert after["admitted"] == 4 and after["queue_wait_max_ms"] >= 0
    assert after["mean_execution_ms"] is not None
