"""Doubles for the proxy transport's tests (server/proxy/), shared by test_proxy_*.py.

Two backends stand behind a ProxyServer here, and neither opens a port:

  ASGIBackend   the real server.proxy.backend.Backend, every session going through
                httpx2.ASGITransport into a real BrowserPlugMCPServer app (the SDK session
                manager, streamable HTTP, SSE). It records each HTTP request the backend
                leg made, so a test can count sessions, read headers and see the DELETEs.
  FakeBackend   the same five coroutines, scripted, for tests about the front alone.

`backend_tools()` is the tool table behind ASGIBackend: a clean tool, one that returns a
degraded result, one that raises, and one that returns an image block, the shapes the
real tools put on the wire.

`DelayedAnswers` holds back the backend's answer to one JSON-RPC method, and
`live_sessions` counts the sessions a backend app still holds: together they show whether
a backend leg that gave up on a request left its session behind.
"""

import json
from contextlib import asynccontextmanager

import anyio
import httpx2
import mcp_types

from server.proxy import policy
from server.proxy.backend import Backend
from server.tools.mcp_server import BrowserPlugMCPServer
from server.tools.tool_registry import ToolRegistry, ToolSpec

BACKEND_URL = "http://127.0.0.1:2830/mcp"
BACKEND_INSTRUCTIONS = "Backend instructions, forwarded by the proxy."
DEGRADED_META = {"browserplug/degraded": {"reason": "fixture"}}


def config(backend_url=BACKEND_URL, **over):
    values = dict(backend_url=backend_url, client_pid=4242, launcher_pid=5151,
                  launcher_version="9.9.9")
    values.update(over)
    return policy.ProxyConfig(**values)


async def echo(text: str = "pong") -> str:
    """Echo `text`."""
    return text


async def degraded() -> mcp_types.CallToolResult:
    """A result that succeeded but degraded."""
    return mcp_types.CallToolResult(
        content=[mcp_types.TextContent(type="text", text="partly")], meta=DEGRADED_META)


async def boom() -> str:
    """Always raises."""
    raise RuntimeError("kaboom detail")


async def picture() -> list:
    """An image block after a text block."""
    return [mcp_types.TextContent(type="text", text="shot"),
            mcp_types.ImageContent(type="image", data="aGk=", mimeType="image/png")]


def backend_tools():
    return [echo, degraded, boom, picture]


def backend_server(functions=None, instructions=BACKEND_INSTRUCTIONS):
    registry = ToolRegistry([ToolSpec.from_function(fn) for fn in (functions or backend_tools())])
    return BrowserPlugMCPServer(registry, instructions=instructions)


class _Recorded:
    """An ASGI wrapper that notes (method, path, headers) of every HTTP request that
    reaches the app.

    It first yields to the event loop, as a request crossing a socket does. ASGITransport
    runs the app in the client's own task, so without that a request sent from a cancelled
    scope (the DELETE of a session torn down with its caller) ran until the app's first
    real suspension and was served; over TCP it never leaves the client."""

    def __init__(self, app, log):
        self.app = app
        self.log = log

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            await anyio.sleep(0)
            headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                       for k, v in scope.get("headers") or []}
            self.log.append((scope["method"], scope["path"], headers))
        await self.app(scope, receive, send)


class DelayedAnswers:
    """An ASGI wrapper that delays the response to every POST carrying JSON-RPC `method`,
    after the app has handled it: the server has acted (a session exists, a list was
    read), the client has not heard. `reached` is set when the first such answer is held."""

    def __init__(self, app, method, seconds):
        self.app = app
        self.method = method
        self.seconds = seconds
        self.reached = anyio.Event()

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or scope.get("method") != "POST":
            return await self.app(scope, receive, send)
        messages, body, more = [], b"", True
        while more:
            message = await receive()
            messages.append(message)
            body += message.get("body", b"")
            more = message.get("more_body", False)
        try:
            method = json.loads(body).get("method")
        except (ValueError, AttributeError):
            method = None

        async def replay():
            return messages.pop(0) if messages else await receive()

        async def held(message):
            if message["type"] == "http.response.start":
                self.reached.set()
                await anyio.sleep(self.seconds)
            await send(message)

        await self.app(scope, replay, held if method == self.method else send)


