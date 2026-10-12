"""Tell MCP clients when the tool surface changes (notifications/tools/list_changed).

The surface changes while the server runs: the `browsers` tool comes and goes with a
second browser, and tabs/navigate gain or lose `proxy`. Each such change ends in
BrowserPlugTools._recompute_tools_signature, which calls
ListChangedNotifier.surface_changed(). One debounced broadcast (list_changed_policy) then
reaches every client that can hear it:

  handshake era  ServerSession.send_tool_list_changed() to every session the middleware
                 recorded. Over Streamable HTTP it lands on the session's GET stream. A
                 session that holds none, such as the stdio relay's, gets nothing, and the
                 SDK drops it without a word; the relay and the proxy transport synthesize
                 their own notice from /health's tools_signature. Over --stdio it is
                 written to the pipe.
  2026-07-28     a ToolsListChanged event on the binding's subscription bus. The SDK's
                 ListenHandler stamps it onto every open subscriptions/listen stream that
                 asked for tool changes. A bare notification is out of spec at this era,
                 and the SDK drops it.

Threads. Changes arrive on the WebSocket loop's thread. Sessions and listen streams live
on the loop of the transport serving them: uvicorn's thread over HTTP, the WebSocket loop
under --stdio, a test's own loop in-process. A transport binds the notifier to its loop
for its lifetime (`running()`). surface_changed() only reads the binding list and calls
call_soon_threadsafe. The debounce, the broadcast, the bus and the listen streams of a
binding never leave its loop.

Shutdown. A listen stream is a response that never ends on its own, and uvicorn waits for
every open response before its thread returns. lifecycle.stop_mcp_server therefore calls
close_streams() before it sets should_exit. That close latches: uvicorn serves requests
until its next tick after should_exit, so a client that re-listens the moment its stream
ends is refused (CLOSING_TEXT) instead of being given a stream nothing would end. The exit
of running() closes and latches the streams as well, which is all --stdio and in-process
connections need.

Containment. A broadcast shares the transport's task group, and an exception that escaped
it would cancel the group's host task, which for HTTP is the Starlette lifespan: the SDK
session manager would unwind and every later /mcp request would answer 500 while /health
stays up. A notification feature must not be able to do that, so a failed broadcast is
logged and dropped.

Only public SDK API: the ServerRequestContext fields, ServerSession.send_tool_list_changed,
ListenHandler, InMemorySubscriptionBus and ToolsListChanged. The SDK exposes no "session
ended" signal (Connection.exit_stack is reachable only through a private attribute), so
SessionEndLayer infers the end from a DELETE or a 404.
"""

import asyncio
import hashlib
import json
import logging
import threading
from contextlib import asynccontextmanager
from typing import Any, Callable, List, Optional, Tuple

import anyio
import mcp_types
from mcp.server.subscriptions import InMemorySubscriptionBus, ListenHandler, ToolsListChanged
from mcp.shared.exceptions import MCPError
from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS

from server.tools import list_changed_policy as policy

logger = logging.getLogger(__name__)

# How long one session's push may take before the broadcast stops waiting for it. A
# client that stopped reading its GET stream blocks that session's writer once the
# buffers fill; the other sessions are pushed concurrently and do not wait.
SEND_TIMEOUT_SECONDS = 5.0

SESSION_HEADER = "mcp-session-id"
_SESSION_HEADER_BYTES = SESSION_HEADER.encode("latin-1")

NOT_BOUND_TEXT = ("subscriptions/listen is not served on this connection: no BrowserPlug "
                  "transport publishes tool-list changes on its event loop")
CLOSING_TEXT = ("subscriptions/listen is not served: the server is shutting down; listen "
                "again on the next server")

# Every live binding of every notifier in this process, newest last. Replaced whole under
# the lock and read without it. lifecycle.stop_mcp_server holds no notifier, so
# close_streams() finds the bindings of the uvicorn thread it is stopping here.
_live_lock = threading.Lock()
_live: Tuple["_Binding", ...] = ()


def _register(binding: "_Binding") -> None:
    global _live
    with _live_lock:
        _live = _live + (binding,)


def _unregister(binding: "_Binding") -> None:
    global _live
    with _live_lock:
        _live = tuple(b for b in _live if b is not binding)


def close_streams(thread: Optional[threading.Thread]) -> None:
    """End every subscriptions/listen stream served on `thread`, gracefully, from any
    thread. Each stream then sends its final result and its response completes, so a
    uvicorn told to exit right after is not held open by it."""
    for binding in _live:
        if thread is not None and binding.thread is thread:
            try:
                binding.close_streams_threadsafe()
            except Exception:
                # The caller goes on to stop the server; one binding must not skip that.
                logger.warning("Could not close a binding's listen streams", exc_info=True)


