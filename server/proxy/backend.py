"""The proxy's backend leg: the SDK client, one fresh session per request.

Every request the proxy forwards opens its own streamable HTTP session on the shared
server (initialize, the request, DELETE), as the proxy this replaces did. That is what
makes a server /restart transparent here: the next request simply opens a session on the
replacement, and no session id outlives the server that issued it. It costs a few
loopback round trips per request, measured in milliseconds.

The session is pinned to the handshake era: the 2026-07-28 era sends no Mcp-Session-Id,
and the server's client tracking keys on it. Each session gets its own httpx2 client:
trust_env=False, because the backend is our own loopback server and an HTTP_PROXY in the
environment (with no NO_PROXY for localhost) would route every call to the corporate proxy
and fail one hop after a /health that looked fine; and the client pid as a static header,
because the TCP peer is this process and the server would otherwise key the client by
session id (CLIENT_PID_HEADER).

A dead backend surfaces from the SDK as an exception group around the transport error.
`session()` turns that into one BackendUnreachable whose text is the same whichever way the
failure arrived; an MCPError the backend answered passes through untouched.

Each exchange (open, request, close) runs in a task group that `running()` holds for the
whole stdio connection, not in the handler that asked for it. When the client cancels a
call, the handler's cancel scope would otherwise take the session's transport down with
it: the SDK's courtesy notifications/cancelled is never POSTed, the DELETE in its finally
dies on its first await, the tool keeps running on the server, and the server, which reaps
no idle sessions, keeps the session for good. So a cancelled caller cancels only the scope
around the request; the request's own task sends the cancel, closes the session (DELETE)
and ends. The same holds for a tools/list refresh and for the handshake gate's probe when
its time runs out.
"""

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Optional

import anyio
import httpx2
import mcp.client.streamable_http as _sdk_http
import mcp_types
from anyio.abc import TaskGroup
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from pydantic import TypeAdapter

from browserplug_shared.version import CLIENT_PID_HEADER
from server.proxy import policy

# Results are taken as the wire dict, so a backend result reaches the client field for
# field (content, isError, structuredContent, _meta) without a model in between.
_RAW = TypeAdapter(Dict[str, Any])

# `_meta` keys that describe one negotiated connection. They never cross the proxy: the
# backend session negotiates its own, and a handshake-era backend must not receive them.
CONNECTION_META_KEYS = frozenset({
    mcp_types.PROTOCOL_VERSION_META_KEY,
    mcp_types.CLIENT_INFO_META_KEY,
    mcp_types.CLIENT_CAPABILITIES_META_KEY,
})


class BackendUnreachable(RuntimeError):
    """The backend leg failed below MCP. str() is policy.connect_failure_text(cause)."""

    def __init__(self, cause: BaseException):
        super().__init__(policy.connect_failure_text(cause))
        self.cause = cause


# Failures of the connection itself, as opposed to an answer: a refused or reset socket, a
# stream the SDK closed under the request, a read that timed out.
_TRANSPORT_ERRORS = (httpx2.HTTPError, OSError, TimeoutError, anyio.ClosedResourceError,
                     anyio.EndOfStream, anyio.BrokenResourceError)


class _UncappedEventSource(httpx2.EventSource):
    """httpx2.EventSource without its per-event size cap.

    The backend answers every POST as text/event-stream, one event per result, and the SDK
    reads it with `EventSource(response)` (mcp/client/streamable_http.py, in
    StreamableHTTPTransport._handle_sse_response), which takes httpx2's default
    max_event_size of 1 MiB (DEFAULT_MAX_EVENT_SIZE_BYTES). A larger event raises SSEError,
    which the SDK logs at debug level and reports as "SSE stream ended without a response":
    a page_screenshot over about 786 KB raw failed that way. The relay forwards data: lines
    with no limit, and the backend is this checkout's own server, so there is none here
    either."""

    def __init__(self, response, max_event_size=None):
        super().__init__(response, max_event_size=max_event_size)


