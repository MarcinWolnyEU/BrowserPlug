"""Review finding S8: no request body is drained without a cap, and no token-gated body is
read before the token check.

The bodies here are streamed by a fake `receive` that counts what the server pulled, so the
tests see how much of a hostile body the server was willing to read, not only the status.
"""

import json
import socket
import types

import pytest

from secure_ext import signed_headers
from server.web import http_endpoints
from server.server import BrowserPlugServer

MIB = 1024 * 1024
CHUNK = 64 * 1024
# The pinned caps. getattr keeps the red run about behaviour, not a missing name.
MAX_CONTROL = getattr(http_endpoints, "MAX_CONTROL_BODY", 64 * 1024)
MAX_MCP = getattr(http_endpoints, "MAX_MCP_BODY", 16 * MIB)
LOCAL_PEER = ("127.0.0.1", 5555)


def _server():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    return BrowserPlugServer(host="localhost", port=port, start_mcp=False,
                             ws_token="a" * 43)


class Stream:
    """A request body of `total` bytes delivered in 64 KiB chunks; `pulled` counts them."""

    def __init__(self, total):
        self.total = total
        self.pulled = 0

    async def receive(self):
        n = min(CHUNK, self.total - self.pulled)
        self.pulled += n
        return {"type": "http.request", "body": b"x" * n,
                "more_body": self.pulled < self.total}


async def post(app, path, stream, headers=None, declare=False):
    sent = []

    async def send(message):
        sent.append(message)

    hdrs = list(headers or [])
    if declare:
        hdrs.append((b"content-length", str(stream.total).encode()))
    await app({"type": "http", "method": "POST", "path": path, "headers": hdrs,
               "client": LOCAL_PEER}, stream.receive, send)
    start = next((m for m in sent if m["type"] == "http.response.start"), None)
    payload = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return (start["status"] if start else None), payload


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/ws-bind", "/reset-config"])
async def test_an_unsigned_token_route_reads_none_of_the_body(path):
    server = _server()
    app = server._wrap_client_tracking(lambda *a: None)
    stream = Stream(5 * MIB)
    status, _ = await post(app, path, stream)
    assert status == 403
    assert stream.pulled == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/timing", "/ws-bind", "/reset-config", "/shutdown",
                                  "/restart"])
async def test_a_signed_oversized_body_is_refused_413_after_a_bounded_read(path, monkeypatch):
    # /shutdown ends the process from a timer thread when the handler is reached (the
    # bug), so keep a red run from killing pytest.
    monkeypatch.setattr(http_endpoints, "threading", types.SimpleNamespace(
        Thread=lambda *a, **k: types.SimpleNamespace(start=lambda: None)))
    server = _server()
    server.restarts = []
    server._begin_restart = server.restarts.append
    server._restart_onto = server.restarts.append
    app = server._wrap_client_tracking(lambda *a: None)
    stream = Stream(8 * MIB)
    status, payload = await post(app, path, stream,
                                 headers=signed_headers(server, "POST", path))
    assert status == 413, payload
    assert stream.pulled <= MAX_CONTROL + CHUNK
    assert server.restarts == []


@pytest.mark.asyncio
async def test_a_declared_oversized_length_is_refused_without_reading():
    server = _server()
    app = server._wrap_client_tracking(lambda *a: None)
    stream = Stream(8 * MIB)
    status, _ = await post(app, "/timing", stream, declare=True,
                           headers=signed_headers(server, "POST", "/timing"))
    assert status == 413
    assert stream.pulled == 0


@pytest.mark.asyncio
async def test_a_small_signed_body_still_works():
    server = _server()
    app = server._wrap_client_tracking(lambda *a: None)
    stream = Stream(1024)
    status, payload = await post(app, "/timing", stream, declare=True,
                                 headers=signed_headers(server, "POST", "/timing",
                                                        body=b"x" * 1024))
    assert status == 200
    assert json.loads(payload) == {"matched": False}


@pytest.mark.asyncio
async def test_the_mcp_recorder_stops_buffering_an_oversized_body():
    server = _server()
    reached = []

    async def inner(scope, receive, send):
        reached.append(scope["path"])

    app = server._wrap_client_tracking(inner)
    stream = Stream(MAX_MCP + 8 * MIB)
    status, _ = await post(app, "/mcp/", stream, headers=[(b"mcp-session-id", b"s1")])
    assert status == 413
    assert reached == []
    assert stream.pulled <= MAX_MCP + CHUNK


@pytest.mark.asyncio
async def test_the_mcp_recorder_still_passes_a_large_legitimate_call():
    server = _server()
    seen = []

    async def inner(scope, receive, send):
        total = 0
        while True:
            m = await receive()
            total += len(m.get("body", b""))
            if not m.get("more_body", False):
                break
        seen.append(total)

    app = server._wrap_client_tracking(inner)
    stream = Stream(4 * MIB)
    await post(app, "/mcp/", stream, headers=[(b"mcp-session-id", b"s1")])
    assert seen == [4 * MIB]


@pytest.mark.asyncio
async def test_read_body_raises_at_the_cap():
    stream = Stream(1 * MIB)
    with pytest.raises(http_endpoints.BodyTooLarge):
        await http_endpoints.read_body(stream.receive, limit=100 * 1024)
    assert stream.pulled <= 100 * 1024 + CHUNK
