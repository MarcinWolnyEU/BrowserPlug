"""server/proxy/front.py: what an MCP client sees on the proxy transport's stdio.

The front is the SDK's low-level Server; these drive it with the SDK client in-process
(`Client(server, mode="legacy")` frames real JSON-RPC through Server.run), in front of a
FakeBackend or of a real BrowserPlugMCPServer reached through proxy_doubles.ASGIBackend.

Ported here from the pre-SDK suite:
  * test_launcher.py TestInstalledMiddlewares: the session capture the /health watcher
    pushes tools/list_changed through, and the per-call /timing report;
  * test_tool_outcome.py's StaleToolNameMiddleware tests: a stale tool name answered at
    the proxy with the renamed-tool text, identical to what the server itself answers.
New: the initialize reply the old proxy gave (capabilities, serverInfo, instructions),
the tool-name cache and its TTL, results forwarded field for field, and how each request
reads with the backend gone. And what the old proxy did without a test: a request the
client cancels, or leaves in flight by closing stdin, is cancelled at the backend and
its session closed; a result far above httpx2's 1 MiB SSE event cap arrives whole.
"""

import asyncio
import base64
import os
import time
from contextlib import AsyncExitStack, asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import anyio
import httpx2
import mcp_types
import pytest
from mcp import Client
from mcp.server.subscriptions import SUBSCRIPTION_ID_META_KEY, ToolsListChanged
from mcp.shared.exceptions import MCPError
from mcp.shared.memory import create_client_server_memory_streams

from browserplug_shared import mcp_era
from browserplug_shared.version import __version__ as BROWSERPLUG_VERSION
from proxy_doubles import (BACKEND_INSTRUCTIONS, DEGRADED_META, ASGIBackend, DelayedAnswers,
                           FakeBackend, backend_server, backend_tools, config, live_sessions)
from server.proxy import backend as backend_mod
from server.proxy import front, policy, side_channel
from server.proxy.backend import CONNECTION_META_KEYS, BackendUnreachable
from server.proxy.front import ProxyServer
from server.tools.tool_renames import RENAMED_TOOLS, renamed_tool_message

# The initialize capabilities the old proxy advertised on stdio, as parsed JSON.
STDIO_CAPABILITIES = {"logging": {}, "prompts": {"listChanged": False},
                      "resources": {"subscribe": False, "listChanged": False},
                      "tools": {"listChanged": True}}

_TAIL = ("Your tool list is stale - re-read it (tools/list); this server "
         "exposes 9 tools (10 with 2+ browsers connected).")
PAGE_WAIT_FOR_TEXT = ("Unknown tool 'page_wait_for': it was renamed. Use 'page_act' with "
                      'a step {"action": "wait", "target": "<css>"}. ' + _TAIL)


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class _Ctx:
    def __init__(self, method="tools/list", session=None, params=None):
        self.method = method
        self.session = session
        self.params = params or {}


def _proxy(backend=None, report=None, clock=None):
    cache = policy.ToolNameCache(clock=clock) if clock else None
    return ProxyServer(config(), backend=backend or FakeBackend(),
                       report_timing=report or AsyncMock(), tools_cache=cache)


def _text(result):
    return "".join(getattr(c, "text", "") for c in result.content)


class _RealBackend:
    """A real BrowserPlugMCPServer app behind an ASGIBackend, entered in the test's task,
    serving each request in a task group of its own as a server across a socket would
    (proxy_doubles._ServedApart). `wrap(app)` puts an ASGI wrapper (DelayedAnswers)
    between the two."""

    def __init__(self, functions=None, wrap=None):
        self.server = backend_server(functions)
        self.app = self.server.http_app()
        self.wrapped = wrap(self.app) if wrap else self.app
        self.backend = None
        self._stack = None

    async def __aenter__(self):
        self._stack = AsyncExitStack()
        await self._stack.__aenter__()
        await self._stack.enter_async_context(self.app.router.lifespan_context(self.app))
        serving = await self._stack.enter_async_context(anyio.create_task_group())
        self._stack.callback(serving.cancel_scope.cancel)   # open SSE streams end here
        self.backend = ASGIBackend(self.wrapped, serve_group=serving)
        return self.backend

    async def __aexit__(self, *exc):
        return await self._stack.__aexit__(*exc)


