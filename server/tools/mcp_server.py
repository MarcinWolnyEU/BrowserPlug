"""The MCP server itself: the SDK's low-level `Server`, answering from BrowserPlug's tool table.

The SDK's `Server` owns the protocol (the handshake, JSON-RPC framing, sessions,
cancellation, the Streamable HTTP and stdio transports). Everything an agent reads about a
tool is BrowserPlug's own and lives in `tool_registry.py` (schemas, argument validation,
the error texts) and `tool_outcome.py` (what a recorded failure or degradation means on
the wire). This class wires the two together:

  tools/list   the registry's snapshot, in registration order, one page.
  tools/call   unknown name -> the rename hint or "Unknown tool: 'x'"; argument error ->
               pydantic's report; the tool ran -> ToolOutcomePolicy decides; the tool
               raised -> "Error calling tool 'x': <error>". All of them are RESULTS
               (isError), never JSON-RPC errors: a handler that raises reaches the client
               as a code 0 error with the raw message and logs a traceback to the root
               logger, so nothing may escape the handler except cancellation.
  initialize   serverInfo {"name": "BrowserPlug", "version": <BrowserPlug's version>},
               the instructions, and the capabilities MCP clients have always been sent
               (see HTTP_NOTIFICATIONS / STDIO_NOTIFICATIONS).
  the rest     empty prompt/resource lists, their not-found errors, logging/setLevel
               answered with {}, ping by the SDK.
  list_changed notifications/tools/list_changed when the tool surface changes, and the
               2026-07-28 subscriptions/listen streams that carry it (list_changed.py).

Only public SDK API is used. `list_tools()` and `get_tool(name)` are the test-facing
accessors the unit suite reads tools through (`(await tools.mcp.get_tool(n)).fn`).
"""

import logging
import os
import traceback
import uuid
from contextlib import asynccontextmanager, nullcontext
from typing import Any, AsyncContextManager, AsyncIterator, Callable, Dict, List, Optional

import mcp_types
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.stdio import stdio_server
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import MCPError

from browserplug_shared.version import __version__ as BROWSERPLUG_VERSION

from server.rpc import protocol_era
from server.tools import list_changed
from server.tools.argument_hints import refusal_hints, tool_item_fields, tool_parameters
from server.tools.tool_outcome import ToolOutcomePolicy
from server.tools.tool_registry import (ToolArgumentError, ToolRegistry, ToolSpec, error_result,
                                        unexpected_error_text, unknown_tool_text)
from server.tools.tool_renames import renamed_tool_message
from server.web import mcp_host_origin, request_context

logger = logging.getLogger(__name__)

SERVER_NAME = "BrowserPlug"

# The capabilities HTTP clients have always been sent: tools/prompts/resources listChanged
# true (plus logging {}, which the setLevel handler advertises).
# The server pushes notifications/tools/list_changed on a session's GET stream
# (list_changed.py). The stdio relay holds no GET stream: it forwards this reply verbatim
# and synthesises the notification from /health's tools_signature. A client that honours
# the flag would ignore either if tools.listChanged said false, and the SDK's default is
# false. The Streamable HTTP manager and the in-process test transport never pass options,
# so the default below is what they get.
HTTP_NOTIFICATIONS = NotificationOptions(prompts_changed=True, resources_changed=True,
                                         tools_changed=True)
# `--stdio` has always advertised tools listChanged only (prompts and resources false).
STDIO_NOTIFICATIONS = NotificationOptions(tools_changed=True)

# The request body cap on /mcp: the SDK default the server has always run with (4 MiB),
# pinned so an SDK upgrade cannot move it silently. HttpEndpoints' recorder cap (16 MiB)
# sits outside and only bounds what it buffers.
MAX_REQUEST_BODY = 4 * 1024 * 1024

# Where `lifespan` keeps the connection token (see _call_identity).
_CONNECTION_KEY = "browserplug.connection"

# Bounds on what raise_site() writes for one raising tool: the innermost frames of each
# traceback (a RecursionError would otherwise put a thousand frames on one log line) and
# the exceptions followed down a __cause__/__context__ chain.
_MAX_LOGGED_FRAMES = 20
_MAX_LOGGED_CHAIN = 4


def _frame_locations(exc: BaseException) -> str:
    frames = [f"{os.path.basename(frame.f_code.co_filename)}:{lineno} in {frame.f_code.co_name}"
              for frame, lineno in traceback.walk_tb(exc.__traceback__)]
    if len(frames) > _MAX_LOGGED_FRAMES:
        skipped = len(frames) - _MAX_LOGGED_FRAMES
        frames = [f"({skipped} outer frames)"] + frames[-_MAX_LOGGED_FRAMES:]
    return "; ".join(frames) or "(no traceback)"