def lift_sse_event_cap() -> None:
    """Point the SDK's EventSource at _UncappedEventSource. Process-wide, and idempotent:
    the proxy process reads no other SSE.

    The SDK looks the name up as a module global at call time, so rebinding it is the whole
    fix, and it silently stops working if a new SDK constructs the reader any other way.
    Hence the check: the reading function must still name `EventSource` and the global must
    still be httpx2's class (or this one), else the proxy refuses to start rather than fail
    every large result. tests/unit/test_proxy_front.py round-trips a 2 MB image."""
    reader = getattr(_sdk_http.StreamableHTTPTransport, "_handle_sse_response", None)
    names = getattr(getattr(reader, "__code__", None), "co_names", ())
    current = getattr(_sdk_http, "EventSource", None)
    if "EventSource" not in names or current not in (httpx2.EventSource, _UncappedEventSource):
        raise RuntimeError(
            "mcp.client.streamable_http no longer reads SSE through a module-level "
            "`EventSource`, so the proxy cannot lift httpx2's 1 MiB per-event cap and every "
            "tool result above it would fail. Update lift_sse_event_cap in "
            "server/proxy/backend.py for this SDK version.")
    _sdk_http.EventSource = _UncappedEventSource


def _leaves(error: BaseException):
    if isinstance(error, BaseExceptionGroup):
        for inner in error.exceptions:
            yield from _leaves(inner)
    else:
        yield error


def _root_cause(error: BaseException) -> Optional[BaseException]:
    """The first leaf of `error`, or None when any leaf is a cancellation: anyio leaves
    cancellations out of a group unless one is in progress, and that must propagate as
    it is, whatever else failed while it unwound."""
    cancelled = anyio.get_cancelled_exc_class()
    leaves = list(_leaves(error))
    if not leaves or any(isinstance(leaf, cancelled) for leaf in leaves):
        return None
    return leaves[0]


def forwardable_request_meta(meta: Optional[dict]) -> Optional[dict]:
    """The client's request `_meta` minus the connection-owned keys, or None."""
    kept = {k: v for k, v in (meta or {}).items() if k not in CONNECTION_META_KEYS}
    return kept or None


def forwardable_result_meta(result: Dict[str, Any]) -> Dict[str, Any]:
    """`result` with the backend's connection-owned `_meta` keys (and its serverInfo
    stamp) removed, dropping a `_meta` left empty."""
    meta = result.get("_meta")
    if not isinstance(meta, dict):
        return result
    kept = {k: v for k, v in meta.items()
            if k not in CONNECTION_META_KEYS and k != mcp_types.SERVER_INFO_META_KEY}
    if kept == meta:
        return result
    out = {k: v for k, v in result.items() if k != "_meta"}
    if kept:
        out["_meta"] = kept
    return out


