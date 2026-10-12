"""
How a restart shows up in a tool's recorded outcome.

A call refused because the server is restarting used to carry no code, so it classified as
PAGE_ERROR and was not retryable even though its text says a retry is safe. Nothing was
sent to the browser, so a retry is safe for every action, not only for the read-only ones.
"""

import pytest

import server.runtime.graceful_restart as gr
import server.rpc.request_retry as request_retry
import server.tools.tool_outcome as tool_outcome
from server.server import BrowserPlugServer
from server.tools.tool_outcome import arm, classify, fail, read
from server.tools.browser_calls import BrowserCalls


@pytest.fixture(autouse=True)
def clean_outcome():
    arm()
    yield
    arm()


@pytest.fixture
def server():
    return BrowserPlugServer(host="localhost", port=8772, start_mcp=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["content.snapshot", "content.act", "navigation.navigate"])
async def test_a_refused_call_carries_a_restart_code_and_is_retryable(server, action):
    server.rpc.set_restarting(True)
    envelope = await server.send_request_and_wait(
        {"id": "r1", "type": "request", "action": action, "data": {}}, timeout=1.0)
    assert envelope["error"] == gr.RESTARTING_REFUSAL
    assert envelope["code"] == tool_outcome.SERVER_RESTARTING
    assert envelope["retryable"] is True


class _Stub:
    """The bits of the server BrowserCalls touches, answering with a fixed envelope."""
    _mcp_call = None
    extension_version = "x"
    routing_memory = object()
    http_clients = {}

    def __init__(self, envelope):
        self.envelope = envelope

    def resolve_target_browser(self, key):
        return None

    async def send_request_and_wait(self, request, timeout=30.0, browser=None):
        return dict(self.envelope)


@pytest.mark.asyncio
async def test_the_refusal_reaches_the_recorded_failure(server):
    server.rpc.set_restarting(True)
    envelope = await server.send_request_and_wait(
        {"id": "r1", "type": "request", "action": "content.act", "data": {}}, timeout=1.0)
    calls = BrowserCalls(_Stub(envelope))
    response = await calls.request("content.act", {})
    fail(f"Error executing steps: {calls.error_of(response)}")
    outcome = read()
    assert outcome["code"] == tool_outcome.SERVER_RESTARTING
    assert outcome["retryable"] is True


def test_the_refusal_text_alone_classifies_as_a_restart():
    assert classify(gr.RESTARTING_REFUSAL) == tool_outcome.SERVER_RESTARTING


def test_the_wire_constant_matches_the_tool_outcome_constant():
    assert request_retry.CODE_RESTARTING == tool_outcome.SERVER_RESTARTING


def test_the_in_flight_restart_failure_is_a_disconnect_retryable_only_for_reads():
    read_only = request_retry.with_disconnect_advice(
        {"error": gr.PENDING_REASON, "code": request_retry.CODE_DISCONNECTED}, "content.read")
    mutating = request_retry.with_disconnect_advice(
        {"error": gr.PENDING_REASON, "code": request_retry.CODE_DISCONNECTED}, "content.act")
    assert read_only["retryable"] is True
    assert mutating["retryable"] is False
    assert classify(gr.PENDING_REASON) == tool_outcome.EXTENSION_DISCONNECTED
