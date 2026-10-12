"""The admission gate as wired into the REAL server send path.

tests/unit/test_request_queue.py pins the gate's own behaviour. This module pins the six
lines that HOOK it into BrowserPlugServer.send_request_and_wait, which is where the risk
actually lives: a gate that is never acquired, never released, or released on only one of
the exit paths would leave the suite green and the browser progressively bricked.

The finding being fixed: driven by ONE agent the bridge timed out on 0.8% of navigations;
driven by several subagents sharing one browser that became 39.3% — 474 calls that returned
no page at all — and the response never said contention was the cause, so the agent's
reasonable inferences (slow site, bad URL, retry) were all wrong and retrying was the worst
of them.
"""

import asyncio
import uuid

import pytest

import test_imports  # noqa: F401
from server.browsers import request_queue
from server.server import BrowserPlugServer
from test_browser_registry import FakeWS, connect, hello


@pytest.fixture
def server():
    return BrowserPlugServer(host="localhost", port=8772, start_mcp=False)


async def add_browser(server, instance_id="ff-1", pid=4242):
    ws = FakeWS(peer=("127.0.0.1", 10000 + pid))
    identity = {"pid": pid, "brand": "Firefox", "friendly": "Firefox", "is_firefox": True,
                "exe_path": r"C:\ff\firefox.exe", "start_time": None}
    await connect(server, ws, identity=identity)
    await hello(server, ws, instance_id=instance_id,
                browserInfo={"name": "Firefox", "version": "152.0"})
    return ws


def answer_after(server, delay, observed):
    """Stand in for the extension: record how many requests are executing CONCURRENTLY,
    hold for `delay`, then complete the pending future."""

    async def send_to_extension(request, browser=None):
        observed["now"] = observed.get("now", 0) + 1
        observed["peak"] = max(observed.get("peak", 0), observed["now"])

        async def finish():
            await asyncio.sleep(delay)
            observed["now"] -= 1
            fut = server.rpc.pending_requests.get(request["id"])
            if fut is not None and not fut.done():
                fut.set_result({"type": "response", "action": request["action"],
                                "data": {"ok": True}})

        asyncio.get_running_loop().create_task(finish())
        return True

    server.rpc.send_to_extension = send_to_extension


def a_request(action="tabs.list"):
    # uuid, not id(object()): CPython recycles addresses the moment the temporary is freed,
    # so id() collides and two requests end up sharing one pending-future slot.
    return {"id": str(uuid.uuid4()), "type": "request", "action": action, "data": {}}


@pytest.mark.asyncio
async def test_fan_out_is_queued_not_dropped(server):
    """Ten concurrent requests at one browser: every one succeeds, none times out, and the
    extension is never asked to hold more than the limit at once. This is the whole story —
    the load arrives, it is absorbed by waiting, and nothing fails for arriving Nth."""
    await add_browser(server)
    observed = {}
    answer_after(server, 0.02, observed)

    results = await asyncio.gather(*[
        server.send_request_and_wait(a_request(), timeout=5.0) for _ in range(10)])

    assert all("error" not in r for r in results), \
        f"a fan-out must not fail: {[r for r in results if 'error' in r]}"
    assert observed["peak"] <= request_queue.max_concurrent_requests()
    assert observed["now"] == 0


@pytest.mark.asyncio
async def test_the_gate_is_released_on_every_exit_path(server):
    """Success, timeout and a send that fails all have to give the slot back. A leak here
    degrades monotonically and silently: lose `limit` slots and the browser answers nothing
    ever again, with no error anywhere pointing at the cause."""
    await add_browser(server)
    gate = server.request_queue.gate_for("ff-1")

    # 1. success
    observed = {}
    answer_after(server, 0.0, observed)
    await server.send_request_and_wait(a_request(), timeout=5.0)
    assert (gate.inflight, gate.queued) == (0, 0)

    # 2. timeout — the extension simply never answers
    async def never_answer(request, browser=None):
        return True
    server.rpc.send_to_extension = never_answer
    out = await server.send_request_and_wait(a_request(), timeout=0.05)
    assert "timed out" in out["error"]
    # Changed deliberately (review C6): the browser may still be executing a timed-out
    # request and the wire has no cancel, so the slot is held until the late reply or
    # the grace (2 x the timeout). tests/unit/test_rpc_slot_lifecycle.py pins each release.
    assert (gate.inflight, gate.queued) == (1, 0)
    deadline = asyncio.get_running_loop().time() + 1.0
    while gate.inflight and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.02)
    assert (gate.inflight, gate.queued) == (0, 0)

    # 3. the send itself fails
    async def refuse(request, browser=None):
        return False
    server.rpc.send_to_extension = refuse
    out = await server.send_request_and_wait(a_request(), timeout=5.0)
    assert out["error"] == "Failed to send request to extension"
    assert (gate.inflight, gate.queued) == (0, 0)