class Backend:
    """The shared server as the proxy sees it: a URL and the headers every request carries."""

    def __init__(self, config: policy.ProxyConfig, transport_factory=None):
        """`transport_factory` returns the httpx2 transport for each session's client; None
        (production) is httpx2's own over TCP. Tests pass an ASGI transport."""
        lift_sse_event_cap()
        self.config = config
        self.url = config.backend_url
        self.headers = {CLIENT_PID_HEADER: str(config.client_pid)}
        self._transport_factory = transport_factory
        self._group: Optional[TaskGroup] = None   # held by running()
        self._unfinished: set = set()     # the done events of exchanges still running

    @asynccontextmanager
    async def running(self) -> AsyncIterator[None]:
        """Hold the task group every exchange runs in, for one stdio connection
        (front.ProxyServer.run). Outside it, as in tests that drive a Backend directly, an
        exchange runs in the caller's own task.

        On the way out, exchanges whose callers were cancelled (the stdio session ended
        under them) get policy.BACKEND_DRAIN_SECONDS to send their cancel and DELETE before
        the group is cancelled."""
        async with anyio.create_task_group() as group:
            self._group = group
            try:
                yield
            finally:
                self._group = None
                try:
                    with anyio.move_on_after(policy.BACKEND_DRAIN_SECONDS):
                        for done in list(self._unfinished):
                            await done.wait()
                finally:
                    group.cancel_scope.cancel()

    async def _exchange(self, work: Callable[[ClientSession], Awaitable[Any]]) -> Any:
        """`work(session)` on a backend session of its own; its result, or its exception.

        Under running() the session lives in a task of the backend's group and the caller
        only waits for it. A cancelled caller cancels `call`, the scope around `work` alone:
        the SDK then sends its notifications/cancelled through a transport that is still
        up, and the session closes normally (DELETE). A caller cancelled before the task
        began opens no session at all; one cancelled during the open still opens and then
        closes it, because a session the server has created must be DELETEd to go away.
        httpx2's timeouts bound how long such a task can outlive its caller."""
        group = self._group
        if group is None:
            async with self.session() as session:
                return await work(session)
        call = anyio.CancelScope()
        done = anyio.Event()
        outcome: Dict[str, Any] = {}

        async def exchange():
            try:
                if call.cancel_called:
                    return
                async with self.session() as session:
                    with call:
                        outcome["result"] = await work(session)
            except Exception as error:
                outcome["error"] = error
            finally:
                self._unfinished.discard(done)
                done.set()

        self._unfinished.add(done)
        group.start_soon(exchange)
        try:
            await done.wait()
        except anyio.get_cancelled_exc_class():
            call.cancel()
            raise
        if "error" in outcome:
            raise outcome["error"]
        if "result" not in outcome:
            # Only the group's own cancellation ends an exchange this way, and running()
            # cancels it after the stdio session, and with it every caller, is gone.
            raise BackendUnreachable(RuntimeError("the proxy is shutting down"))
        return outcome["result"]

    def http_client(self) -> httpx2.AsyncClient:
        """A fresh, proxy-blind httpx2 client for one backend session."""
        extra = {}
        if self._transport_factory is not None:
            extra["transport"] = self._transport_factory()
        return httpx2.AsyncClient(
            headers=self.headers,
            timeout=httpx2.Timeout(self.config.timeout, read=self.config.read_timeout),
            # As the old proxy's client had it; the SDK follows same-origin redirects
            # itself and does not read the flag.
            follow_redirects=True,
            trust_env=False,
            **extra)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[ClientSession]:
        """An initialized handshake-era session, closed (DELETE) on exit.

        Raises BackendUnreachable for a transport failure, MCPError for an error the
        backend answered; cancellation propagates unchanged."""
        try:
            async with self.http_client() as http:
                async with streamable_http_client(self.url, http_client=http) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        yield session
        except (MCPError, BackendUnreachable):
            raise
        except BaseException as error:
            cause = _root_cause(error)
            if cause is None or not isinstance(cause, Exception):
                raise          # cancellation, GeneratorExit, KeyboardInterrupt: as they are
            if isinstance(cause, _TRANSPORT_ERRORS):
                raise BackendUnreachable(cause) from cause
            if cause is error:
                raise
            raise cause from None

    async def probe(self) -> mcp_types.InitializeResult:
        """Open and close one session; its InitializeResult (the server's instructions)."""
        async def initialized(session):
            return session.initialize_result

        result = await self._exchange(initialized)
        assert result is not None
        return result

    async def send(self, request: Any) -> Dict[str, Any]:
        """Send one request on a session of its own; the backend's result as the wire dict."""
        return await self._exchange(lambda session: session.send_request(request, _RAW))

    async def list_all(self, method: str, key: str) -> List[Dict[str, Any]]:
        """Every item of a paginated list (tools/list, resources/list, ...), all pages, on
        one session."""
        request_type = _LIST_REQUESTS[method]

        async def every_page(session):
            items: List[Dict[str, Any]] = []
            cursor = None
            while True:
                params = mcp_types.PaginatedRequestParams(cursor=cursor) if cursor else None
                page = await session.send_request(request_type(params=params), _RAW)
                items.extend(page.get(key) or [])
                cursor = page.get("nextCursor")
                if not cursor:
                    return items

        return await self._exchange(every_page)

    async def ping(self) -> Dict[str, Any]:
        return await self.send(mcp_types.PingRequest())

    async def call_tool(self, name: str, arguments: Dict[str, Any],
                        meta: Optional[dict]) -> Dict[str, Any]:
        wire: Dict[str, Any] = {"name": name, "arguments": arguments}
        if meta:
            wire["_meta"] = meta
        params = mcp_types.CallToolRequestParams.model_validate(wire)
        return await self.send(mcp_types.CallToolRequest(params=params))


_LIST_REQUESTS = {
    "tools/list": mcp_types.ListToolsRequest,
    "resources/list": mcp_types.ListResourcesRequest,
    "resources/templates/list": mcp_types.ListResourceTemplatesRequest,
    "prompts/list": mcp_types.ListPromptsRequest,
}
