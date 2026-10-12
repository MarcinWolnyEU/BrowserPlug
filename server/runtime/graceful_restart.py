#!/usr/bin/env python3
"""Graceful restart: settle what is in flight, close the sockets, then hand over.

Every restart path (POST /restart, the WS `server.restart` the options page prefers,
and the /ws-bind + /reset-config rebinds) used to do the same three things: ack,
spawn the replacement, os._exit(0) a third of a second later. Whatever was in flight
died with the process. A browser socket was dropped with no close frame, so the
extension learned about the restart from a transport error rather than from the
server; an in-flight tool call reached the agent as a bare timeout, long after the
fact, and nothing in it said a restart was the cause.

The sequence below settles that work first, and its ORDER is the load-bearing part:

  0. mark the server restarting  -- new calls are refused with the restart message and
                                    an in-flight call that fails says "restart", not
                                    "disconnected" (the flag drops again on any abort)
  1. let the ack flush           -- whoever asked is told the restart began
  2. drain in-flight WS requests -- bounded; the agent gets its answer
  3. spawn the replacement       -- the first step that can fail, and nothing is torn
                                    down yet, so a failed spawn still aborts the whole
                                    restart and leaves a fully serving process
  3b. confirm the replacement    -- it must prove it survived its imports and reached the
                                    instance-lock wait (a marker it writes). Spawned is
                                    not started: a syntax error dies in about a second, and
                                    tearing down for a corpse strands every client. A dead
                                    or silent child aborts exactly like a failed spawn.
  4. close the browser sockets   -- 1001 "going away" WITH a reason, handshake awaited
  5. stop the HTTP server        -- uvicorn drains its own in-flight responses
  6. exit                        -- freeing the ports the replacement is waiting on

Step 3 sits between the two halves deliberately. Spawning earlier gives a replacement
that races a server still answering calls; spawning later means discovering the spawn
failed after the sockets are already gone, which is the one outcome the old code was
careful to avoid (it stayed alive on a failed spawn, and so does this).

The whole teardown is bounded well under the 25 s a replacement waits for the instance
lock in acquire_instance_lock_waiting. Overrun it and the replacement gives up, finds
our still-healthy /health, and exits 0 as a redundant duplicate: a restart that quietly
ends in no restart. That is what DRAIN_MAX_SECONDS is for, and why the drain budget is
clamped rather than taken at face value.
"""

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence

logger = logging.getLogger(__name__)

# RFC 6455's "the server is going away". 1000 (normal closure) would be a lie: nothing
# about the connection was finished with, and the extension reads the code on close.
WS_CLOSE_GOING_AWAY = 1001

# A close reason is capped at 123 BYTES by the protocol and websockets raises above it,
# so this stays short. It is the first thing a human sees in the extension's log.
CLOSE_REASON = "server restarting — reconnect shortly"

# What anything still pending after the drain deadline is failed with. Whether a retry is
# safe depends on the action (the request may already have run in the page), so the
# per-action sentence is appended by the RPC layer, not promised here.
PENDING_REASON = ("The server restarted while this request was in flight. The browser "
                  "reconnects on its own within seconds.")

# What a call admitted AFTER the restart began is refused with. Nothing was sent, so the
# retry is not just safe but certain to be the first attempt.
RESTARTING_REFUSAL = ("The server is restarting, so this request was not sent. The browser "
                      "reconnects on its own within seconds — retrying is safe.")

