"""
The retry claims a failure makes must be true.

Two lies this file pins shut:
  * After a socket drop the agent was told any call "is safe to retry", including a click
    or a script that may already have run in the page.
  * `retryable` was derived from the message text, so every "timed out after" counted as
    retryable, including the queue's own "Do NOT retry immediately" and page_act/page_eval
    timeouts. It is now data carried from the site that knows (extension_rpc), and only an
    action on the read-only allowlist in request_retry.py can be retryable.

The disconnect tests drive the real connection handler with fake sockets, like
test_disconnect_retry.py.
"""

import asyncio

import pytest

import server.rpc.request_retry as request_retry
from server.browsers.request_queue import Ticket
from server.server import BrowserPlugServer
from server.tools.tool_outcome import (EXTENSION_DISCONNECTED, EXTENSION_SEND_FAILED, TIMEOUT,
                                       arm, classify, fail, read)
from server.tools.browser_calls import BrowserCalls

from test_disconnect_retry import (FakeWS, connect, disconnect, hello, wait_until)  # noqa: F401


@pytest.fixture
def server():
    return BrowserPlugServer(host="localhost", port=8772, start_mcp=False)


@pytest.fixture(autouse=True)
def fast_grace(monkeypatch):
    monkeypatch.setattr(request_retry, "RECONNECT_GRACE_SECONDS", 0.1)


@pytest.fixture(autouse=True)
def clean_outcome():
    arm()
    yield
    arm()


async def _dropped_call(server, action):
    ws = FakeWS(peer=("127.0.0.1", 1111))
    task = await connect(server, ws)
    await hello(server, ws, instanceId="ff-x")
    browser = server.browsers["ff-x"]
    call = asyncio.create_task(server.send_request_and_wait(
        {"id": "req-1", "type": "request", "action": action, "data": {}},
        timeout=5.0, browser=browser))
    await wait_until(lambda: ws.sent, "the request reached the browser")
    await disconnect(ws, task)
    return await asyncio.wait_for(call, 3.0)


# --- disconnect wording is per action -------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["content.act", "content.execute_script",
                                    "navigation.navigate"])
async def test_a_dropped_mutating_call_is_not_called_safe_to_retry(server, action):
    result = await _dropped_call(server, action)
    text = result["error"]
    assert "safe to retry" not in text
    assert "may have already run" in text
    assert "check" in text.lower()
    assert result["retryable"] is False
    assert result["code"] == EXTENSION_DISCONNECTED


@pytest.mark.asyncio
async def test_a_dropped_read_is_still_called_safe_to_retry(server):
    result = await _dropped_call(server, "content.snapshot")
    assert "safe to retry" in result["error"]
    assert "may have already run" not in result["error"]
    assert result["retryable"] is True
    assert result["code"] == EXTENSION_DISCONNECTED


def test_the_disconnect_texts_classify_as_a_disconnect_not_a_page_error():
    assert classify("Firefox disconnected before responding (Firefox closed).") \
        == EXTENSION_DISCONNECTED
    from server.runtime.graceful_restart import PENDING_REASON
    assert classify(PENDING_REASON) == EXTENSION_DISCONNECTED


# --- timeouts: retryable is data, and agrees with the words ---------------------------------

async def _timed_out_call(server, action, ticket_kwargs=None):
    ws = FakeWS(peer=("127.0.0.1", 1111))
    task = await connect(server, ws)
    await hello(server, ws, instanceId="ff-x")
    browser = server.browsers["ff-x"]
    result = await server.send_request_and_wait(
        {"id": "req-t", "type": "request", "action": action, "data": {}},
        timeout=0.05, browser=browser)
    await disconnect(ws, task)
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["content.act", "content.execute_script",
                                    "navigation.navigate"])
async def test_a_timed_out_mutating_call_is_not_retryable(server, action):
    result = await _timed_out_call(server, action)
    assert "timed out after" in result["error"]
    assert result["code"] == TIMEOUT
    assert result["retryable"] is False


@pytest.mark.asyncio
async def test_a_timed_out_read_is_retryable(server):
    result = await _timed_out_call(server, "content.snapshot")
    assert result["code"] == TIMEOUT
    assert result["retryable"] is True


@pytest.mark.asyncio
async def test_a_contended_timeout_that_says_do_not_retry_is_not_retryable(server, monkeypatch):
    contended = Ticket(browser_key="ff-x", peak_inflight=4, queue_wait=2.5, limit=3)
    real_gate_for = server.request_queue.gate_for

    async def acquire(action=None):
        return contended
    gate = real_gate_for("ff-x")
    monkeypatch.setattr(gate, "acquire", acquire)
    monkeypatch.setattr(server.request_queue, "gate_for", lambda key: gate)
    result = await _timed_out_call(server, "content.snapshot")   # a READ, on purpose
    assert "Do NOT retry immediately" in result["error"]
    assert result["retryable"] is False


# --- tool_outcome: no more sniffing retryable out of prose ----------------------------------

def test_fail_does_not_derive_retryable_from_the_text():
    fail("Error executing steps: Request timed out after 30 seconds")
    assert read()["code"] == TIMEOUT
    assert read()["retryable"] is False


def test_fail_takes_retryable_as_data():
    fail("Error reading page: whatever", retryable=True)
    assert read()["retryable"] is True


def test_the_queue_message_can_never_be_marked_retryable():
    fail("Request timed out after 35.0 seconds — CONTENDED ... Do NOT retry immediately")
    assert read()["retryable"] is False


class _StubServer:
    def __init__(self, envelope):
        self.envelope = envelope
        self._mcp_call = None
        self.extension_version = "x"
        self.routing_memory = object()

    def resolve_target_browser(self, key):
        return None

    @property
    def http_clients(self):
        return {}

    async def send_request_and_wait(self, request, timeout=30.0, browser=None):
        return dict(self.envelope)


@pytest.mark.asyncio
async def test_the_wire_verdict_reaches_the_recorded_failure():
    stub = _StubServer({"error": "Request timed out after 5 seconds", "code": TIMEOUT,
                        "retryable": True})
    calls = BrowserCalls(stub)
    response = await calls.request("content.snapshot", {})
    err = calls.error_of(response)
    fail(f"Error taking snapshot: {err}")
    assert read()["retryable"] is True and read()["code"] == TIMEOUT

    stub.envelope = {"error": "Request timed out after 5 seconds", "code": TIMEOUT,
                     "retryable": False}
    response = await calls.request("content.act", {})
    fail(f"Error executing steps: {calls.error_of(response)}")
    assert read()["retryable"] is False


@pytest.mark.asyncio
async def test_a_later_success_clears_the_wire_verdict():
    stub = _StubServer({"error": "boom", "code": TIMEOUT, "retryable": True})
    calls = BrowserCalls(stub)
    await calls.request("content.snapshot", {})
    stub.envelope = {"type": "response", "data": {}}
    await calls.request("content.snapshot", {})
    fail("Error: something unrelated")
    assert read()["retryable"] is False


def test_the_wire_codes_match_the_tool_outcome_constants():
    assert request_retry.CODE_TIMEOUT == TIMEOUT
    assert request_retry.CODE_DISCONNECTED == EXTENSION_DISCONNECTED
    assert request_retry.CODE_SEND_FAILED == EXTENSION_SEND_FAILED
