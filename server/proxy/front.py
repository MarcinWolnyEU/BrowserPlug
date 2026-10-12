"""The proxy's front: a low-level SDK Server on stdio that forwards to the shared server.

What a client sees here, method by method (each matches the pre-SDK proxy this replaces;
tests/unit/test_proxy_front.py and test_proxy_e2e.py pin them):

  initialize / server/discover
      Gated: the proxy opens one backend session first (backend.Backend.probe, bounded by
      PROBE_TIMEOUT_SECONDS). No answer fails the handshake with policy.gate_refusal_text,
      because a session that opens and lists nothing looks connected to a client and stays
      empty (the watcher takes its baseline from the first /health it reads, so a server
      that appears later is never announced). The probe's InitializeResult supplies the
      instructions, which the proxy has none of its own.
      serverInfo {"name": "BrowserPlug", "version": <BrowserPlug's version>}; capabilities
      logging {}, prompts and resources listChanged false, tools listChanged true. On a
      2026-07-28 connection the SDK derives every listChanged flag and resources.subscribe
      from subscriptions/listen being served, so all of them read true there.
  subscriptions/listen (2026-07-28 only)
      Answered here from the proxy's own bus (ListenHandler). Only ToolsListChanged is ever
      published on it; a handshake-era client gets -32601, as before. The era setting
      (mcp.era) decides whether 2026-07-28 is served at all: run() goes through
      protocol_era.serve_duplex, so under `handshake` the connection is handshake-era only
      (a modern client ends on 2025-11-25, listen unreachable), and under `direct` and
      `all` the SDK's dual-era loop serves whichever era the client's first request names.
  tools/list
      Forwarded, all pages, and the names are cached (policy.ToolNameCache). A backend
      that cannot be listed answers an empty list, as the old proxy did.
  tools/call
      Resolved against the cache (listed again once it is older than the TTL). A name not
      in the list is answered here: the renamed-tool hint for a name in
      server/tools/tool_renames.py, else "Unknown tool: 'x'". A known name is forwarded
      and the backend's result returned field for field. A backend that fails mid-call
      gives the isError text "Error calling tool 'x': <cause>".
  ping, resources/*, prompts/*
      Forwarded; an error the backend answered passes through as it is, an unreachable
      backend is a JSON-RPC error "Client failed to connect: <cause>".
  logging/setLevel
      Answered {} here.
  notifications/cancelled
      The SDK cancels the handler; the backend request it was waiting on is cancelled in
      turn, and its session closed (backend.Backend._exchange), as the old proxy did.

Every tools/call's stdio-side duration goes to /timing (side_channel.report_timing). When
the tool surface moves, side_channel.watch_tools pushes tools/list_changed on the captured
session (handshake era) and publishes ToolsListChanged on the bus (2026-07-28 era, where the
SDK drops the bare push). Each era receives only its own copy.
"""

import asyncio
import logging
import sys
import time
from collections.abc import Mapping
from typing import Any, Dict, Optional

import anyio
import mcp_types
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.stdio import stdio_server
from mcp.server.subscriptions import InMemorySubscriptionBus, ListenHandler
from mcp.shared.exceptions import MCPError

from browserplug_shared.version import __version__ as BROWSERPLUG_VERSION
from server.proxy import policy, side_channel
from server.proxy.backend import (Backend, BackendUnreachable, forwardable_request_meta,
                                  forwardable_result_meta)
from server.rpc import protocol_era
from server.tools.tool_registry import error_result, unexpected_error_text, unknown_tool_text
from server.tools.tool_renames import renamed_tool_message

logger = logging.getLogger(__name__)

SERVER_NAME = "BrowserPlug"

# The handshake-era advertisement: tools listChanged true because watch_tools pushes it;
# prompts and resources false, as the old proxy advertised on stdio. The 2026-07-28 era
# ignores these options (see get_capabilities in the SDK's lowlevel Server).
NOTIFICATIONS = NotificationOptions(tools_changed=True)

# The requests the handshake gate holds until the backend has answered.
HANDSHAKE_METHODS = frozenset({"initialize", "server/discover"})


def _wire_meta(ctx) -> Optional[dict]:
    params = ctx.params
    meta = params.get("_meta") if isinstance(params, Mapping) else None
    return dict(meta) if isinstance(meta, Mapping) else None


def _wire_params(ctx) -> Dict[str, Any]:
    """The request's params as sent, minus the connection-owned `_meta` keys."""
    params = dict(ctx.params) if isinstance(ctx.params, Mapping) else {}
    meta = forwardable_request_meta(_wire_meta(ctx))
    params.pop("_meta", None)
    if meta:
        params["_meta"] = meta
    return params


def _with_upstream(result: Any, upstream: mcp_types.InitializeResult) -> Any:
    """The front handshake result with the backend's instructions (and its `_meta`, minus
    the connection-owned keys) where the proxy has none of its own."""
    if not isinstance(result, dict):
        return result
    out = dict(result)
    if out.get("instructions") is None and upstream.instructions is not None:
        out["instructions"] = upstream.instructions
    upstream_meta = forwardable_result_meta({"_meta": dict(upstream.meta or {})}).get("_meta")
    if upstream_meta:
        out["_meta"] = {**upstream_meta, **(out.get("_meta") or {})}
    return out


