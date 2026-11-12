"""
Unit tests for the transient-disconnect auto-retry.

Background: `ExtensionRpc.fail_pending_requests` settles an in-flight request the instant the browser
drops its socket, instead of letting it burn the full timeout and misreport "Request timed
out". That fast-fail is correct, but a drop is very often purely transport-transient — the
extension reconnects within seconds and a manual retry succeeds. The server makes that retry
automatic, but ONLY:
  * for read-only ("idempotent") actions — re-sending them is safe,
  * ONCE — a second disconnect on the retry is returned, never retried again,
  * within a short reconnect grace — a browser that never comes back still yields the error.

The policy lives in server/rpc/request_retry.py; the hooks are in `ExtensionRpc.fail_pending_requests`
(it tags an instance-scoped failure) and `send_request_and_wait` (it consults the policy and
re-sends once). These tests drive the REAL connection handler with fake sockets, exactly like
test_browser_registry.py, so the real disconnect reason text and the real registry re-keying
are exercised end to end.
"""

import asyncio
import inspect
import json
from unittest.mock import patch

import pytest

from server.server import BrowserPlugServer
import server.rpc.request_retry as request_retry
from wait_helper import (
    DEFAULT_TIMEOUT,
    assert_never_within_async,
    wait_until_async,
)

_EOF = object()


class FakeWS:
    """A WebSocket the connection handler can iterate, fed message by message. `sent` records
    every frame the SERVER sends to this browser (as parsed dicts)."""

    def __init__(self, peer=("127.0.0.1", 5555)):
        self.remote_address = peer
        self.close_code = None
        self.sent = []
        self._q = asyncio.Queue()

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    async def close(self, code=1000, reason=""):
        if self.close_code is None:
            self.close_code = code
        self._q.put_nowait(_EOF)

    def feed(self, message):
        self._q.put_nowait(json.dumps(message))

    def drop(self, code=1006):
        """The peer vanished (Firefox closed / crashed / reloaded the extension)."""
        self.close_code = code
        self._q.put_nowait(_EOF)

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self._q.get()
        if item is _EOF:
            raise StopAsyncIteration
        return item


@pytest.fixture
def server():
    return BrowserPlugServer(host="localhost", port=8772, start_mcp=False)


@pytest.fixture(autouse=True)
def fast_grace(monkeypatch):
    """Bound every test's reconnect wait so a mis-wired retry can't hang the default 8s.
    5 s is still far more than a reconnect needs (the tests wait on state, not on sleeps),
    so a slow runner does not run out of grace mid-test. Individual tests may override via
    the BROWSERPLUG_RECONNECT_GRACE env var (which reconnect_grace_seconds() reads first)."""
    monkeypatch.setattr(request_retry, "RECONNECT_GRACE_SECONDS", 5.0)


async def connect(server, ws, identity=None):
    """Run the real connection handler for `ws`, with the local pid probe stubbed."""
    with patch.object(server, "_probe_browser_identity", return_value=identity or {}):
        task = asyncio.create_task(server.handle_extension_connection(ws))
        await wait_until_async(lambda: server.registry.browser_for_ws(ws) is not None,
                               "the handler registered the socket")
    return task


async def hello(server, ws, **fields):
    """Deliver an extension_hello on `ws` (its instance_id names the browser)."""
    ws.feed({"type": "extension_hello", "data": fields})
    instance_id = fields["instanceId"]
    await wait_until_async(
        lambda: getattr(server.browsers.get(instance_id), "ws", None) is ws,
        f"the hello promoted the socket to browser {instance_id!r}")


async def disconnect(ws, task):
    """The browser vanishes: its handler runs its finally (remove + fail-pending)."""
    ws.drop()
    await asyncio.wait_for(task, timeout=DEFAULT_TIMEOUT)
    await asyncio.sleep(0)  # futures settle on their owning loop's next tick


def call_action(server, request_id, browser, action, data=None, timeout=DEFAULT_TIMEOUT):
    return asyncio.create_task(server.send_request_and_wait(
        {"id": request_id, "type": "request", "action": action, "data": data or {}},
        timeout=timeout, browser=browser))


# --- 1. idempotent action retried once on reconnect, caller gets the retry's result --------