# ---- initialize ----------------------------------------------------------------------

async def test_initialize_answers_what_the_old_proxy_answered():
    async with Client(_proxy(), mode="legacy") as client:
        caps = client.server_capabilities.model_dump(by_alias=True, mode="json",
                                                     exclude_none=True)
        info = client.server_info
        instructions = client.instructions
        version = client.protocol_version
    assert caps == STDIO_CAPABILITIES, "no `experimental`, prompts/resources listChanged false"
    assert (info.name, info.version) == ("BrowserPlug", BROWSERPLUG_VERSION)
    assert instructions == BACKEND_INSTRUCTIONS, "the proxy has none; the backend's are used"
    assert version == "2025-11-25"


async def test_the_installed_middleware_is_the_capture_and_the_gate_and_nothing_else():
    server = _proxy()
    names = [getattr(m, "__name__", type(m).__name__) for m in server.middleware]
    assert names == ["_capture_session", "_handshake_gate"], (
        "the SDK's OpenTelemetry span middleware is dropped, as the old proxy dropped it")


async def test_logging_set_level_is_answered_here():
    backend = FakeBackend()
    async with Client(_proxy(backend), mode="legacy") as client:
        # The raw request: Client.set_logging_level warns that 2026-07-28 drops logging.
        result = await client.session.send_request(
            mcp_types.SetLevelRequest(params=mcp_types.SetLevelRequestParams(level="debug")),
            mcp_types.EmptyResult)
    assert result == mcp_types.EmptyResult()
    assert [c for c in backend.calls if c[0] != "probe"] == []


# ---- the session capture (was TestInstalledMiddlewares) -------------------------------

async def test_capture_session_stores_the_live_session_for_the_watcher():
    """This is the hand-off watch_tools reads: holder["session"] must end up holding the
    session of the live stdio connection, and a later message replaces it."""
    server = _proxy()
    assert server.holder == {"session": None}
    session = object()
    call_next = AsyncMock(return_value="downstream-result")
    context = _Ctx(session=session)
    assert await server._capture_session(context, call_next) == "downstream-result"
    call_next.assert_awaited_once_with(context)
    assert server.holder["session"] is session

    newer = object()
    await server._capture_session(_Ctx(session=newer), AsyncMock(return_value=None))
    assert server.holder["session"] is newer


async def test_capture_session_survives_a_context_without_a_session():
    server = _proxy()
    assert await server._capture_session(_Ctx(session=None), AsyncMock(return_value="ok")) == "ok"
    assert server.holder["session"] is None


async def test_capture_session_swallows_a_raising_context():
    class _Raising(_Ctx):
        @property
        def session(self):
            raise RuntimeError("no context")

        @session.setter
        def session(self, value):
            pass

    server = _proxy()
    # The call must still be proxied even though the capture failed.
    assert await server._capture_session(_Raising(), AsyncMock(return_value="ok")) == "ok"


async def test_a_real_connection_fills_the_holder_with_a_session_that_can_push():
    server = _proxy()
    async with Client(server, mode="legacy"):
        session = server.holder["session"]
        assert session is not None and hasattr(session, "send_tool_list_changed")


# ---- tool-list changes, in both eras, through serve() --------------------------------

class _Health:
    """The /health that side_channel.watch_tools polls: one server_pid, and a
    tools_signature the test moves by hand."""

    def __init__(self):
        self.signature = "sig-a"
        self.polls = 0

    def httpx2(self):
        health = self

        class _Client:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def get(self, url, headers=None):
                health.polls += 1
                body = {"server_pid": 100, "tools_signature": health.signature}
                return SimpleNamespace(json=lambda: body)

        return SimpleNamespace(AsyncClient=_Client)

    async def change(self):
        """Move the surface once the watcher holds its baseline (a change before the first
        poll would become the baseline and never be announced)."""
        await _until(lambda: self.polls >= 1, "the watcher's first /health read")
        self.signature += "+"


@pytest.fixture(autouse=True)
def _default_era(monkeypatch):
    """These tests run under the default setting unless one sets its own; a shell that
    exports BROWSERPLUG_MCP_ERA must not change them."""
    monkeypatch.delenv(mcp_era.ERA_ENV, raising=False)