@pytest.mark.asyncio
async def test_a_solo_timeout_reads_exactly_as_it_always_did(server):
    """Criterion (d), stated as a string. With nothing else in flight the message must be
    byte-identical to the wording used before the gate existed — both because it is what
    every existing diagnosis expects, and because the CONTENDED wording only carries
    information while it is not what a solo agent routinely sees."""
    await add_browser(server)

    async def never_answer(request, browser=None):
        return True
    server.rpc.send_to_extension = never_answer

    out = await server.send_request_and_wait(a_request(), timeout=0.05)
    assert out["error"] == "Request timed out after 0.05 seconds"


@pytest.mark.asyncio
async def test_a_contended_timeout_names_the_contention(server):
    """Criterion (b). Under real contention the same failure must say so, and must steer
    away from the two reflexes that made the original incident worse."""
    await add_browser(server)

    async def never_answer(request, browser=None):
        return True
    server.rpc.send_to_extension = never_answer

    outs = await asyncio.gather(*[
        server.send_request_and_wait(a_request(), timeout=0.3) for _ in range(6)])
    contended = [o["error"] for o in outs if "CONTENDED" in o.get("error", "")]

    assert contended, f"no contended timeout among {len(outs)}: {outs[0]['error']!r}"
    msg = contended[0]
    assert "other request(s) were executing" in msg
    assert "NOT evidence that the page failed to load" in msg
    assert "do NOT raise timeout_ms" in msg


@pytest.mark.asyncio
async def test_two_browsers_do_not_share_a_limit(server):
    """A saturated Chrome must not throttle an idle Firefox — they are two independent
    bridges, and one queue across both would turn a busy browser into an outage for the
    other, which is the opposite of what the gate is for."""
    await add_browser(server, instance_id="ff-1", pid=1)
    await add_browser(server, instance_id="ff-2", pid=2)
    a, b = server.browsers["ff-1"], server.browsers["ff-2"]

    observed = {}
    answer_after(server, 0.02, observed)

    # Saturate ff-1 while ff-2 stays free; ff-2's request must not queue behind it.
    busy = [asyncio.create_task(
        server.send_request_and_wait(a_request(), timeout=5.0, browser=a))
        for _ in range(8)]
    await asyncio.sleep(0)
    out = await server.send_request_and_wait(a_request(), timeout=1.0, browser=b)
    assert "error" not in out
    assert server.request_queue.gate_for("ff-2").snapshot()["queued"] == 0
    await asyncio.gather(*busy)


@pytest.mark.asyncio
async def test_the_limit_is_discoverable(server):
    """Criterion (e): an operator must be able to READ the limit instead of inferring it
    from failure rates after the fact. server_info already dumps this payload, so it costs
    no tool-description bytes — which matters, because that block is a capped budget."""
    await add_browser(server)
    info = server.get_connections_info()["browser_concurrency"]
    assert info["max_concurrent_per_browser"] == request_queue.max_concurrent_requests()
    assert info["env_vars"]["concurrency"] == "BROWSERPLUG_MAX_CONCURRENT_REQUESTS"


@pytest.mark.asyncio
async def test_a_departed_browser_leaves_no_gate_behind(server):
    """A reconnecting browser must start with a clean depth, not inherit a dead one's
    counters — and dropping the gate must not strand anyone still holding a slot."""
    await add_browser(server)
    observed = {}
    answer_after(server, 0.0, observed)
    await server.send_request_and_wait(a_request(), timeout=5.0)
    assert "ff-1" in server.request_queue.snapshot()

    server.registry.remove(server.browsers["ff-1"])
    assert "ff-1" not in server.request_queue.snapshot()