@pytest.mark.asyncio
async def test_idempotent_call_retries_once_after_reconnect(server):
    a = FakeWS(peer=("127.0.0.1", 1111))
    a_task = await connect(server, a)
    await hello(server, a, instanceId="ff-x")
    browser_a = server.browsers["ff-x"]

    data = {"tabId": 7, "full_page": False}
    # tabs.screenshot is the action the original report came from, and it is idempotent.
    call = call_action(server, "req-1", browser_a, "tabs.screenshot", data)
    await wait_until_async(lambda: a.sent, "the screenshot request reached the browser")
    assert a.sent[-1]["id"] == "req-1"

    # The extension drops mid-request (transport-transient).
    await disconnect(a, a_task)
    assert not call.done(), "an idempotent read must wait for the reconnect, not fail yet"

    # Same profile reconnects within the grace window.
    a2 = FakeWS(peer=("127.0.0.1", 2222))
    a2_task = await connect(server, a2)
    await hello(server, a2, instanceId="ff-x")

    await wait_until_async(lambda: a2.sent, "the retry reached the reconnected browser")
    retry = a2.sent[-1]
    assert retry["action"] == "tabs.screenshot", "same action is re-sent"
    assert retry["data"] == data, "same data is re-sent"
    assert retry["id"] != "req-1", "the retry is a FRESH request id"

    # The reconnected browser answers the retry -> the caller gets the retried result.
    a2.feed({"id": retry["id"], "type": "response", "action": "tabs.screenshot",
             "data": {"image": "PNGDATA"}})
    result = await asyncio.wait_for(call, DEFAULT_TIMEOUT)
    assert result["type"] == "response"
    assert result["data"] == {"image": "PNGDATA"}
    assert "transient_disconnect" not in result and "instance_id" not in result, \
        "the private plumbing keys never reach the caller"

    await disconnect(a2, a2_task)


# --- 2. non-idempotent action: immediate error with the hint, never re-sent ----------------

@pytest.mark.asyncio
async def test_non_idempotent_action_is_not_retried(server):
    a = FakeWS(peer=("127.0.0.1", 1111))
    a_task = await connect(server, a)
    await hello(server, a, instanceId="ff-x")
    browser_a = server.browsers["ff-x"]

    call = call_action(server, "req-1", browser_a, "content.act", {"steps": []})
    await wait_until_async(lambda: a.sent, "the mutating request reached the browser")

    await disconnect(a, a_task)

    # content.act mutates, so it must NOT be re-sent — the caller gets the error at once.
    result = await asyncio.wait_for(call, DEFAULT_TIMEOUT)
    assert "error" in result
    assert "disconnected" in result["error"]
    # Changed on purpose (test_retry_truth.py): a click may already have run, so the
    # message no longer calls it safe to retry; it says to check the page first.
    assert "safe to retry" not in result["error"]
    assert "may have already run" in result["error"]
    assert "transient_disconnect" not in result and "instance_id" not in result

    # Even if that browser reconnects, the mutating call is never re-sent.
    a2 = FakeWS(peer=("127.0.0.1", 2222))
    a2_task = await connect(server, a2)
    await hello(server, a2, instanceId="ff-x")
    await assert_never_within_async(     # ample time for an (erroneous) retry to fire
        lambda: any(m.get("action") == "content.act" for m in a2.sent),
        "a non-idempotent action was auto-retried", within=0.3)

    await disconnect(a2, a2_task)


# --- 3. no reconnect within grace: original error returned, private keys stripped ----------

@pytest.mark.asyncio
async def test_no_reconnect_within_grace_returns_original_error(server, monkeypatch):
    # A tiny grace via the env var (which reconnect_grace_seconds reads first, overriding
    # the fixture's attribute — so this also exercises env precedence).
    monkeypatch.setenv("BROWSERPLUG_RECONNECT_GRACE", "0.15")

    a = FakeWS(peer=("127.0.0.1", 1111))
    a_task = await connect(server, a)
    await hello(server, a, instanceId="ff-x")
    browser_a = server.browsers["ff-x"]

    call = call_action(server, "req-1", browser_a, "content.snapshot")
    await wait_until_async(lambda: a.sent, "the snapshot request reached the browser")

    # The browser drops and never comes back.
    await disconnect(a, a_task)

    result = await asyncio.wait_for(call, DEFAULT_TIMEOUT)
    assert "error" in result
    assert "disconnected" in result["error"]
    assert "safe to retry" in result["error"], "the transient hint is preserved"
    assert "transient_disconnect" not in result, "private key stripped on the fall-through path"
    assert "instance_id" not in result