@asynccontextmanager
async def _served(monkeypatch, health):
    """front.serve() as the proxy process runs it (the watcher included), with a memory
    pipe for stdio and a FakeBackend. Yields the client's end as a Transport; closing it is
    stdin closing, and serve() must then return."""
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        @asynccontextmanager
        async def stdio():
            yield server_streams

        @asynccontextmanager
        async def pipe():
            try:
                yield client_streams
            finally:
                await client_streams[1].aclose()

        monkeypatch.setattr(front, "stdio_server", stdio)
        monkeypatch.setattr(front, "Backend", lambda cfg: FakeBackend())
        monkeypatch.setattr(side_channel, "TOOLS_POLL_SECONDS", 0.01)
        monkeypatch.setattr(side_channel, "httpx2", health.httpx2())
        returned = anyio.Event()

        async def serve():
            await front.serve(config())
            returned.set()

        async with anyio.create_task_group() as group:
            group.start_soon(serve)
            yield pipe()
            with anyio.fail_after(5):
                await returned.wait()


async def test_a_2026_client_hears_of_a_tool_change_on_its_listen_stream(monkeypatch):
    """At 2026-07-28 the SDK drops a bare tools/list_changed; the change reaches the client
    only on a subscriptions/listen stream, fed by the bus serve() hands the watcher."""
    health = _Health()
    bare = []

    async def on_message(message):
        if isinstance(message, mcp_types.ToolListChangedNotification):
            bare.append(message)

    async with _served(monkeypatch, health) as pipe:
        async with Client(pipe, message_handler=on_message) as client:
            assert client.protocol_version == "2026-07-28"
            assert client.server_capabilities.tools.list_changed is True
            async with client.listen(tools_list_changed=True) as subscription:
                await health.change()
                with anyio.fail_after(5):
                    assert await subscription.__anext__() == ToolsListChanged()
                polls = health.polls
                await _until(lambda: health.polls >= polls + 3, "three more steady polls")
                with anyio.move_on_after(0.05) as quiet:
                    await subscription.__anext__()
                assert quiet.cancelled_caught, "one change, one event"
                stamp = {SUBSCRIPTION_ID_META_KEY: subscription.subscription_id}
    # The client also hands listen frames to the message handler; an unstamped frame here
    # would be the bare push reaching a client that never asked for it.
    assert [m.params.meta for m in bare] == [stamp], "the stamped copy and nothing else"


async def test_a_handshake_client_hears_of_a_tool_change_once(monkeypatch):
    health = _Health()
    pushed = []

    async def on_message(message):
        if isinstance(message, mcp_types.ToolListChangedNotification):
            pushed.append(message)

    async with _served(monkeypatch, health) as pipe:
        async with Client(pipe, mode="legacy", message_handler=on_message) as client:
            assert client.server_capabilities.tools.list_changed is True
            await client.list_tools()           # a message fills the watcher's holder
            await health.change()
            await _until(lambda: pushed, "the push")
            polls = health.polls
            await _until(lambda: health.polls >= polls + 3, "three more steady polls")
    assert len(pushed) == 1


@pytest.mark.parametrize("era", ["direct", "all"])
async def test_a_setting_that_serves_2026_07_28_serves_it_with_list_changed(monkeypatch, era):
    """The valve in front of the proxy's run: `direct` and `all` leave the dual-era loop, so
    an auto client ends on 2026-07-28 and hears of a tool change on its listen stream."""
    monkeypatch.setenv(mcp_era.ERA_ENV, era)
    health = _Health()
    async with _served(monkeypatch, health) as pipe:
        async with Client(pipe) as client:
            assert client.protocol_version == "2026-07-28"
            async with client.listen(tools_list_changed=True) as subscription:
                await health.change()
                with anyio.fail_after(5):
                    assert await subscription.__anext__() == ToolsListChanged()


async def test_the_handshake_setting_ends_a_modern_client_on_2025_11_25(monkeypatch):
    """Under `handshake` the probe a 2026-07-28 client opens with is refused, so an auto
    client falls back to `initialize`, reads the same capabilities, and is pushed the
    change as a bare tools/list_changed, once."""
    monkeypatch.setenv(mcp_era.ERA_ENV, "handshake")
    health = _Health()
    pushed = []

    async def on_message(message):
        if isinstance(message, mcp_types.ToolListChangedNotification):
            pushed.append(message)

    async with _served(monkeypatch, health) as pipe:
        async with Client(pipe, message_handler=on_message) as client:
            assert client.protocol_version == "2025-11-25"
            assert client.server_capabilities.tools.list_changed is True
            await client.list_tools()           # a message fills the watcher's holder
            await health.change()
            await _until(lambda: pushed, "the push")
            polls = health.polls
            await _until(lambda: health.polls >= polls + 3, "three more steady polls")
    assert len(pushed) == 1
    assert [m.params for m in pushed if m.params and m.params.meta] == [], "never stamped"