DRAIN_SECONDS_ENV = "BROWSERPLUG_RESTART_DRAIN_SECONDS"
DEFAULT_DRAIN_SECONDS = 5.0
# The clamp the module docstring argues for: 12 (drain) + 2 (close) + 6 (http stop) +
# 0.3 (ack flush) still leaves the replacement's 25 s lock wait several seconds of slack
# on a loaded box.
DRAIN_MAX_SECONDS = 12.0
DRAIN_POLL_SECONDS = 0.05
CLOSE_TIMEOUT_SECONDS = 2.0
HTTP_STOP_TIMEOUT_SECONDS = 6.0
ACK_FLUSH_SECONDS = 0.3
# After the replacement is confirmed, whoever waited on the verdict (POST /restart) needs a
# moment to get its reply out before the sockets and uvicorn go.
VERDICT_FLUSH_SECONDS = 0.2
# Hard outer bound on the confirmation step; the hook bounds itself well inside it.
CONFIRM_MAX_SECONDS = 30.0
# How long POST /restart holds its reply for the verdict. Shorter than the options page's
# 6 s HTTP fallback timeout, so a slow restart still answers "restarting" as it always did.
VERDICT_WAIT_SECONDS = 4.0


def drain_seconds(environ: Optional[dict] = None) -> float:
    """Resolve the drain budget from DRAIN_SECONDS_ENV, clamped to DRAIN_MAX_SECONDS.

    A garbage value reads as the default and a too-large one as the clamp; neither
    raises. This runs on the exit path of a server that was ASKED to restart, and a
    typo in an env var must not be the thing that keeps it alive.
    """
    raw = (os.environ if environ is None else environ).get(DRAIN_SECONDS_ENV)
    if raw is None or str(raw).strip() == "":
        return DEFAULT_DRAIN_SECONDS
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logger.warning("%s=%r is not a number — draining for the default %.1fs",
                       DRAIN_SECONDS_ENV, raw, DEFAULT_DRAIN_SECONDS)
        return DEFAULT_DRAIN_SECONDS
    if value < 0:
        logger.warning("%s=%r is negative — draining for the default %.1fs",
                       DRAIN_SECONDS_ENV, raw, DEFAULT_DRAIN_SECONDS)
        return DEFAULT_DRAIN_SECONDS
    if value > DRAIN_MAX_SECONDS:
        logger.warning("%s=%r exceeds the %.1fs ceiling a replacement can wait out — "
                       "clamping", DRAIN_SECONDS_ENV, raw, DRAIN_MAX_SECONDS)
        return DRAIN_MAX_SECONDS
    return value


class RestartInProgress(Exception):
    """A restart is already running; a second one would spawn a second replacement."""


@dataclass
class DrainReport:
    """Outcome of the drain step. `remaining` > 0 means the deadline won."""
    drained: bool
    remaining: int
    waited: float


@dataclass
class RestartReport:
    """One restart, start to finish — logged as a single line before the process goes."""
    trigger: str
    drain: Optional[DrainReport] = None
    closed: int = 0
    aborted: bool = False
    error: Optional[str] = None

    def describe(self) -> str:
        if self.aborted:
            return f"restart ({self.trigger}) ABORTED: {self.error}"
        if self.drain is None:
            return f"restart ({self.trigger})"
        if self.drain.drained:
            settled = f"{self.drain.waited:.2f}s to settle in-flight work"
        else:
            settled = (f"{self.drain.remaining} request(s) still in flight after "
                       f"{self.drain.waited:.2f}s — interrupted")
        return f"restart ({self.trigger}): {settled}, {self.closed} socket(s) closed cleanly"


async def drain_in_flight(count_in_flight: Callable[[], int], *, timeout: float,
                          poll: float = DRAIN_POLL_SECONDS,
                          sleep=None, clock=time.monotonic) -> DrainReport:
    """Wait (up to `timeout`) for the in-flight browser request count to reach zero.

    Polling rather than awaiting the futures themselves: they belong to the tool
    coroutines' own loops (send_request_and_wait registers them from the uvicorn
    thread), and reaching across for them from here would be a second, subtler version
    of the cross-loop bug ExtensionRpc.settle_request_future already exists to avoid.
    """
    sleep = asyncio.sleep if sleep is None else sleep
    started = clock()
    remaining = int(count_in_flight() or 0)
    if remaining == 0:
        return DrainReport(drained=True, remaining=0, waited=0.0)

    logger.info("Restart: %d request(s) in flight — draining for up to %.1fs",
                remaining, timeout)
    deadline = started + timeout
    while remaining > 0 and clock() < deadline:
        await sleep(poll)
        remaining = int(count_in_flight() or 0)

    waited = clock() - started
    if remaining:
        logger.warning("Restart: %d request(s) still in flight after %.2fs — they will be "
                       "failed with a retryable message", remaining, waited)
    else:
        logger.info("Restart: in-flight work settled in %.2fs", waited)
    return DrainReport(drained=remaining == 0, remaining=remaining, waited=waited)


