"""
Unit tests for the restart sequence (server/runtime/graceful_restart.py).

What restart used to be: ack, spawn the replacement, os._exit(0) 0.3 s later. Anything
in flight died mid-air — a tool call became a bare timeout at the agent 20 s later, and
a browser socket was dropped with no close frame, so the extension found out from a
transport error rather than from the server.

The order of the six steps is the whole design, so that is what these tests pin:

  (a) in-flight work is DRAINED before anything is torn down, bounded, and whatever
      outlives the deadline is failed with a message that says retrying is safe;
  (b) the replacement is spawned AFTER the drain and BEFORE the first teardown step —
      so a failed spawn aborts the restart with the server still whole and serving,
      which is the property the old code had and must not lose;
  (c) sockets get a real 1001 close frame with a reason, and the handshake is awaited
      but bounded (a peer that never answers cannot hold the restart open);
  (d) the drain budget is CLAMPED, because a teardown longer than the replacement's
      25 s wait for the instance lock ends with the replacement exiting as a duplicate
      and no restart happening at all;
  (e) exit happens even when a teardown step fails — the ports must be freed for the
      replacement that is already waiting on them.
"""

import asyncio
import threading

import pytest

import server.runtime.graceful_restart as gr
from server.runtime.graceful_restart import (DEFAULT_DRAIN_SECONDS, DRAIN_MAX_SECONDS,
                                             DRAIN_SECONDS_ENV, PENDING_REASON,
                                             WS_CLOSE_GOING_AWAY, RestartHooks, close_connections,
                                             drain_in_flight, drain_seconds, perform_restart)


class FakeClock:
    """A clock that only moves when the code under test sleeps. Every drain assertion is
    about the deadline being honored, never about real elapsed milliseconds."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.now += seconds
        await asyncio.sleep(0)


class FakeSocket:
    """An extension socket that records how it was closed."""

    def __init__(self, hang=False):
        self.closed_with = None
        self.hang = hang

    async def close(self, code=None, reason=None):
        self.closed_with = (code, reason)
        if self.hang:
            await asyncio.Event().wait()   # a peer that never answers the handshake


class LegacySocket:
    """close() takes no arguments (an older websockets, or a hand-rolled double)."""

    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


def make_hooks(log, *, in_flight=None, connections=(), spawn_error=None):
    """Hooks that append their own name to `log`, so a test can assert the ORDER."""
    counts = list(in_flight or [0])

    def count():
        return counts.pop(0) if len(counts) > 1 else counts[0]

    def spawn():
        log.append("spawn")
        if spawn_error is not None:
            raise spawn_error

    return RestartHooks(
        count_in_flight=count,
        connections=lambda: list(connections),
        spawn_replacement=spawn,
        close_registry=lambda reason: log.append(f"close_registry:{reason[:12]}"),
        stop_http=lambda: log.append("stop_http"),
        exit_process=lambda: log.append("exit"),
        arm=lambda: log.append("arm"),
        disarm=lambda: log.append("disarm"),
    )


# --- (a) the drain ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_drain_returns_at_once_when_nothing_is_in_flight():
    """The common case: no work, no delay. A restart of an idle server must not pay for
    the drain it doesn't need."""
    clock = FakeClock()
    report = await drain_in_flight(lambda: 0, timeout=5.0, sleep=clock.sleep, clock=clock)
    assert report.drained is True
    assert report.remaining == 0
    assert report.waited == 0.0
    assert clock.now == 0.0, "an idle drain must not advance the clock at all"


@pytest.mark.asyncio
async def test_drain_waits_for_in_flight_work_to_settle():
    """Two requests finish while the drain polls; it returns as soon as they do, not
    when the budget runs out."""
    clock = FakeClock()
    remaining = [2, 2, 1, 0]

    def count():
        return remaining.pop(0) if remaining else 0

    report = await drain_in_flight(count, timeout=5.0, poll=0.05,
                                   sleep=clock.sleep, clock=clock)
    assert report.drained is True
    assert report.remaining == 0
    assert clock.now < 5.0, "the drain must end on the work, not on the deadline"