def live_sessions(server):
    """How many streamable HTTP sessions `server` (a BrowserPlugMCPServer whose http_app is
    running) still holds. Read from the SDK session manager's private table: nothing public
    counts them, and a session nobody DELETEs stays there for the server's life."""
    return len(server.session_manager._server_instances)


class _ServedApart(httpx2.AsyncBaseTransport):
    """httpx2.ASGITransport with the app run in `group`, a task group of the server side's
    own, as an app behind a socket runs. A client that gives up stops waiting; the server
    finishes the request regardless, and a session it made stays made. In the client's
    task, as ASGITransport runs it, the client's cancellation would cancel the server's
    handling too and clean up after it, which no server on the far side of TCP does."""

    def __init__(self, inner, group):
        self.inner = inner
        self.group = group

    async def handle_async_request(self, request):
        await anyio.sleep(0)            # a request from a cancelled scope never leaves
        done = anyio.Event()
        outcome = {}

        async def serve():
            try:
                outcome["response"] = await self.inner.handle_async_request(request)
            except Exception as error:
                outcome["error"] = error
            finally:
                done.set()

        self.group.start_soon(serve)
        await done.wait()
        if "error" in outcome:
            raise outcome["error"]
        if "response" not in outcome:
            raise httpx2.RemoteProtocolError("the test server shut down mid-request")
        return outcome["response"]


class ASGIBackend(Backend):
    """The real Backend with its HTTP going into an in-process app instead of a socket.

    With `serve_group`, every request is served in that task group (_ServedApart), apart
    from the client's tasks; without it, in the client's task, as httpx2 does."""

    def __init__(self, app, cfg=None, serve_group=None):
        self.requests = []
        recorded = _Recorded(app, self.requests)

        def transport():
            inner = httpx2.ASGITransport(app=recorded)
            return _ServedApart(inner, serve_group) if serve_group is not None else inner

        super().__init__(cfg or config(), transport_factory=transport)
        self.clients = []

    def http_client(self):
        client = super().http_client()
        self.clients.append(client)
        return client

    def sessions(self):
        """The Mcp-Session-Id of every session the backend leg closed (DELETE), in order."""
        return [h.get("mcp-session-id") for m, _p, h in self.requests if m == "DELETE"]


class FakeBackend:
    """Scripted stand-in for server.proxy.backend.Backend."""

    def __init__(self, tools=None, call_result=None, probe=None, url=BACKEND_URL):
        self.url = url
        self.calls = []
        self.tool_list = list(tools if tools is not None else
                              [{"name": "echo", "description": "Echo.",
                                "inputSchema": {"type": "object", "properties": {}}}])
        self.call_result = call_result or {
            "content": [{"type": "text", "text": "hi"}], "isError": False}
        self._probe = probe
        self.sent = []              # every request send() forwarded, as sent

    @asynccontextmanager
    async def running(self):
        yield

    async def probe(self):
        self.calls.append(("probe",))
        if self._probe is not None:
            return await self._probe()
        return mcp_types.InitializeResult(
            protocol_version="2025-11-25", capabilities=mcp_types.ServerCapabilities(),
            server_info=mcp_types.Implementation(name="backend", version="0"),
            instructions=BACKEND_INSTRUCTIONS)

    async def list_all(self, method, key):
        self.calls.append(("list", method))
        if isinstance(self.tool_list, Exception):
            raise self.tool_list
        return list(self.tool_list) if method == "tools/list" else []

    async def call_tool(self, name, arguments, meta):
        self.calls.append(("call", name, arguments, meta))
        if isinstance(self.call_result, BaseException):
            raise self.call_result
        return self.call_result

    async def send(self, request):
        self.calls.append(("request", request.method))
        self.sent.append(request)
        return {}

    async def ping(self):
        return await self.send(mcp_types.PingRequest())