async def _close_one(conn: Any, code: int, reason: str) -> bool:
    """Close one extension socket, awaiting the handshake. True if it closed cleanly."""
    try:
        try:
            closing = conn.close(code, reason)
        except TypeError:
            # A connection object whose close() takes no arguments (an old websockets,
            # or a test double). A codeless close still beats dropping the socket.
            closing = conn.close()
        if asyncio.iscoroutine(closing) or isinstance(closing, asyncio.Future):
            await closing
        return True
    except Exception as e:
        logger.warning("Restart: closing an extension socket failed (%s) — the process "
                       "exit drops it instead", e)
        return False


async def close_connections(connections: Sequence[Any], *, code: int = WS_CLOSE_GOING_AWAY,
                            reason: str = CLOSE_REASON,
                            timeout: float = CLOSE_TIMEOUT_SECONDS) -> int:
    """Close every extension socket with a real close frame. Returns how many made it.

    Bounded: a peer that never answers the close handshake must not hold the restart
    open. Whatever the timeout catches is dropped by the process exit anyway, which is
    exactly what every close did before this existed.
    """
    conns = [c for c in (connections or []) if c is not None]
    if not conns:
        return 0
    try:
        results = await asyncio.wait_for(
            asyncio.gather(*(_close_one(c, code, reason) for c in conns),
                           return_exceptions=True),
            timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning("Restart: %d extension socket(s) did not finish the close "
                       "handshake within %.1fs — exiting on top of them", len(conns), timeout)
        return 0
    return sum(1 for r in results if r is True)


@dataclass
class RestartHooks:
    """Everything the sequence needs from the server, as plain callables.

    Deliberately not a reference to the server object: the ordering above is the whole
    point of this module and it is worth being able to test it against fakes, without
    a WebSocket, a uvicorn thread, or a process to exit.
    """
    count_in_flight: Callable[[], int]
    connections: Callable[[], Sequence[Any]]
    spawn_replacement: Callable[[], None]
    close_registry: Callable[[str], None]
    stop_http: Callable[[], None]
    exit_process: Callable[[], None]
    # Marks the coming exit as intentional (and takes it back if the spawn fails), so
    # the MCP-thread death guard doesn't read our own shutdown as a crash.
    arm: Callable[[], None] = lambda: None
    disarm: Callable[[], None] = lambda: None
    # Blocking; returns None once the replacement has proved it is alive and waiting for
    # our lock, else a one-line reason (with the tail of its startup log). Bounded by the
    # implementation. The default trusts the spawn, which is what every restart did before.
    confirm_replacement: Callable[[], Optional[str]] = lambda: None
    # True from the start of the sequence until an abort; the server refuses new calls with
    # RESTARTING_REFUSAL while it is up.
    set_restarting: Callable[[bool], None] = lambda on: None
    # The verdict, once: None = proceeding, a string = aborted with that error.
    announce: Callable[[Optional[str]], None] = lambda error: None


async def _run_blocking(fn: Callable[[], None], *, timeout: float) -> None:
    """Run a blocking teardown step off the event loop, bounded.

    stop_http joins the uvicorn thread, which must never happen on the loop that is
    still closing sockets — nor on the uvicorn thread itself, which is where POST
    /restart is handled and where joining would be joining ourselves.
    """
    try:
        await asyncio.wait_for(asyncio.to_thread(fn), timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning("Restart: a teardown step did not finish within %.1fs — "
                       "continuing to exit", timeout)
    except Exception as e:
        logger.warning("Restart: a teardown step failed (%s) — continuing to exit", e)


async def _confirm_replacement(hooks: RestartHooks, timeout: float) -> Optional[str]:
    """Run the blocking confirmation off the loop. None = the replacement is alive."""
    try:
        return await asyncio.wait_for(asyncio.to_thread(hooks.confirm_replacement),
                                      timeout=timeout)
    except asyncio.TimeoutError:
        return f"the replacement was not confirmed within {timeout:.0f}s"
    except Exception as e:
        return f"confirming the replacement failed: {e}"


async def perform_restart(hooks: RestartHooks, *, trigger: str,
                          drain_timeout: Optional[float] = None,
                          ack_flush: float = ACK_FLUSH_SECONDS,
                          verdict_flush: float = VERDICT_FLUSH_SECONDS,
                          confirm_timeout: float = CONFIRM_MAX_SECONDS,
                          close_timeout: float = CLOSE_TIMEOUT_SECONDS,
                          http_stop_timeout: float = HTTP_STOP_TIMEOUT_SECONDS,
                          sleep=None, clock=time.monotonic) -> RestartReport:
    """The steps in the module docstring. Returns without exiting only if aborted."""
    sleep = asyncio.sleep if sleep is None else sleep
    report = RestartReport(trigger=trigger)

    # Both ends of the sequence are logged, and on purpose. The closing report is
    # written by a process that is about to call os._exit, so if anything in between
    # wedges, this opening line is the only evidence the restart was ever asked for.
    logger.info("Restart (%s) beginning: draining PID %d before handing over",
                trigger, os.getpid())

    # Before the drain, not after it: a call admitted while we wait for the others would
    # only add to what has to settle, and a failure inside the window must read as a
    # restart (extension_rpc reads this flag) rather than as a browser that went away.
    hooks.set_restarting(True)

    def abort(error: str, why: str) -> RestartReport:
        # Nothing has been closed yet, which is the whole reason the spawn and its
        # confirmation sit before the teardown: this server is still whole and simply
        # carries on serving.
        hooks.disarm()
        hooks.set_restarting(False)
        report.aborted = True
        report.error = error
        logger.error("Restart (%s): %s (%s) — staying alive with every connection "
                     "intact; retry the restart", trigger, why, error)
        hooks.announce(error)
        return report

    try:
        if ack_flush:
            await sleep(ack_flush)

        budget = drain_seconds() if drain_timeout is None else drain_timeout
        report.drain = await drain_in_flight(hooks.count_in_flight, timeout=budget,
                                             sleep=sleep, clock=clock)

        # From here the process is leaving. Arm BEFORE the spawn: a replacement that
        # binds while we still read our own shutdown as a crash is how the death guard
        # takes the box down with exit(1).
        hooks.arm()
        try:
            hooks.spawn_replacement()
        except Exception as e:
            return abort(str(e), "spawning the replacement failed")

        # Spawned is not started. The replacement dies within about a second when it
        # cannot import, and by then the old code had closed every socket.
        error = await _confirm_replacement(hooks, confirm_timeout)
        if error:
            return abort(error, "the replacement did not come up")
        hooks.announce(None)
        if verdict_flush:
            await sleep(verdict_flush)
    except BaseException as e:
        # A bug in the sequence itself must not leave the server refusing every call.
        hooks.disarm()
        hooks.set_restarting(False)
        if not report.aborted:
            hooks.announce(f"restart failed: {e!r}")
        raise

    report.closed = await close_connections(hooks.connections(), timeout=close_timeout)
    # Registry bookkeeping + whatever outlived the drain. The sockets are already gone,
    # so this is the step that turns them into an honest answer rather than a hang.
    hooks.close_registry(PENDING_REASON)
    await _run_blocking(hooks.stop_http, timeout=http_stop_timeout)

    logger.info("Graceful %s — handing the ports to the replacement", report.describe())
    hooks.exit_process()
    return report