@pytest.mark.asyncio
async def test_drain_gives_up_at_the_deadline_and_says_what_is_left():
    """A request that never settles cannot hold the restart open. The count that
    outlived the budget is REPORTED — that is what turns into the retryable failure."""
    clock = FakeClock()
    report = await drain_in_flight(lambda: 3, timeout=1.0, poll=0.05,
                                   sleep=clock.sleep, clock=clock)
    assert report.drained is False
    assert report.remaining == 3
    assert report.waited >= 1.0


# --- (d) the clamped budget ---------------------------------------------------------

def test_drain_budget_defaults_and_clamps():
    assert drain_seconds({}) == DEFAULT_DRAIN_SECONDS
    assert drain_seconds({DRAIN_SECONDS_ENV: ""}) == DEFAULT_DRAIN_SECONDS
    assert drain_seconds({DRAIN_SECONDS_ENV: "2.5"}) == 2.5
    assert drain_seconds({DRAIN_SECONDS_ENV: "0"}) == 0.0
    # Garbage and negatives read as the default. This resolves on the exit path of a
    # server that was ASKED to restart; a typo must not be what keeps it alive.
    assert drain_seconds({DRAIN_SECONDS_ENV: "soon"}) == DEFAULT_DRAIN_SECONDS
    assert drain_seconds({DRAIN_SECONDS_ENV: "-4"}) == DEFAULT_DRAIN_SECONDS
    # The clamp exists so the teardown always fits inside the replacement's 25s wait for
    # the instance lock. Without it, a long drain ends in NO restart: the replacement
    # gives up, finds our still-healthy /health and exits 0 as a duplicate.
    assert drain_seconds({DRAIN_SECONDS_ENV: "600"}) == DRAIN_MAX_SECONDS


# --- (c) closing the sockets --------------------------------------------------------

@pytest.mark.asyncio
async def test_sockets_are_closed_with_a_going_away_code_and_a_reason():
    socks = [FakeSocket(), FakeSocket()]
    closed = await close_connections(socks)
    assert closed == 2
    for s in socks:
        code, reason = s.closed_with
        assert code == WS_CLOSE_GOING_AWAY, "1000 would claim the connection was finished with"
        assert "restart" in reason.lower()
        assert len(reason.encode()) <= 123, "a close reason over 123 bytes makes websockets raise"


@pytest.mark.asyncio
async def test_a_socket_whose_close_takes_no_arguments_still_closes():
    sock = LegacySocket()
    assert await close_connections([sock]) == 1
    assert sock.closed is True


@pytest.mark.asyncio
async def test_a_hanging_close_handshake_cannot_hold_the_restart_open():
    """The process exit drops the socket anyway — which is exactly what every close did
    before this module existed. The bound is what keeps that the worst case."""
    hung = FakeSocket(hang=True)
    closed = await close_connections([hung], timeout=0.05)
    assert closed == 0


@pytest.mark.asyncio
async def test_no_connections_is_not_an_error():
    assert await close_connections([]) == 0
    assert await close_connections([None]) == 0


# --- (b) the order, and the abort it protects ---------------------------------------

@pytest.mark.asyncio
async def test_the_sequence_drains_then_spawns_then_tears_down():
    log = []
    sock = FakeSocket()
    hooks = make_hooks(log, in_flight=[0], connections=[sock])
    report = await perform_restart(hooks, trigger="test", ack_flush=0, drain_timeout=0.1)

    assert [step for step in log if step != "arm"] == [
        "spawn", f"close_registry:{PENDING_REASON[:12]}", "stop_http", "exit",
    ]
    assert log.index("arm") < log.index("spawn"), (
        "the death guard must be armed before the replacement binds, or our own shutdown "
        "reads as an MCP crash and exits(1)")
    assert sock.closed_with is not None, "sockets close before the process leaves"
    assert report.aborted is False
    assert report.closed == 1