def registry_signature(registry) -> Callable[[], Optional[str]]:
    """A content hash of the tools `registry` advertises: the signature of a bare
    BrowserPlugMCPServer. BrowserPlugTools passes its tools_signature instead, the value
    /health shows and the launchers poll, so every channel agrees on what a change is."""
    def signature() -> Optional[str]:
        wire = json.dumps(registry.wire_tools(), sort_keys=True, default=str)
        return hashlib.sha1(wire.encode("utf-8")).hexdigest()[:12]
    return signature


def book_key(ctx, connection_key: Optional[str]) -> Optional[str]:
    """The session `ctx` belongs to: its Mcp-Session-Id over HTTP, else the connection
    token the server lifespan put under `connection_key` (stdio, in-process).

    Read off the request itself, never off request_context: an app served without
    HttpEndpoints in front would otherwise file every HTTP session under the session
    manager's one lifespan token."""
    request = ctx.request
    if request is not None:
        headers = getattr(request, "headers", None)
        return headers.get(SESSION_HEADER) if headers is not None else None
    lifespan = ctx.lifespan_context
    if connection_key is None or not isinstance(lifespan, dict):
        return None
    return lifespan.get(connection_key)


async def _push(session) -> None:
    with anyio.move_on_after(SEND_TIMEOUT_SECONDS) as scope:
        try:
            await session.send_tool_list_changed()
        except Exception as e:
            logger.debug("tools/list_changed was not sent to a session: %r", e)
    if scope.cancelled_caught:
        logger.debug("a session did not take tools/list_changed within %.0f s",
                     SEND_TIMEOUT_SECONDS)


class _Binding:
    """One transport's hold on a notifier: its loop, debounce, broadcasts and listen
    streams. Everything but close_streams_threadsafe runs on `loop`."""

    def __init__(self, notifier: "ListChangedNotifier", loop, thread, group):
        self.notifier = notifier
        self.loop = loop
        self.thread = thread
        self.group = group
        self.gate = policy.ChangeGate(policy.QUIET_SECONDS, policy.MAX_WAIT_SECONDS)
        self.bus = InMemorySubscriptionBus()
        self.listen = ListenHandler(self.bus)
        self.active = True
        self.closing = False        # latched by the close that precedes a stop
        self._timer = None
        self._broadcasting = False
        self._again = False

    def on_change(self, signature) -> None:
        if not self.active:
            return
        delay = self.gate.change(signature, self.loop.time())
        if delay is None:
            return
        if self._timer is not None:
            self._timer.cancel()
        self._timer = self.loop.call_later(delay, self._due)

    def _due(self) -> None:
        self._timer = None
        if not self.active or not self.gate.settle():
            return
        # One broadcast at a time: a deadline that passes during one makes it run again.
        if self._broadcasting:
            self._again = True
            return
        self._broadcasting = True
        try:
            self.group.start_soon(self._broadcast)
        except Exception:
            self._broadcasting = False
            logger.warning("Could not start a tools/list_changed broadcast", exc_info=True)

    async def _broadcast(self) -> None:
        try:
            while True:
                self._again = False
                try:
                    await self._announce()
                except Exception:
                    # Never let this reach the task group: it would cancel the transport.
                    logger.warning("tools/list_changed broadcast failed", exc_info=True)
                if not self._again:
                    return
        finally:
            self._broadcasting = False

    async def _announce(self) -> None:
        sessions = self.notifier.book.on_loop(self.loop)
        async with anyio.create_task_group() as group:
            for _key, session in sessions:
                group.start_soon(_push, session)
        await self.bus.publish(ToolsListChanged())
        logger.info("Tool surface changed (tools_signature %s): tools/list_changed pushed "
                    "to %d session(s) and published to subscriptions/listen streams",
                    self.notifier.current_signature(), len(sessions))

    def stop(self) -> None:
        self.active = False
        self.closing = True
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        self.listen.close()

    def _close_streams(self) -> None:
        # Latch and close in one loop callback: a listen handled before it is closed here,
        # one handled after it is refused (ListChangedNotifier.listen).
        self.closing = True
        self.listen.close()

    def close_streams_threadsafe(self) -> None:
        try:
            self.loop.call_soon_threadsafe(self._close_streams)
        except RuntimeError:
            pass        # the loop is closed, and its streams with it


