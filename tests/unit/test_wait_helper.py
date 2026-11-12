"""tests/wait_helper.py: the shared polling wait, which every other unit test leans on."""

import ast
import asyncio
import re
from pathlib import Path

import pytest

import wait_helper
from wait_helper import (
    assert_never_within,
    assert_never_within_async,
    wait_until,
    wait_until_async,
)


def test_the_default_timeout_is_generous():
    # It guards a hang; the predicate ends the wait. A short default made the suite flaky.
    assert wait_helper.DEFAULT_TIMEOUT >= 10


def test_wait_until_returns_the_truthy_value_as_soon_as_it_holds():
    calls = []

    def predicate():
        calls.append(1)
        return "row" if len(calls) == 3 else None

    assert wait_until(predicate, "the row appeared") == "row"
    assert len(calls) == 3


def test_wait_until_names_what_never_happened():
    with pytest.raises(AssertionError, match="waiting until the moon turned green"):
        wait_until(lambda: False, "the moon turned green", timeout=0.05)


async def test_wait_until_async_takes_a_plain_or_an_awaitable_predicate():
    box = []
    asyncio.get_running_loop().call_later(0.02, box.append, 1)
    assert await wait_until_async(lambda: bool(box), "the box filled") is True

    async def filled():
        return box

    assert await wait_until_async(filled, "the box was seen filled") == [1]


async def test_wait_until_async_names_what_never_happened():
    with pytest.raises(AssertionError, match="waiting until the box filled"):
        await wait_until_async(lambda: False, "the box filled", timeout=0.05)


def test_assert_never_within_passes_when_nothing_happens():
    assert_never_within(lambda: False, "it fired", within=0.05)


def test_assert_never_within_fails_when_it_happens_in_the_window():
    with pytest.raises(AssertionError, match="expected never: it fired"):
        assert_never_within(lambda: True, "it fired", within=0.05)


async def test_assert_never_within_async_sees_a_late_event_and_takes_awaitables():
    box = []
    asyncio.get_running_loop().call_later(0.02, box.append, 1)
    with pytest.raises(AssertionError, match="expected never: the box filled"):
        await assert_never_within_async(lambda: box, "the box filled", within=1.0)

    async def never():
        return False

    await assert_never_within_async(never, "it fired", within=0.05)


# --- the guard: no private poll-until-true helpers -------------------------------------

UNIT_DIR = Path(__file__).resolve().parent

# A poll helper is found by name (leading underscore and `_async` suffix included) or by
# taking its condition in a parameter called `predicate`, which every one of them did.
HELPER_NAME = re.compile(r"_?(wait_until|wait_for|until|eventually|poll_until)(_async)?")

# "file::qualified name" for each def that matches but does not poll, with the reason.
ALLOWED_DEFS = {
    # Replaces asyncio.wait_for inside http_endpoints and records the timeout it was given.
    # It waits on an awaitable once; it polls nothing.
    "test_identity_connection_lifetime.py::RecordedWaits.wait_for",
}


def _unit_trees():
    for path in sorted(UNIT_DIR.glob("*.py")):
        yield path.name, ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _defs(tree, scope=()):
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield ".".join((*scope, node.name)), node
            yield from _defs(node, (*scope, node.name))
        elif isinstance(node, ast.ClassDef):
            yield from _defs(node, (*scope, node.name))


def _is_poll_helper(node):
    args = node.args
    names = [a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)]
    return bool(HELPER_NAME.fullmatch(node.name)) or "predicate" in names


def test_no_unit_test_defines_its_own_poll_until_true_helper():
    found = [f"{name}:{node.lineno} defines {qualified}()"
             for name, tree in _unit_trees()
             for qualified, node in _defs(tree)
             if _is_poll_helper(node) and f"{name}::{qualified}" not in ALLOWED_DEFS]
    assert not found, (
        "These files poll with a private helper. Use tests/wait_helper.py instead: "
        "wait_until or wait_until_async for something that will happen, "
        "assert_never_within or assert_never_within_async for something that must not. "
        "A private copy has its own timeout and its own failure text, and a short one "
        "is what made the suite flaky on slow runners.\n" + "\n".join(found))


def test_no_unit_test_takes_a_wait_helper_from_another_test_module():
    found = [f"{name}:{node.lineno} imports {alias.name} from {node.module}"
             for name, tree in _unit_trees()
             for node in ast.walk(tree)
             if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("test_")
             for alias in node.names
             if HELPER_NAME.fullmatch(alias.name)]
    assert not found, (
        "Import the wait from wait_helper directly (`from wait_helper import wait_until`), "
        "not through another test module:\n" + "\n".join(found))


def test_the_guard_recognises_a_private_helper():
    tree = ast.parse("class T:\n    async def until(self, marker): ...\n"
                     "def _wait_until(p, what): ...\ndef settle(predicate): ...\n"
                     "def fine(x): ...\n")
    flagged = [q for q, node in _defs(tree) if _is_poll_helper(node)]
    assert flagged == ["T.until", "_wait_until", "settle"]