class ProxyServer(Server):
    """The stdio server one launcher's client talks to. One per proxy process."""

    def __init__(self, config: policy.ProxyConfig, backend: Optional[Backend] = None,
                 report_timing=None, tools_cache: Optional[policy.ToolNameCache] = None):
        # The 2026-07-28 era's only list_changed channel: watch_tools publishes on the bus,
        # and every open subscriptions/listen stream on this connection receives it.
        bus = InMemorySubscriptionBus()
        listen = ListenHandler(bus)
        super().__init__(
            SERVER_NAME, version=BROWSERPLUG_VERSION,
            on_list_tools=self._on_list_tools,
            on_call_tool=self._on_call_tool,
            on_list_resources=self._on_list_resources,
            on_list_resource_templates=self._on_list_resource_templates,
            on_read_resource=self._on_read_resource,
            on_list_prompts=self._on_list_prompts,
            on_get_prompt=self._on_get_prompt,
            on_subscriptions_listen=listen,
            on_ping=self._on_ping)
        self.bus = bus
        self.listen = listen
        # Through add_request_handler: the constructor's on_set_logging_level warns
        # (MCPDeprecationWarning) on every construction.
        self.add_request_handler("logging/setLevel", mcp_types.SetLevelRequestParams,
                                 self._on_set_logging_level)
        # The SDK seeds an OpenTelemetry span middleware; the proxy has never emitted spans.
        # Matched by name: its class lives in a private SDK module.
        self.middleware = [self._capture_session, self._handshake_gate] + [
            mw for mw in self.middleware if type(mw).__name__ != "OpenTelemetryMiddleware"]
        self.config = config
        self.backend = backend or Backend(config)
        self.tools = tools_cache or policy.ToolNameCache()
        # The live stdio session, for watch_tools. Replaced on every message.
        self.holder: Dict[str, Any] = {"session": None}
        self._report_timing = report_timing or side_channel.report_timing
        self._timing_tasks: set = set()

    # ---- initialize --------------------------------------------------------------------

    def create_initialization_options(self, notification_options=None,
                                      experimental_capabilities=None, extensions=None):
        return super().create_initialization_options(
            notification_options or NOTIFICATIONS, experimental_capabilities, extensions)

    def get_capabilities(self, notification_options=None, experimental_capabilities=None,
                         extensions=None, *, protocol_version=None):
        # create_initialization_options passes `experimental or {}`, which the wire would
        # carry as "experimental": {}; the proxy has never sent that key.
        return super().get_capabilities(notification_options, experimental_capabilities or None,
                                        extensions or None, protocol_version=protocol_version)

    # ---- middleware --------------------------------------------------------------------

    async def _capture_session(self, ctx, call_next):
        try:
            if ctx.session is not None:
                self.holder["session"] = ctx.session
        except Exception:
            pass
        return await call_next(ctx)

    async def _handshake_gate(self, ctx, call_next):
        if ctx.method not in HANDSHAKE_METHODS:
            return await call_next(ctx)
        upstream = await self.require_backend()
        return _with_upstream(await call_next(ctx), upstream)

    async def require_backend(self) -> mcp_types.InitializeResult:
        """The backend's InitializeResult, or the MCPError that fails the handshake.

        An error the backend answered passes through; cancellation stays cancellation."""
        try:
            with anyio.fail_after(policy.PROBE_TIMEOUT_SECONDS):
                return await self.backend.probe()
        except MCPError:
            raise
        except Exception as error:
            detail = str(error) or type(error).__name__
            raise MCPError(code=policy.INTERNAL_ERROR,
                           message=policy.gate_refusal_text(self.backend.url, detail)) from error

    # ---- tools -------------------------------------------------------------------------

    async def refresh_tools(self):
        tools = await self.backend.list_all("tools/list", "tools")
        self.tools.store(t.get("name") for t in tools if isinstance(t, dict))
        return tools

    async def _on_list_tools(self, ctx, params):
        try:
            tools = await self.refresh_tools()
        except Exception as e:
            logger.warning("tools/list from the shared server failed: %s", e)
            tools = []
        return mcp_types.ListToolsResult(tools=[mcp_types.Tool.model_validate(t) for t in tools])

    async def _name_is_unknown(self, name: str) -> bool:
        """True only when a current tools/list lacks `name`. When the list cannot be had,
        the call is forwarded and the backend answers it (with the same texts)."""
        if not self.tools.is_fresh():
            try:
                await self.refresh_tools()
            except Exception as e:
                logger.warning("tools/list from the shared server failed: %s", e)
                return False
        return self.tools.knows(name) is False

    async def _on_call_tool(self, ctx, params):
        started = time.time()
        try:
            return await self._call_tool(ctx, params)
        finally:
            self._schedule_timing(params.name, started, (time.time() - started) * 1000.0)

    async def _call_tool(self, ctx, params):
        name = params.name
        if await self._name_is_unknown(name):
            return error_result(renamed_tool_message(name) or unknown_tool_text(name))
        try:
            result = await self.backend.call_tool(name, params.arguments or {},
                                                  forwardable_request_meta(_wire_meta(ctx)))
        except MCPError as e:
            # A missing client capability says the request cannot be served at all, so it
            # stays a JSON-RPC error; any other answer becomes the call's error result.
            if e.code == mcp_types.MISSING_REQUIRED_CLIENT_CAPABILITY:
                raise
            return self._call_failed(name, e)
        except Exception as e:
            return self._call_failed(name, e)
        return mcp_types.CallToolResult.model_validate(forwardable_result_meta(result))

    @staticmethod
    def _call_failed(name, error):
        logger.warning("tools/call %r failed: %s", name, error)
        return error_result(unexpected_error_text(name, error))

    def _schedule_timing(self, tool, started_epoch, total_ms):
        """Fire and forget: the result goes back to the client without waiting on it."""
        try:
            task = asyncio.get_running_loop().create_task(
                self._report_timing(self.config, tool, started_epoch, total_ms))
            self._timing_tasks.add(task)
            task.add_done_callback(self._timing_tasks.discard)
        except Exception:
            pass

    # ---- everything else is forwarded --------------------------------------------------

    async def _forward(self, request, result_type):
        """`request` on a backend session of its own, its result as `result_type` (the SDK
        then shapes it for whichever era the client speaks)."""
        try:
            raw = await self.backend.send(request)
        except BackendUnreachable as e:
            raise MCPError(code=0, message=str(e)) from e
        return result_type.model_validate(forwardable_result_meta(raw))

    async def _list_or_empty(self, method, key, result_type, item_type):
        try:
            items = await self.backend.list_all(method, key)
        except Exception as e:
            logger.warning("%s from the shared server failed: %s", method, e)
            items = []
        return result_type(**{key: [item_type.model_validate(i) for i in items]})

    async def _on_ping(self, ctx, params):
        return await self._forward(mcp_types.PingRequest(), mcp_types.EmptyResult)

    async def _on_list_resources(self, ctx, params):
        return await self._list_or_empty("resources/list", "resources",
                                         mcp_types.ListResourcesResult, mcp_types.Resource)

    async def _on_list_resource_templates(self, ctx, params):
        return await self._list_or_empty("resources/templates/list", "resourceTemplates",
                                         mcp_types.ListResourceTemplatesResult,
                                         mcp_types.ResourceTemplate)

    async def _on_list_prompts(self, ctx, params):
        return await self._list_or_empty("prompts/list", "prompts",
                                         mcp_types.ListPromptsResult, mcp_types.Prompt)

    async def _on_read_resource(self, ctx, params):
        return await self._forward(mcp_types.ReadResourceRequest(
            params=mcp_types.ReadResourceRequestParams.model_validate(_wire_params(ctx))),
            mcp_types.ReadResourceResult)

    async def _on_get_prompt(self, ctx, params):
        return await self._forward(mcp_types.GetPromptRequest(
            params=mcp_types.GetPromptRequestParams.model_validate(_wire_params(ctx))),
            mcp_types.GetPromptResult)

    async def _on_set_logging_level(self, ctx, params):
        return mcp_types.EmptyResult()

    # ---- serving -----------------------------------------------------------------------

    async def run(self, read_stream, write_stream, initialization_options,
                  raise_exceptions: bool = False) -> None:
        """Server.run inside the backend's task group (backend.Backend.running): every
        backend exchange runs there, so a request the client cancels still sends its cancel
        and its DELETE to the shared server.

        The era setting (mcp.era) is applied here, through serve_duplex: under `handshake`
        a 2026-07-28 client's probe is refused and it ends on the handshake era; under
        `direct` and `all` it is served 2026-07-28, listen stream included."""
        async with self.backend.running():
            await protocol_era.serve_duplex(self, read_stream, write_stream,
                                            initialization_options, raise_exceptions)

    async def run_stdio(self) -> None:
        async with stdio_server() as (read_stream, write_stream):
            await self.run(read_stream, write_stream, self.create_initialization_options())


async def serve(config: policy.ProxyConfig) -> None:
    """Serve one client on this process's stdio until it closes stdin."""
    server = ProxyServer(config)
    async with anyio.create_task_group() as group:
        group.start_soon(side_channel.watch_tools, server.holder, config, server.bus)
        await server.run_stdio()
        group.cancel_scope.cancel()


def configure_logging() -> None:
    """Warnings and up to stderr, which is the launcher's log; stdout is the MCP channel."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("[browserplug-launcher] proxy %(levelname)s "
                                           "%(name)s: %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.WARNING)


def main(argv=None) -> int:
    """The proxy process: `server/proxy/__main__.py <launcher's arguments>`."""
    config = policy.parse_args(sys.argv[1:] if argv is None else argv)
    configure_logging()
    try:
        anyio.run(serve, config)
    except KeyboardInterrupt:
        return 130
    return 0
