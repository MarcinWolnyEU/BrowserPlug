"""Admission slots and pending-request state across retry, cancel, timeout and disconnect.

Review findings C5 and C6 (.tmp/server-launcher-review-2026-09-29.md):

  C5  A transient-disconnect retry ran while its parent request still held the browser's
      admission slot. A displaced socket keeps its gate (only remove() drops it), so with
      BROWSERPLUG_MAX_CONCURRENT_REQUESTS=1 the retry queued behind its own parent until
      the 60 s queue budget ran out.
  C6  A client cancel raised CancelledError past `except Exception`, leaving the
      pending_requests/pending_owners entries behind (every restart then waited out its
      drain budget). A timeout or cancel also released the slot at once although the
      browser was still working, so the gate stopped bounding the extension.
"""

import asyncio

import pytest

import test_imports  # noqa: F401
from server.server import BrowserPlugServer
from test_concurrency_gate import a_request, add_browser
from test_disconnect_retry import FakeWS, connect, hello, wait_until
import server.rpc.request_retry as request_retry


@pytest.fixture
def server():
    return BrowserPlugServer(host="localhost", port=8772, start_mcp=False)


async def silent_extension(server):
    async def send(request, browser=None):
        return True
    server.rpc.send_to_extension = send


def start(server, request, timeout):
    return asyncio.create_task(server.send_request_and_wait(request, timeout=timeout))


# --- C5 -----------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_retry_after_displacement_does_not_wait_on_its_parents_slot(server, monkeypatch):
    monkeypatch.setenv("BROWSERPLUG_MAX_CONCURRENT_REQUESTS", "1")
    monkeypatch.setenv("BROWSERPLUG_MAX_QUEUE_WAIT", "2")
    monkeypatch.setattr(request_retry, "RECONNECT_GRACE_SECONDS", 1.5)
    identity = {"pid": 4242, "brand": "Firefox", "friendly": "Firefox", "is_firefox": True,
                "exe_path": r"C:firefox.exe", "start_time": None}
    a = FakeWS(peer=("127.0.0.1", 1111))
    await connect(server, a, identity=identity)
    await hello(server, a, instanceId="ff-x")
    browser = server.browsers["ff-x"]

    call = asyncio.create_task(server.send_request_and_wait(
        {"id": "req-1", "type": "request", "action": "tabs.list", "data": {}},
        timeout=5.0, browser=browser))
    await wait_until(lambda: a.sent, "the read reached the first socket")

    # Same profile reconnects while the first socket is still open: displacement, no remove().
    a2 = FakeWS(peer=("127.0.0.1", 2222))
    await connect(server, a2, identity=identity)
    await hello(server, a2, instanceId="ff-x")

    await wait_until(lambda: a2.sent, "the retry reached the new socket", timeout=1.0)
    retry = a2.sent[-1]
    a2.feed({"id": retry["id"], "type": "response", "action": "tabs.list", "data": {"ok": 1}})
    result = await asyncio.wait_for(call, 2.0)
    assert result.get("data") == {"ok": 1}, result


# --- C6: cancellation cleanup ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_cancelled_request_leaves_no_pending_state(server):
    await add_browser(server)
    await silent_extension(server)
    request = a_request()
    task = start(server, request, 30.0)
    await wait_until(lambda: request["id"] in server.rpc.pending_requests, "request pending")

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert request["id"] not in server.rpc.pending_requests
    assert request["id"] not in server.rpc.pending_owners
    assert len(server.rpc.pending_requests) == 0


# --- C6: slot semantics ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_timeout_keeps_the_slot_until_the_late_reply(server):
    await add_browser(server)
    await silent_extension(server)
    gate = server.request_queue.gate_for("ff-1")
    request = a_request()

    out = await server.send_request_and_wait(request, timeout=0.05)
    assert "timed out" in out["error"]
    assert gate.inflight == 1, "the browser may still be working on it"

    await server.rpc.handle_response({"id": request["id"], "type": "response", "data": {}})
    await asyncio.sleep(0.01)
    assert gate.inflight == 0, "the late reply frees the slot"


@pytest.mark.asyncio
async def test_timeout_slot_is_freed_after_the_grace_if_no_reply_comes(server):
    await add_browser(server)
    await silent_extension(server)
    gate = server.request_queue.gate_for("ff-1")

    await server.send_request_and_wait(a_request(), timeout=0.05)  # grace = 2 x 0.05
    assert gate.inflight == 1
    await wait_until(lambda: gate.inflight == 0, "grace expiry", timeout=1.0)


@pytest.mark.asyncio
async def test_cancel_keeps_the_slot_until_the_late_reply(server):
    await add_browser(server)
    await silent_extension(server)
    gate = server.request_queue.gate_for("ff-1")
    request = a_request()
    task = start(server, request, 30.0)
    await wait_until(lambda: request["id"] in server.rpc.pending_requests, "request pending")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert gate.inflight == 1

    await server.rpc.handle_response({"id": request["id"], "type": "response", "data": {}})
    await asyncio.sleep(0.01)
    assert gate.inflight == 0


@pytest.mark.asyncio
async def test_disconnect_frees_a_lingering_slot(server):
    """A dead socket is not working on anything, so waiting out the grace would only
    strand the slot."""
    await add_browser(server)
    await silent_extension(server)
    gate = server.request_queue.gate_for("ff-1")
    await server.send_request_and_wait(a_request(), timeout=0.05)
    assert gate.inflight == 1

    server.rpc.fail_pending_requests("gone", instance_id="ff-1")
    await asyncio.sleep(0.01)
    assert gate.inflight == 0