@pytest.mark.parametrize("era, served", [("handshake", False), ("all", True)])
async def test_a_client_that_insists_on_2026_07_28_is_served_only_where_the_setting_serves_it(
        monkeypatch, era, served):
    """A client that adopts 2026-07-28 without a probe (mode="2026-07-28") gets its tools/list
    answered under `all` and refused under `handshake`: the valve decides, not the client."""
    monkeypatch.setenv(mcp_era.ERA_ENV, era)
    async with _served(monkeypatch, _Health()) as pipe:
        async with Client(pipe, mode="2026-07-28") as client:
            with anyio.fail_after(10):
                if served:
                    assert [t.name for t in (await client.list_tools()).tools] == ["echo"]
                else:
                    with pytest.raises(MCPError):
                        await client.list_tools()


async def test_a_handshake_client_still_cannot_listen():
    """Serving listen for 2026-07-28 leaves the handshake era's method table alone."""
    async with Client(_proxy(), mode="legacy") as client:
        with pytest.raises(MCPError) as refused:
            await client.session.send_request(
                mcp_types.SubscriptionsListenRequest(
                    params=mcp_types.SubscriptionsListenRequestParams(
                        notifications=mcp_types.SubscriptionFilter(tools_list_changed=True))),
                mcp_types.SubscriptionsListenResult)
    assert refused.value.code == mcp_types.METHOD_NOT_FOUND


# ---- the /timing report (was TestInstalledMiddlewares) --------------------------------

async def test_timing_reports_the_tool_name_and_the_config():
    report = AsyncMock()
    async with Client(_proxy(report=report), mode="legacy") as client:
        await client.call_tool("echo", {})
        await asyncio.sleep(0)   # let the fire-and-forget task run
    report.assert_awaited_once()
    cfg, tool, started_epoch, total_ms = report.await_args.args
    assert cfg.backend_url == config().backend_url and cfg.launcher_pid == 5151
    assert tool == "echo"
    assert total_ms >= 0 and started_epoch > 0


async def test_timing_reports_even_when_the_tool_call_fails():
    """The report lives in a finally: a failing call is exactly the one whose duration you
    want on the connection map. A failure _call_tool turns into an error result returns
    normally, so this one raises out of the handler: the missing-capability error, which
    stays a JSON-RPC error (a cancelled call is the other way out, below)."""
    report = AsyncMock()
    backend = FakeBackend(call_result=MCPError(code=mcp_types.MISSING_REQUIRED_CLIENT_CAPABILITY,
                                               message="needs sampling"))
    async with Client(_proxy(backend, report), mode="legacy") as client:
        with pytest.raises(MCPError):
            await client.call_tool("echo", {})
        await asyncio.sleep(0)
    report.assert_awaited_once()
    assert report.await_args.args[1] == "echo"


async def test_a_call_that_raises_out_of_the_handler_is_still_reported():
    report = AsyncMock()
    server = _proxy(report=report)
    server._call_tool = AsyncMock(side_effect=ValueError("not a CallToolResult"))
    with pytest.raises(ValueError):
        await server._on_call_tool(_Ctx("tools/call"),
                                   mcp_types.CallToolRequestParams(name="echo", arguments={}))
    await asyncio.sleep(0)
    report.assert_awaited_once()
    assert report.await_args.args[1] == "echo"


async def test_timing_reports_a_call_to_an_unknown_name_too():
    report = AsyncMock()
    async with Client(_proxy(report=report), mode="legacy") as client:
        await client.call_tool("page_wait_for", {})
        await asyncio.sleep(0)
    assert report.await_args.args[1] == "page_wait_for"