def raise_site(exc: BaseException) -> str:
    """Where a tool's exception came from, as the rotating log may record it: the class
    and `file:line in function` for each frame, outermost first, then the same for the
    exceptions it was raised from or while handling.

    Never the message or the args, which can carry page data (a URL, a selector, JS), and
    never source text: file names and line numbers locate the bug without either."""
    parts, seen, link = [], set(), ""
    while exc is not None and id(exc) not in seen and len(parts) <= _MAX_LOGGED_CHAIN:
        seen.add(id(exc))
        parts.append(f"{link}{type(exc).__name__} at {_frame_locations(exc)}")
        if exc.__cause__ is not None:
            exc, link = exc.__cause__, "caused by "
        elif exc.__context__ is not None and not exc.__suppress_context__:
            exc, link = exc.__context__, "while handling "
        else:
            exc = None
    return " | ".join(parts)


@asynccontextmanager
async def _connection_lifespan(server: "BrowserPlugMCPServer") -> AsyncIterator[Dict[str, str]]:
    """The SDK enters this once per stdio or in-process connection (and once for the whole
    HTTP session manager). It only mints a token naming the connection: the WebSocket
    listener and everything else heavy is started by server.py, never per connection.
    The token also keys the connection's tools/list_changed pushes, which end with it."""
    key = f"conn-{uuid.uuid4().hex}"
    try:
        yield {_CONNECTION_KEY: key}
    finally:
        server.list_changed.forget(key)