# --- 4. retry happens at MOST once: a second disconnect is returned, not retried again ------

@pytest.mark.asyncio
async def test_retry_happens_at_most_once(server, monkeypatch):
    # A grace far longer than the test. A second retry would wait in wait_for_reconnect for
    # the whole grace, which keeps the call in flight while the third browser below connects
    # and would be handed the request. The correct code settles the call the instant the
    # retry's socket drops, so the grace never starts.
    monkeypatch.setattr(request_retry, "RECONNECT_GRACE_SECONDS", 60.0)
    # wait_for_reconnect looks for the browser every `poll` seconds, so a second retry would
    # land within one `poll` of a3's hello. The window below is five polls.
    poll = inspect.signature(request_retry.wait_for_reconnect).parameters["poll_seconds"].default
    window = poll * 5

    a = FakeWS(peer=("127.0.0.1", 1111))
    a_task = await connect(server, a)
    await hello(server, a, instanceId="ff-x")
    browser_a = server.browsers["ff-x"]

    call = call_action(server, "req-1", browser_a, "tabs.list")
    await wait_until_async(lambda: a.sent, "the list request reached the browser")

    await disconnect(a, a_task)  # transient disconnect #1

    # First reconnect -> the ONE allowed retry is dispatched here.
    a2 = FakeWS(peer=("127.0.0.1", 2222))
    a2_task = await connect(server, a2)
    await hello(server, a2, instanceId="ff-x")
    await wait_until_async(lambda: a2.sent, "the retry reached the first reconnect")
    retry = a2.sent[-1]
    assert retry["action"] == "tabs.list" and retry["id"] != "req-1"

    # ...but the retry ALSO hits a transient disconnect. The call is not awaited yet, so a
    # second retry would still have it in flight when the third browser arrives.
    await disconnect(a2, a2_task)  # transient disconnect #2
    a3 = FakeWS(peer=("127.0.0.1", 3333))
    a3_task = await connect(server, a3)
    await hello(server, a3, instanceId="ff-x")

    def lists_to(ws):
        return [m for m in ws.sent if m.get("action") == "tabs.list"]

    await assert_never_within_async(
        lambda: lists_to(a3),
        "the request was retried a second time, onto the third connection", within=window)

    result = await asyncio.wait_for(call, DEFAULT_TIMEOUT)
    assert "error" in result and "disconnected" in result["error"], \
        "a second disconnect is returned as the error, not retried again"
    assert result.get("code") == request_retry.CODE_DISCONNECTED
    assert "transient_disconnect" not in result and "instance_id" not in result
    # The call went out twice in all (the original and the one retry), never a third time.
    assert [len(lists_to(ws)) for ws in (a, a2, a3)] == [1, 1, 0]

    await disconnect(a3, a3_task)


# --- 5. server-shutdown failure (instance_id=None) is never retried ------------------------

@pytest.mark.asyncio
async def test_server_shutdown_failure_is_not_retried(server):
    a = FakeWS(peer=("127.0.0.1", 1111))
    a_task = await connect(server, a)
    await hello(server, a, instanceId="ff-x")
    browser_a = server.browsers["ff-x"]

    call = call_action(server, "req-1", browser_a, "tabs.list")
    await wait_until_async(lambda: a.sent, "the list request reached the browser")

    # A server-wide shutdown fails everything with NO instance_id -> no transient tag, so
    # even an idempotent action is returned straight to the caller, never retried.
    server.rpc.fail_pending_requests("Server shutting down", instance_id=None)

    result = await asyncio.wait_for(call, DEFAULT_TIMEOUT)
    assert result == {"error": "Server shutting down"}, \
        "the shutdown path keeps the plain envelope (no transient flag, no retry)"

    await disconnect(a, a_task)