async def test_timing_does_not_await_the_report():
    """It must not add its own latency to the call: the report is scheduled as a task, so
    the tool result returns before the POST completes."""
    gate = asyncio.Event()
    finished = []

    async def slow_report(*args):
        await gate.wait()
        finished.append(args)

    async with Client(_proxy(report=slow_report), mode="legacy") as client:
        result = await client.call_tool("echo", {})
        assert _text(result) == "hi"
        assert finished == []       # returned without waiting on the report
        gate.set()
        for _ in range(5):
            await asyncio.sleep(0)
    assert len(finished) == 1


# ---- stale and unknown tool names (was test_tool_outcome's StaleToolNameMiddleware) ---

async def test_the_proxy_rewrites_only_names_it_knows():
    backend = FakeBackend()
    async with Client(_proxy(backend), mode="legacy") as client:
        renamed = await client.call_tool("page_wait_for", {})
        unknown = await client.call_tool("nope", {})
    assert renamed.is_error and _text(renamed) == PAGE_WAIT_FOR_TEXT
    assert unknown.is_error and _text(unknown) == "Unknown tool: 'nope'"
    assert not [c for c in backend.calls if c[0] == "call"], (
        "a name outside the tool list is answered here and never sent")


async def test_a_known_name_passes_through_untouched():
    """On the happy path the proxy adds no wrapping and no meta."""
    backend = FakeBackend(call_result={"content": [{"type": "text", "text": "ok"}],
                                       "isError": False})
    async with Client(_proxy(backend), mode="legacy") as client:
        result = await client.call_tool("echo", {"x": 1})
    assert result.model_dump(by_alias=True, mode="json", exclude_none=True) == {
        "content": [{"type": "text", "text": "ok"}], "isError": False,
        "resultType": "complete"}
    assert ("call", "echo", {"x": 1}, None) in backend.calls


@pytest.mark.parametrize("name", [*sorted(RENAMED_TOOLS), "nope", "", "totally_made_up",
                                  "page_wait", "PAGE_WAIT_FOR"])
async def test_the_proxy_answers_a_stale_name_exactly_as_the_server_does(name):
    """The proxy answers these itself (they are not in the tool list), and the relay path
    forwards them to the server; the client must read the same text either way."""
    async with _RealBackend() as backend:
        async with Client(_proxy(backend), mode="legacy") as client:
            via_proxy = await client.call_tool(name, {})
        async with Client(backend_server(), mode="legacy") as direct:
            via_server = await direct.call_tool(name, {})
    assert via_proxy.is_error is True and via_server.is_error is True
    assert _text(via_proxy) == _text(via_server)
    expected = renamed_tool_message(name) or f"Unknown tool: {name!r}"
    assert _text(via_proxy) == expected


# ---- the tool-name cache ---------------------------------------------------------------

def test_the_production_cache_uses_the_policy_ttl():
    """The cache a ProxyServer builds for itself, not one a test injects."""
    server = ProxyServer(config(), backend=FakeBackend(), report_timing=AsyncMock())
    assert server.tools.ttl == policy.TOOLS_CACHE_TTL_SECONDS


async def test_a_call_inside_the_ttl_is_resolved_without_listing_again():
    clock = _Clock()
    backend = FakeBackend()
    async with Client(_proxy(backend, clock=clock), mode="legacy") as client:
        await client.list_tools()
        clock.now += policy.TOOLS_CACHE_TTL_SECONDS - 1
        await client.call_tool("echo", {})
    assert [c for c in backend.calls if c[0] == "list"] == [("list", "tools/list")]


async def test_a_stale_cache_is_listed_again_before_the_call():
    clock = _Clock()
    backend = FakeBackend()
    async with Client(_proxy(backend, clock=clock), mode="legacy") as client:
        await client.list_tools()
        clock.now += policy.TOOLS_CACHE_TTL_SECONDS
        await client.call_tool("echo", {})
    assert [c for c in backend.calls if c[0] == "list"] == [("list", "tools/list")] * 2


async def test_a_first_call_with_no_list_yet_lists_first():
    backend = FakeBackend()
    async with Client(_proxy(backend), mode="legacy") as client:
        await client.call_tool("echo", {})
    kinds = [c[0] for c in backend.calls if c[0] != "probe"]
    # The SDK client then lists on its own, to check the result against an output schema
    # it has not seen; that third entry is the client's, not the proxy's.
    assert kinds[:2] == ["list", "call"]