class BrowserPlugMCPServer(Server):
    """The low-level SDK server with BrowserPlug's tool dispatch. One per BrowserPlugTools."""

    def __init__(self, registry: ToolRegistry, *, instructions: Optional[str] = None,
                 record: Optional[Any] = None, name: str = SERVER_NAME,
                 version: str = BROWSERPLUG_VERSION,
                 signature: Optional[Callable[[], Any]] = None,
                 call_gate: Optional[Callable[[str, Dict[str, Any]],
                                              AsyncContextManager[Any]]] = None):
        # `signature` names the tool surface for list_changed; BrowserPlugTools passes its
        # tools_signature, the value /health shows and the launchers poll.
        # `call_gate(name, arguments)` returns the async context a call runs inside;
        # BrowserPlugTools orders a client's calls around a browser switch with it
        # (call_order.py). It is entered before the first await, so it sees the calls in
        # the order the dispatcher started them.
        self.list_changed = list_changed.ListChangedNotifier(
            signature or list_changed.registry_signature(registry),
            connection_key=_CONNECTION_KEY)
        super().__init__(
            name, version=version, instructions=instructions,
            lifespan=_connection_lifespan,
            on_list_tools=self._on_list_tools,
            on_call_tool=self._on_call_tool,
            on_list_prompts=self._on_list_prompts,
            on_get_prompt=self._on_get_prompt,
            on_list_resources=self._on_list_resources,
            on_list_resource_templates=self._on_list_resource_templates,
            on_read_resource=self._on_read_resource,
            on_subscriptions_listen=self.list_changed.listen)
        # Registered through add_request_handler, not the constructor's
        # on_set_logging_level, which warns (MCPDeprecationWarning) on every construction;
        # the handler alone is what advertises `logging: {}`.
        self.add_request_handler("logging/setLevel", mcp_types.SetLevelRequestParams,
                                 self._on_set_logging_level)
        # The SDK seeds an OpenTelemetry span middleware on every message; BrowserPlug has
        # never emitted spans. Matched by name: its class lives in a private SDK module.
        # list_changed's middleware records each handshake-era session for its pushes.
        self.middleware = [self.list_changed.middleware] + [
            mw for mw in self.middleware if type(mw).__name__ != "OpenTelemetryMiddleware"]
        self.registry = registry
        self.outcome = ToolOutcomePolicy(record=record)
        self.call_gate = call_gate or (lambda name, arguments: nullcontext())

    # ---- the accessors the unit suite reads tools through ------------------------------

    async def list_tools(self) -> List[ToolSpec]:
        """Every tool, in registration order."""
        return await self.registry.list_tools()

    async def get_tool(self, name: str) -> Optional[ToolSpec]:
        """The tool called `name`, or None."""
        return await self.registry.get_tool(name)

    # ---- initialize --------------------------------------------------------------------

    def create_initialization_options(self, notification_options=None,
                                      experimental_capabilities=None, extensions=None):
        return super().create_initialization_options(
            notification_options or HTTP_NOTIFICATIONS, experimental_capabilities, extensions)

    def get_capabilities(self, notification_options=None, experimental_capabilities=None,
                         extensions=None, *, protocol_version=None):
        # create_initialization_options always passes `experimental or {}` (and the empty
        # extensions map), which the wire would carry as "experimental": {}; clients have
        # never been sent that key.
        return super().get_capabilities(notification_options, experimental_capabilities or None,
                                        extensions or None, protocol_version=protocol_version)

    # ---- transports --------------------------------------------------------------------

    def http_app(self, *, host_origin_layer: bool = True):
        """A fresh Starlette app serving Streamable HTTP at /mcp, with a new session
        manager (a manager runs once, so every uvicorn start needs its own app).

        Stateful and SSE, as every client has always been served: the session id is the
        client's identity here. No idle expiry (the SDK default reaps a session after 30
        min idle and answers its next call 404).

        Host and Origin are checked twice before the SDK sees a request: by HostGuard in
        HttpEndpoints, then by the HostOriginLayer installed here (mcp_host_origin.py),
        the check that guarded this app before the SDK migration. `host_origin_layer=False`
        leaves the second one out, for the tests that prove HostGuard holds on its own. The SDK's own
        DNS-rebinding check stays off: it would be a third allowlist, with rules no
        client was ever served under."""
        app = self.streamable_http_app(
            streamable_http_path="/mcp", json_response=False, stateless_http=False,
            session_idle_timeout=None, max_request_body_size=MAX_REQUEST_BODY,
            transport_security=TransportSecuritySettings(
                enable_dns_rebinding_protection=False))
        # tools/list_changed runs in this app's lifespan, on uvicorn's loop. Installed
        # before HostOriginLayer so that layer stays outermost.
        list_changed.install(app, self.list_changed)
        if host_origin_layer:
            app.add_middleware(mcp_host_origin.HostOriginLayer)
        return app

    async def run_stdio(self) -> None:
        """Serve MCP over stdin/stdout (`server.py --stdio`), with tools/list_changed bound
        to this loop for as long as the pipe is open.

        The era setting (mcp.era) is applied here, through serve_duplex, and not by
        overriding Server.run: the in-process client transport calls run() and keeps the
        SDK's dual-era loop."""
        async with self.list_changed.running(), stdio_server() as (read_stream, write_stream):
            await protocol_era.serve_duplex(
                self, read_stream, write_stream,
                self.create_initialization_options(STDIO_NOTIFICATIONS))

    # ---- tools -------------------------------------------------------------------------

    async def _on_list_tools(self, ctx, params) -> mcp_types.ListToolsResult:
        # One snapshot, so a refresh on the WebSocket thread never shows half a change.
        return mcp_types.ListToolsResult(tools=self.registry.wire_models())

    async def _on_call_tool(self, ctx, params) -> mcp_types.CallToolResult:
        name = params.name
        # An absent `arguments` and `"arguments": null` are both a call with none.
        arguments = params.arguments or {}
        spec = self.registry.get(name)
        if spec is None:
            return error_result(renamed_tool_message(name) or unknown_tool_text(name))
        try:
            async with self.call_gate(name, arguments):
                return await self.outcome.apply(name, lambda: spec.call(arguments),
                                                call=self._call_identity(ctx))
        except ToolArgumentError as e:
            # pydantic's report first, unchanged; then the argument it meant, or the shape
            # to send (argument_hints.py).
            specs = self.registry.snapshot()
            return error_result(str(e) + refusal_hints(
                name, e.validation_error.errors(), tool_parameters(specs), tool_item_fields(specs)))
        except Exception as e:
            # Exception, never BaseException: a cancelled call must stay cancelled. The
            # client gets the error text; the rotating log gets the tool, the exception
            # class and its frame locations, never the message (see raise_site).
            logger.warning("tools/call %r raised %s", name, raise_site(e))
            return error_result(unexpected_error_text(name, e))

    @staticmethod
    def _call_identity(ctx):
        """(session, JSON-RPC request id) the outcome is filed under in CallTelemetry.

        Over HTTP the session is the Mcp-Session-Id header, the key the ASGI wrapper
        recorded the call under; a 2026-07-28 request has none, and the wrapper bound its
        connection key under that name (modern_identity.py). Without HTTP (stdio, the
        in-process client) it is this connection's token: it matches no HTTP record, so the
        verdict is dropped rather than written onto another client's newest call."""
        if ctx.request_id is None:
            return None
        session = request_context.header("mcp-session-id")
        if not session:
            lifespan = ctx.lifespan_context
            session = lifespan.get(_CONNECTION_KEY) if isinstance(lifespan, dict) else None
        return (session, ctx.request_id) if session else None

    # ---- prompts, resources, logging: empty, as they have always been ------------------

    async def _on_list_prompts(self, ctx, params) -> mcp_types.ListPromptsResult:
        return mcp_types.ListPromptsResult(prompts=[])

    async def _on_get_prompt(self, ctx, params):
        raise MCPError(code=mcp_types.INVALID_PARAMS, message=f"Unknown prompt: {params.name!r}")

    async def _on_list_resources(self, ctx, params) -> mcp_types.ListResourcesResult:
        return mcp_types.ListResourcesResult(resources=[])

    async def _on_list_resource_templates(self, ctx, params):
        return mcp_types.ListResourceTemplatesResult(resource_templates=[])

    async def _on_read_resource(self, ctx, params):
        uri = str(params.uri)
        raise MCPError(code=mcp_types.INVALID_PARAMS, message=f"Resource not found: {uri!r}",
                       data={"uri": uri})

    async def _on_set_logging_level(self, ctx, params) -> mcp_types.EmptyResult:
        # Nothing is ever sent as notifications/message, so there is no level to keep.
        return mcp_types.EmptyResult()