@pytest.mark.asyncio
async def test_a_failed_spawn_aborts_the_restart_with_everything_still_connected():
    """The one property the old hard-exit path got right, and the reason the spawn sits
    between the drain and the teardown: if there will be no replacement, this server has
    to still be a whole one."""
    log = []
    sock = FakeSocket()
    hooks = make_hooks(log, connections=[sock], spawn_error=OSError("no such file"))
    report = await perform_restart(hooks, trigger="test", ack_flush=0, drain_timeout=0.1)

    assert report.aborted is True
    assert "no such file" in report.error
    assert log == ["arm", "spawn", "disarm"], f"nothing may be torn down: {log}"
    assert sock.closed_with is None, "an aborted restart must not close a browser socket"
    assert "exit" not in log


@pytest.mark.asyncio
async def test_work_that_outlives_the_drain_is_failed_as_retryable():
    """Whatever the deadline catches still gets an answer. Whether a retry is safe depends
    on the action (a click may already have run), so the reason promises nothing; the RPC
    layer adds the per-action sentence."""
    log = []
    reasons = []
    hooks = make_hooks(log, in_flight=[2])
    hooks.close_registry = reasons.append
    clock = FakeClock()
    report = await perform_restart(hooks, trigger="test", ack_flush=0, drain_timeout=0.2,
                                   sleep=clock.sleep, clock=clock)

    assert report.drain.drained is False and report.drain.remaining == 2
    assert reasons == [PENDING_REASON]
    assert "safe" not in PENDING_REASON.lower()
    assert "exit" in log, "an undrainable request must not stop the restart"


# --- (e) exit is not optional -------------------------------------------------------

@pytest.mark.asyncio
async def test_a_teardown_failure_still_frees_the_ports():
    """The replacement is already waiting on :8772. A stop_http that throws must not
    strand it — this process is leaving either way."""
    log = []
    hooks = make_hooks(log)

    def boom():
        log.append("stop_http")
        raise RuntimeError("uvicorn is wedged")

    hooks.stop_http = boom
    await perform_restart(hooks, trigger="test", ack_flush=0, drain_timeout=0.1)
    assert log[-1] == "exit"


@pytest.mark.asyncio
async def test_a_blocking_stop_http_is_bounded():
    """stop_http joins the uvicorn thread, and uvicorn's own graceful shutdown waits on
    in-flight responses. It runs off the loop AND on a deadline, so a session that never
    ends costs a few seconds, not the restart."""
    log = []
    hooks = make_hooks(log)

    # An Event stands in for the wedged uvicorn thread: set in `finally`, so the worker
    # thread ends with the test instead of sleeping into the executor's teardown join.
    release = threading.Event()

    def forever():
        release.wait(30)

    hooks.stop_http = forever
    try:
        await perform_restart(hooks, trigger="test", ack_flush=0, drain_timeout=0.1,
                              http_stop_timeout=0.05)
        assert log[-1] == "exit"
    finally:
        release.set()


@pytest.mark.asyncio
async def test_a_restart_is_logged_at_both_ends(caplog):
    """A restart must be findable in the log after the fact. The closing report is
    written by a process about to os._exit, so the opening line is the only evidence
    left if the teardown itself wedges — both are pinned here."""
    log = []
    hooks = make_hooks(log)
    with caplog.at_level("INFO", logger="server.runtime.graceful_restart"):
        await perform_restart(hooks, trigger="POST /restart", ack_flush=0, drain_timeout=0.1)

    messages = [r.getMessage() for r in caplog.records]
    assert any("beginning" in m and "POST /restart" in m for m in messages), messages
    assert any("Graceful restart (POST /restart)" in m for m in messages), messages


def test_the_report_describes_what_happened():
    """One line, logged just before the process goes; it is the only trace a restart
    leaves in the old server's log."""
    report = gr.RestartReport(trigger="POST /restart",
                              drain=gr.DrainReport(drained=False, remaining=2, waited=5.0),
                              closed=3)
    text = report.describe()
    assert "POST /restart" in text and "2 request" in text and "3 socket" in text