async def test_a_tool_added_after_the_list_is_unknown_until_the_cache_expires():
    """The old proxy behaved this way: a client that skips tools/list after
    tools/list_changed (the `browsers` tool appearing) meets "Unknown tool" for at most
    the TTL."""
    clock = _Clock()
    backend = FakeBackend()
    async with Client(_proxy(backend, clock=clock), mode="legacy") as client:
        await client.list_tools()
        backend.tool_list.append({"name": "browsers",
                                  "inputSchema": {"type": "object", "properties": {}}})
        early = await client.call_tool("browsers", {})
        clock.now += policy.TOOLS_CACHE_TTL_SECONDS
        late = await client.call_tool("browsers", {})
    assert _text(early) == "Unknown tool: 'browsers'"
    assert not late.is_error and _text(late) == "hi"


async def test_a_list_that_fails_leaves_the_call_to_the_backend():
    """When the list cannot be had the proxy cannot tell known from unknown, so it
    forwards: the backend answers with its own (identical) texts, or the call reports the
    connection failure."""
    backend = FakeBackend(call_result=BackendUnreachable(httpx2.ConnectError(
        "All connection attempts failed")))
    backend.tool_list = BackendUnreachable(httpx2.ConnectError("All connection attempts failed"))
    async with Client(_proxy(backend), mode="legacy") as client:
        listed = await client.list_tools()
        result = await client.call_tool("server_info", {})
    assert listed.tools == [], "a backend that cannot be listed lists nothing, as before"
    assert result.is_error
    assert _text(result) == ("Error calling tool 'server_info': Client failed to connect: "
                             "All connection attempts failed")


# ---- forwarding ------------------------------------------------------------------------

async def test_results_are_forwarded_field_for_field_from_a_real_backend():
    async with _RealBackend() as backend:
        async with Client(_proxy(backend), mode="legacy") as client:
            listed = [t.name for t in (await client.list_tools()).tools]
            ok = await client.call_tool("echo", {"text": "hello"})
            degraded = await client.call_tool("degraded", {})
            failed = await client.call_tool("boom", {})
            picture = await client.call_tool("picture", {})
            bad_args = await client.call_tool("echo", {"bogus": 1})
    assert listed == ["echo", "degraded", "boom", "picture"]
    assert _text(ok) == "hello" and ok.is_error is False and ok.meta is None
    assert degraded.meta == DEGRADED_META and degraded.is_error is False
    assert failed.is_error and _text(failed) == "Error calling tool 'boom': kaboom detail"
    assert [c.type for c in picture.content] == ["text", "image"]
    assert picture.content[1].data == "aGk=" and picture.content[1].mime_type == "image/png"
    assert bad_args.is_error and "1 validation error for call[echo]" in _text(bad_args)


async def test_resources_and_prompts_are_forwarded_with_the_backends_errors():
    async with _RealBackend() as backend:
        async with Client(_proxy(backend), mode="legacy") as client:
            assert (await client.list_resources()).resources == []
            assert (await client.list_resource_templates()).resource_templates == []
            assert (await client.list_prompts()).prompts == []
            with pytest.raises(MCPError) as missing_resource:
                await client.read_resource("browserplug://nothing")
            with pytest.raises(MCPError) as missing_prompt:
                await client.get_prompt("nothing")
    assert missing_resource.value.code == -32602
    assert missing_resource.value.message == "Resource not found: 'browserplug://nothing'"
    assert missing_resource.value.data == {"uri": "browserplug://nothing"}
    assert (missing_prompt.value.code, missing_prompt.value.message) == (
        -32602, "Unknown prompt: 'nothing'")


async def test_ping_is_forwarded_and_a_dead_backend_is_a_code_0_error():
    backend = FakeBackend()

    async def ping(client):
        # The raw request: Client.send_ping warns that 2026-07-28 drops ping.
        return await client.session.send_request(mcp_types.PingRequest(),
                                                 mcp_types.EmptyResult)

    async with Client(_proxy(backend), mode="legacy") as client:
        await ping(client)
        assert ("request", "ping") in backend.calls

        async def unreachable(request):
            raise BackendUnreachable(httpx2.ConnectError("All connection attempts failed"))

        backend.send = unreachable
        with pytest.raises(MCPError) as failure:
            await ping(client)
    assert failure.value.code == 0
    assert failure.value.message == "Client failed to connect: All connection attempts failed"