class ListChangedNotifier:
    """Pushes notifications/tools/list_changed and publishes ToolsListChanged.

    `signature` names the current tool surface: the debounce announces only when it
    moved. `connection_key` is where the server lifespan keeps the connection token that
    keys stdio and in-process sessions (mcp_server._CONNECTION_KEY)."""

    def __init__(self, signature: Callable[[], Any], *, connection_key: Optional[str] = None):
        self._signature = signature
        self._connection_key = connection_key
        self.book = policy.SessionBook()

    def current_signature(self) -> Any:
        try:
            return self._signature()
        except Exception:
            return None

    def _bindings(self) -> List[_Binding]:
        """This notifier's live bindings, newest first, at most one per event loop (a
        second binding on a loop would push twice to the same sessions)."""
        found, loops = [], set()
        for binding in reversed(_live):
            if binding.notifier is self and binding.active and binding.loop not in loops:
                loops.add(binding.loop)
                found.append(binding)
        return found

    # ---- any thread --------------------------------------------------------------------

    def surface_changed(self) -> None:
        """The tool surface may have changed. Any thread; never blocks, never raises.

        Reads the signature now, on the caller's thread, right after the registry swap:
        read later on the transport's loop, a change undone in between would look like
        no change at all. A no-op while no transport is bound."""
        try:
            bindings = self._bindings()
            if not bindings:
                return
            signature = self._signature()
            for binding in bindings:
                try:
                    binding.loop.call_soon_threadsafe(binding.on_change, signature)
                except RuntimeError:
                    pass    # that loop closed between the read and the call
        except Exception:
            logger.warning("Could not schedule tools/list_changed", exc_info=True)

    def close_streams(self) -> None:
        """End every subscriptions/listen stream of this notifier gracefully. Any thread."""
        for binding in self._bindings():
            binding.close_streams_threadsafe()

    def forget(self, key: Optional[str]) -> None:
        """`key`'s session ended: stop pushing to it."""
        if key:
            self.book.forget(key)

    def session_count(self) -> int:
        """How many handshake-era sessions a broadcast would push to, on every loop."""
        return len(self.book)

    # ---- the transport's loop ----------------------------------------------------------

    @asynccontextmanager
    async def running(self):
        """Bind to the running loop for the transport's lifetime. Entered by the HTTP
        app's lifespan (install) and by run_stdio. On exit the binding's listen streams
        close, its pending announcement is dropped, and the sessions recorded on its loop
        are forgotten unless another binding of this notifier serves that loop."""
        loop = asyncio.get_running_loop()
        async with anyio.create_task_group() as group:
            binding = _Binding(self, loop, threading.current_thread(), group)
            binding.gate.reset(self.current_signature())
            _register(binding)
            try:
                yield binding
            finally:
                _unregister(binding)
                binding.stop()
                if not any(b.loop is loop for b in self._bindings()):
                    self.book.forget_loop(loop)
                group.cancel_scope.cancel()

    async def middleware(self, ctx, call_next):
        """Server middleware: record each handshake-era session for pushes.

        Before call_next and without awaiting anything, so a session is on the book before
        it can list tools, and `initialize`, which the SDK runs with the read loop parked,
        is never held up. Never fails a request."""
        try:
            self._note(ctx)
        except Exception:
            logger.debug("Could not record an MCP session for tools/list_changed",
                         exc_info=True)
        return await call_next(ctx)

    def _note(self, ctx) -> None:
        # The first message after the handshake (notifications/initialized) is the first
        # one recorded; a 2026-07-28 connection hears changes on subscriptions/listen.
        if ctx.method == "initialize" or ctx.protocol_version not in HANDSHAKE_PROTOCOL_VERSIONS:
            return
        session = ctx.session
        if session is None or session.client_params is None:
            return
        key = book_key(ctx, self._connection_key)
        if key:
            self.book.note(key, session, asyncio.get_running_loop())

    async def listen(self, ctx, params) -> mcp_types.SubscriptionsListenResult:
        """The subscriptions/listen handler. Served by the binding on the request's own
        loop, the only loop that binding's bus publishes on."""
        loop = asyncio.get_running_loop()
        for binding in self._bindings():
            if binding.loop is loop:
                if binding.closing:
                    raise MCPError(code=mcp_types.INTERNAL_ERROR, message=CLOSING_TEXT)
                return await binding.listen(ctx, params)
        raise MCPError(code=mcp_types.INTERNAL_ERROR, message=NOT_BOUND_TEXT)


class SessionEndLayer:
    """ASGI layer: a DELETE answered with a 2xx, or any 404, for a request that carries
    Mcp-Session-Id ends that session on the book. A 404 is how the SDK answers a session
    it no longer has. Forgetting a live session by mistake costs nothing lasting: its next
    message records it again."""

    def __init__(self, app, notifier: ListChangedNotifier):
        self.app = app
        self.notifier = notifier

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        session_id = None
        for name, value in scope.get("headers") or ():
            if name == _SESSION_HEADER_BYTES:
                session_id = value.decode("latin-1")
                break
        if session_id is None:
            await self.app(scope, receive, send)
            return
        status = None

        async def watch(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, watch)
        finally:
            if status == 404 or (scope.get("method") == "DELETE" and status is not None
                                 and 200 <= status < 300):
                self.notifier.forget(session_id)


def install(app, notifier: ListChangedNotifier) -> None:
    """Run `notifier` inside the Starlette app's lifespan (on the loop uvicorn serves it
    from) and add SessionEndLayer. Call it before HostOriginLayer is added: Starlette makes
    the last added layer the outermost, so a request that layer refuses never reaches this
    one."""
    serve = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(asgi_app):
        async with serve(asgi_app) as state, notifier.running():
            yield state

    app.router.lifespan_context = lifespan
    app.add_middleware(SessionEndLayer, notifier=notifier)
