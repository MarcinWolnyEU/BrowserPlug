"""The one polling wait the unit suite uses, in a sync and an async form.

Two shapes, and a call site picks by what it claims:

POSITIVE wait: "this will happen, I only don't know when". Use `wait_until` or
`wait_until_async`. The predicate ends the wait, so a healthy run returns after a few
polls whatever the timeout is. The timeout exists to turn a hang into a failure, so the
default (DEFAULT_TIMEOUT, 15 s) is generous on purpose: a 3-vCPU CI VM that starves a
thread for two seconds must not fail a test that is working. Do not pass a short
`timeout=` to make a test finish sooner on failure; it only makes it flaky.

NEGATIVE check: "this did NOT happen within X seconds". Use `assert_never_within` or
`assert_never_within_async`. There is no event to wait for, so the call spends the whole
window every time and `within` is required, with no default: the call site states how long
"not happening" has to hold. A negative can only produce a false pass on a slow box (the
thing would have happened later), never a false failure, so a short window is safe.

Polling is the right tool when the awaited state has no event to hook. Where it has one
(an asyncio.Event, a threading.Event, a task that returns), wait on that instead.

Both positive forms return the predicate's last (truthy) value, so
`row = wait_until(lambda: registry.get(key), "the row was registered")` works. The async
forms also accept a predicate that returns an awaitable.
"""

import asyncio
import inspect
import time

DEFAULT_TIMEOUT = 15.0
POLL_INTERVAL = 0.01


def wait_until(predicate, what, timeout=DEFAULT_TIMEOUT, interval=POLL_INTERVAL):
    """Poll `predicate` until it returns something truthy, and return that value.

    Raises AssertionError("timed out after Ns waiting until <what>") at the deadline.
    `what` finishes the sentence "waiting until ...", e.g. "the hello registered ff-a".
    """
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out after {timeout}s waiting until {what}")
        time.sleep(interval)


async def wait_until_async(predicate, what, timeout=DEFAULT_TIMEOUT, interval=POLL_INTERVAL):
    """The async twin of wait_until: sleeps with asyncio so the loop keeps running.

    `predicate` may be a plain function or return an awaitable.
    """
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if inspect.isawaitable(value):
            value = await value
        if value:
            return value
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out after {timeout}s waiting until {what}")
        await asyncio.sleep(interval)


def assert_never_within(predicate, what, within, interval=POLL_INTERVAL):
    """Fail if `predicate` turns truthy at any point in the next `within` seconds.

    `what` finishes "did not happen: ...", and names the thing that must stay false.
    Always spends the full window when it passes.
    """
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if predicate():
            raise AssertionError(f"happened within {within}s, expected never: {what}")
        time.sleep(interval)
    if predicate():
        raise AssertionError(f"happened within {within}s, expected never: {what}")


async def assert_never_within_async(predicate, what, within, interval=POLL_INTERVAL):
    """The async twin of assert_never_within. `predicate` may return an awaitable."""

    async def holds():
        value = predicate()
        if inspect.isawaitable(value):
            value = await value
        return value

    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if await holds():
            raise AssertionError(f"happened within {within}s, expected never: {what}")
        await asyncio.sleep(interval)
    if await holds():
        raise AssertionError(f"happened within {within}s, expected never: {what}")