async def test_a_missing_client_capability_stays_a_json_rpc_error():
    backend = FakeBackend(call_result=MCPError(code=mcp_types.MISSING_REQUIRED_CLIENT_CAPABILITY,
                                               message="needs sampling"))
    async with Client(_proxy(backend), mode="legacy") as client:
        with pytest.raises(MCPError) as failure:
            await client.call_tool("echo", {})
    assert failure.value.code == mcp_types.MISSING_REQUIRED_CLIENT_CAPABILITY


async def test_any_other_backend_error_becomes_the_calls_error_result():
    backend = FakeBackend(call_result=MCPError(code=-32603, message="backend broke"))
    async with Client(_proxy(backend), mode="legacy") as client:
        result = await client.call_tool("echo", {})
    assert result.is_error and _text(result) == "Error calling tool 'echo': backend broke"


async def test_absent_and_null_arguments_are_sent_as_an_empty_object():
    backend = FakeBackend()
    async with Client(_proxy(backend), mode="legacy") as client:
        await client.call_tool("echo", None)
    assert ("call", "echo", {}, None) in backend.calls


async def test_connection_meta_never_reaches_the_backend():
    """A client at the 2026-07-28 era stamps its protocol version, clientInfo and
    capabilities into every request's _meta. They describe its connection to the proxy;
    the backend session negotiated its own and is handshake-era. The rest of _meta (the
    progress token) is forwarded, on tools/call and on the requests forwarded whole."""
    backend = FakeBackend()

    async def send(request):
        backend.sent.append(request)
        return ({"contents": []} if request.method == "resources/read"
                else {"messages": []})

    backend.send = send
    async with Client(_proxy(backend), mode="auto") as client:
        assert client.protocol_version == "2026-07-28", "the era that stamps the keys"
        await client.call_tool("echo", {}, meta={"progressToken": 7})
        await client.read_resource("browserplug://x", meta={"progressToken": 8})
        await client.get_prompt("p", meta={"progressToken": 9})
    [call] = [c for c in backend.calls if c[0] == "call"]
    assert call[3] == {"progressToken": 7}
    forwarded = [r.model_dump(by_alias=True, mode="json", exclude_none=True)["params"]["_meta"]
                 for r in backend.sent]
    assert forwarded == [{"progressToken": 8}, {"progressToken": 9}]
    for meta in [call[3], *forwarded]:
        assert not set(meta) & CONNECTION_META_KEYS


# ---- cancellation: the backend hears of it and its session is closed -----------------

def _slow_tool(events):
    """A backend tool that runs until cancelled, noting its start and its cancellation."""
    async def slow(seconds: float = 30.0) -> str:
        """Sleeps."""
        events["started"].set()
        try:
            await anyio.sleep(seconds)
        except anyio.get_cancelled_exc_class():
            events["cancelled"].set()
            raise
        return "slept"
    return slow


def _events():
    return {"started": anyio.Event(), "cancelled": anyio.Event()}


async def _until(predicate, what, seconds=5.0):
    deadline = time.monotonic() + seconds
    while not predicate():
        assert time.monotonic() < deadline, f"not within {seconds} s: {what}"
        await anyio.sleep(0.01)


async def test_a_cancelled_call_is_cancelled_at_the_backend_and_its_session_closed():
    """The client gives up on a call (Esc, its own timeout): it sends
    notifications/cancelled and the SDK cancels the proxy's handler. The backend must hear
    it too, or the tool keeps driving the browser, and the session must be DELETEd, or the
    shared server, which reaps no idle session, holds it for good."""
    events = _events()
    report = AsyncMock()
    real = _RealBackend([*backend_tools(), _slow_tool(events)])
    async with real as backend:
        async with Client(_proxy(backend, report), mode="legacy") as client:
            async with anyio.create_task_group() as group:
                group.start_soon(client.call_tool, "slow", {})
                with anyio.fail_after(10):
                    await events["started"].wait()
                group.cancel_scope.cancel()
            with anyio.fail_after(5):
                await events["cancelled"].wait()
            await _until(lambda: live_sessions(real.server) == 0, "every session closed")
            assert (await client.call_tool("echo", {"text": "after"})).is_error is False
        await asyncio.sleep(0)
    assert backend.sessions(), "the sessions were DELETEd"
    assert [c.args[1] for c in report.await_args_list][:1] == ["slow"], (
        "a cancelled call is timed too")