# --- the guard, second half: inline poll loops ------------------------------------------
# A private helper is found by its name; a loop written inline in a test or in a harness
# method (`while time.monotonic() < deadline: ... time.sleep(...)`) has no name to find. It
# is found by its shape instead: a `while` loop that sleeps and reads a clock or a deadline.

# Names that mark a variable as a deadline.
DEADLINE_NAME = re.compile(r"deadline|give_?up|until", re.IGNORECASE)
CLOCK_CALLS = {"monotonic", "perf_counter", "time"}


def _is_sleep(node):
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    return (isinstance(func, ast.Attribute) and func.attr == "sleep") or (
        isinstance(func, ast.Name) and func.id == "sleep")


def _reads_a_clock_or_deadline(node):
    if isinstance(node, ast.Call):
        return isinstance(node.func, ast.Attribute) and node.func.attr in CLOCK_CALLS
    return isinstance(node, ast.Name) and bool(DEADLINE_NAME.search(node.id))


def _poll_loops(tree, scope=()):
    """(qualified name of the enclosing def or class, line) for each `while` loop that sleeps
    and reads a clock or a deadline. A loop inside a nested def belongs to that def."""
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            yield from _poll_loops(node, (*scope, node.name))
            continue
        if isinstance(node, ast.While):
            inside = list(ast.walk(node))
            if any(_is_sleep(n) for n in inside) and any(
                    _reads_a_clock_or_deadline(n) for n in inside):
                yield ".".join(scope) or "<module>", node.lineno
        yield from _poll_loops(node, scope)


# "file::qualified name" of each loop that is not a poll-until-true, with the reason.
ALLOWED_LOOPS = {
    # Fixed-rate sampling for a fixed window. The loop measures; it waits for nothing.
    "test_hang_guard.py::test_a_client_with_a_hostile_command_line_does_not_stall_either_loop":
        "probes at a fixed rate for a fixed window and keeps the worst answer time",
    "test_hang_guard.py::LoopWatcher.run": "samples the loop's own tick gaps until stopped",
    "test_reports_offloop.py::Ticker.run": "samples the loop's own tick gaps until stopped",
    "test_http_endpoints.py::test_the_host_guards_interface_lookup_runs_on_a_worker_not_the_loop.ticker":
        "ticks on the loop to show it was never blocked",
    "test_watchdog.py::test_a_busy_but_not_blocked_loop_is_left_alone":
        "drives load for a fixed time; the time is the subject",
    "test_request_queue.py::test_queue_wait_does_not_consume_the_execution_budget":
        "lets a fixed budget elapse while the queue is held; elapsed time is the subject",
    # Server-side stand-ins that produce a stream for as long as the client reads it.
    "test_relay_era.py::TestPost.test_a_server_that_only_pings_hits_the_overall_deadline.pinging":
        "a handler that pings until the client cuts the connection",
    "test_stdio_relay.py::TestModern.test_a_slow_modern_call_is_bounded_by_the_deadline_like_any_other.pinging":
        "a handler that pings until the client cuts the connection",
    # A mock that reproduces the product's own deadline wait, so it is not a wait of the test.
    "test_test_helper_protocol.py::TestTestHelperProtocol.mock_server.mock_send_request_and_wait":
        "stands in for the product's send_request_and_wait, deadline included",
}


def test_no_unit_test_polls_with_an_inline_loop_of_its_own():
    found = [f"{name}:{line} polls in {qualified}"
             for name, tree in _unit_trees()
             for qualified, line in _poll_loops(tree)
             if f"{name}::{qualified}" not in ALLOWED_LOOPS]
    assert not found, (
        "These files poll with a `while` loop that sleeps and checks a deadline. Use "
        "tests/wait_helper.py: wait_until or wait_until_async for something that will "
        "happen, assert_never_within or assert_never_within_async for something that must "
        "not. If the loop waits for nothing (it samples, or serves a stream), add it to "
        "ALLOWED_LOOPS with the reason.\n" + "\n".join(found))


def test_the_inline_loop_guard_recognises_a_poll_loop():
    tree = ast.parse(
        "import time\n"
        "def poll(deadline):\n"
        "    while time.monotonic() < deadline:\n"
        "        time.sleep(0.1)\n"
        "async def apoll(end):\n"
        "    while not done() and asyncio.get_running_loop().time() < end:\n"
        "        await asyncio.sleep(0.1)\n"
        "class T:\n"
        "    def test_x(self):\n"
        "        def helper():\n"
        "            while time.perf_counter() < 5:\n"
        "                sleep(1)\n"
        "def not_a_poll():\n"
        "    while queue:\n"
        "        time.sleep(1)\n"
        "    for _ in range(3):\n"
        "        time.sleep(1)\n"
        "    while time.monotonic() < 5:\n"
        "        pass\n")
    # The asyncio loop's `.time()` is a clock too, and is caught through its `end` name.
    assert list(_poll_loops(tree)) == [("poll", 3), ("apoll", 6), ("T.test_x.helper", 11)]