async def test_a_list_refresh_the_client_gives_up_on_closes_its_session():
    """A tools/call that lists the backend first (an empty or stale cache) and is
    cancelled while the list is out."""
    real = _RealBackend(wrap=lambda app: DelayedAnswers(app, "tools/list", 30))
    async with real as backend:
        async with Client(_proxy(backend), mode="legacy") as client:
            async with anyio.create_task_group() as group:
                group.start_soon(client.call_tool, "echo", {})
                with anyio.fail_after(10):
                    await real.wrapped.reached.wait()
                group.cancel_scope.cancel()
            await _until(lambda: live_sessions(real.server) == 0, "the list's session closed")


async def test_a_gate_probe_that_runs_out_of_time_closes_its_session(monkeypatch):
    """The handshake gate gives the probe PROBE_TIMEOUT_SECONDS. The server may have made
    the session by then; the probe's task waits for the answer and DELETEs it."""
    monkeypatch.setattr(policy, "PROBE_TIMEOUT_SECONDS", 0.2)
    real = _RealBackend(wrap=lambda app: DelayedAnswers(app, "initialize", 1.0))
    async with real as backend:
        server = _proxy(backend)
        async with backend.running():
            with pytest.raises(MCPError) as refused:
                await server._handshake_gate(_Ctx("initialize"), AsyncMock())
            assert "TimeoutError" in refused.value.message
            assert live_sessions(real.server) == 1, "the server made the session meanwhile"
            await _until(lambda: live_sessions(real.server) == 0, "the probe's session closed")
        assert len(backend.sessions()) == 1


async def test_calls_in_flight_when_stdin_closes_are_cancelled_and_closed():
    """The client closes stdin (it quit) with a call still running: the proxy cancels it
    at the backend and closes its session before it exits, within BACKEND_DRAIN_SECONDS."""
    events = _events()
    real = _RealBackend([*backend_tools(), _slow_tool(events)])
    async with real as backend:

        async def call_and_lose_the_answer(client):
            try:
                await client.call_tool("slow", {})
            except Exception:
                pass                    # the connection closed under it, as it should

        async with anyio.create_task_group() as group:
            async with Client(_proxy(backend), mode="legacy") as client:
                group.start_soon(call_and_lose_the_answer, client)
                with anyio.fail_after(10):
                    await events["started"].wait()
            closed = time.monotonic()   # stdin is closed: the proxy has returned
        with anyio.fail_after(5):
            await events["cancelled"].wait()
        assert live_sessions(real.server) == 0
        assert time.monotonic() - closed < policy.BACKEND_DRAIN_SECONDS + 1


# ---- a result above httpx2's SSE event cap ------------------------------------------------

async def test_a_two_megabyte_image_arrives_whole():
    """The backend answers in SSE, and httpx2's EventSource caps one event at 1 MiB by
    default: a bigger result (any full-page screenshot) became "SSE stream ended without a
    response". backend.lift_sse_event_cap removes the cap."""
    data = base64.b64encode(os.urandom(1_600_000)).decode()

    async def screenshot() -> list:
        """A big image."""
        return [mcp_types.ImageContent(type="image", data=data, mimeType="image/png")]

    async with _RealBackend([screenshot]) as backend:
        async with Client(_proxy(backend), mode="legacy") as client:
            result = await client.call_tool("screenshot", {})
    assert result.is_error is False, _text(result)
    assert len(data) > 2_000_000 and result.content[0].data == data


def test_lifting_the_cap_refuses_an_sdk_that_reads_sse_another_way(monkeypatch):
    """The fix is a rebound module global, which does nothing once the SDK stops reading
    SSE through it. That must stop the proxy, not quietly fail every large result."""
    import mcp.client.streamable_http as sdk_http

    async def reads_sse_some_other_way(self, response, ctx):
        return None

    monkeypatch.setattr(sdk_http.StreamableHTTPTransport, "_handle_sse_response",
                        reads_sse_some_other_way)
    with pytest.raises(RuntimeError, match="EventSource"):
        backend_mod.lift_sse_event_cap()
    monkeypatch.undo()
    backend_mod.lift_sse_event_cap()
    assert sdk_http.EventSource is backend_mod._UncappedEventSource
