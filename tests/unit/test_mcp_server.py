"""server/tools/mcp_server.py: the SDK's low-level Server answering from BrowserPlug's tools.

What these pin is the part of the wire the SDK would otherwise decide: the initialize
reply every client has been sent (capabilities, serverInfo, instructions), the handlers
beside tools/list and tools/call, what reaches the rotating log when a tool raises, and
the production HTTP app (http_app behind the real ASGI wrapper): the request headers reach
the tool layer, and a lost session answers exactly what the stdio relay's recovery reads.
The tools/call outcome paths are in test_tool_outcome.py.
"""

import asyncio
import json
import logging
import queue
import re
import socket
import subprocess
import sys
import threading
import warnings
from pathlib import Path

import httpx2
import mcp_types
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError

import test_imports  # noqa: F401  (path setup)
from browserplug_shared.version import __version__
from facade_double import FacadeDouble
from server.server import BrowserPlugServer
from server.tools.instructions import instructions as server_instructions
from server.tools.mcp_server import MAX_REQUEST_BODY, STDIO_NOTIFICATIONS, BrowserPlugMCPServer
from server.tools.mcp_tools import BrowserPlugTools
from server.tools.tool_outcome import EXTENSION_DISCONNECTED
from server.tools.tool_registry import ToolRegistry, ToolSpec
from server.web import request_context

ROOT = Path(__file__).resolve().parents[2]
TOKEN = "a" * 43
MCP_HEADERS = {"content-type": "application/json",
               "accept": "application/json, text/event-stream"}

# What every client has been sent in the initialize reply, compared as parsed JSON.
CAPABILITIES = {"logging": {}, "prompts": {"listChanged": True},
                "resources": {"subscribe": False, "listChanged": True},
                "tools": {"listChanged": True}}


class _Facade(FacadeDouble):
    async def send_request_and_wait(self, request, timeout=30.0, browser=None):
        return {"type": "response", "data": {"tabs": [], "windows": []}}


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _server():
    return BrowserPlugServer(host="localhost", port=_free_port(), start_mcp=False,
                             ws_token=TOKEN)


def _frames(response):
    """The JSON-RPC messages in an SSE or JSON reply, parsed the way the stdio relay does."""
    from launcher.stdio_relay import messages_from
    return list(messages_from({"Content-Type": response.headers.get("content-type", "")},
                              response.text))


def _http(app):
    return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),
                              base_url="http://127.0.0.1")


def _init(msg_id=1, version="2025-11-25"):
    return json.dumps({"jsonrpc": "2.0", "id": msg_id, "method": "initialize",
                       "params": {"protocolVersion": version, "capabilities": {},
                                  "clientInfo": {"name": "t", "version": "1"}}})


# =============================================================================
# initialize
# =============================================================================

async def test_initialize_over_the_production_http_app_matches_what_clients_were_sent():
    server = _server()
    app = server.mcp_app.http_app()
    async with app.router.lifespan_context(app):
        async with _http(server._wrap_client_tracking(app)) as http:
            reply = await http.post("/mcp", headers=MCP_HEADERS, content=_init())
    assert reply.status_code == 200 and reply.headers["mcp-session-id"]
    (frame,) = _frames(reply)
    result = json.loads(frame)["result"]
    assert result["protocolVersion"] == "2025-11-25"
    assert result["capabilities"] == CAPABILITIES, "no experimental {}, every listChanged true"
    assert result["serverInfo"] == {"name": "BrowserPlug", "version": __version__}
    assert result["instructions"] == server_instructions()


def test_the_instructions_fit_what_claude_code_shows(monkeypatch):
    # Claude Code cuts a server's instructions at 2,048 characters; past that the agent
    # reads a sentence that stops halfway. Measured with a long pinned browser name.
    from server.tools import instructions as instructions_module
    monkeypatch.setattr(instructions_module, "default_browser_name", lambda: "x" * 60)
    text = instructions_module.instructions()
    assert "x" * 60 in text
    assert len(text) <= instructions_module.CLIENT_SHOWN_CHARS, len(text)


def test_stdio_advertises_tools_list_changed_only():
    caps = BrowserPlugMCPServer(ToolRegistry()).create_initialization_options(
        STDIO_NOTIFICATIONS).capabilities.model_dump(by_alias=True, exclude_none=True)
    assert caps == {"logging": {}, "prompts": {"listChanged": False},
                    "resources": {"subscribe": False, "listChanged": False},
                    "tools": {"listChanged": True}}


# What `server.py --stdio` runs, minus the WebSocket side: run_stdio() over real pipes.
_STDIO_CHILD = """\
import asyncio, sys
sys.path[:0] = sys.argv[1:]
from server.tools.mcp_server import BrowserPlugMCPServer
from server.tools.tool_registry import ToolRegistry, ToolSpec

async def ping() -> str:
    return "pong"

asyncio.run(BrowserPlugMCPServer(ToolRegistry([ToolSpec.from_function(ping)])).run_stdio())
"""


def test_run_stdio_answers_initialize_and_calls_with_no_or_null_arguments(tmp_path):
    """run_stdio() itself, in a process of its own: the stdio capabilities reach the wire,
    and the SDK parses a tools/call with no `arguments` key and one with
    `"arguments": null` into a call with none. No browser, no ports."""
    script = tmp_path / "stdio_child.py"
    script.write_text(_STDIO_CHILD, encoding="utf-8")
    errors = tmp_path / "stderr.txt"
    with open(errors, "wb") as err:
        proc = subprocess.Popen(
            [sys.executable, "-B", str(script), str(ROOT), str(ROOT / "shared-py")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err, cwd=str(tmp_path))
    lines = queue.Queue()
    reader = threading.Thread(target=lambda: ([lines.put(line) for line in proc.stdout],
                                              lines.put(b"")), daemon=True)
    reader.start()

    def send(message):
        proc.stdin.write(json.dumps(message).encode() + b"\n")
        proc.stdin.flush()

    def reply():
        line = lines.get(timeout=60)
        assert line, f"the server closed stdout: {errors.read_text(errors='replace')[-2000:]}"
        return json.loads(line)

    try:
        send(json.loads(_init()))
        init = reply()
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "ping"}})
        send({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
              "params": {"name": "ping", "arguments": None}})
        calls = {r["id"]: r for r in (reply(), reply())}
    finally:
        proc.stdin.close()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        reader.join(timeout=10)
        proc.stdout.close()
    assert init["result"]["capabilities"] == {
        "logging": {}, "prompts": {"listChanged": False},
        "resources": {"subscribe": False, "listChanged": False}, "tools": {"listChanged": True}}
    assert init["result"]["serverInfo"] == {"name": "BrowserPlug", "version": __version__}
    for request_id in (2, 3):
        result = calls[request_id]["result"]
        assert result["isError"] is False, (request_id, result)
        assert result["content"] == [{"type": "text", "text": "pong"}], (request_id, result)


def test_construction_warns_nothing_and_opens_no_telemetry_spans():
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        mcp = BrowserPlugMCPServer(ToolRegistry())
    assert [str(w.message) for w in caught] == []
    # The SDK seeds an OpenTelemetry middleware, which is dropped; list_changed's session
    # recorder is the only one expected in the chain, so a middleware a later SDK seeds
    # shows up here for review.
    assert mcp.middleware == [mcp.list_changed.middleware]


# =============================================================================
# The handlers beside tools/list and tools/call
# =============================================================================

async def test_prompts_and_resources_are_empty_and_their_lookups_fail_as_before():
    async with Client(BrowserPlugTools(_Facade()).mcp, mode="legacy") as client:
        assert (await client.list_prompts()).prompts == []
        assert (await client.list_resources()).resources == []
        assert (await client.list_resource_templates()).resource_templates == []
        with pytest.raises(MCPError) as prompt:
            await client.get_prompt("x")
        with pytest.raises(MCPError) as resource:
            await client.read_resource("file:///x")
        with pytest.raises(MCPError) as completion:
            await client.complete(mcp_types.PromptReference(type="ref/prompt", name="x"),
                                  {"name": "a", "value": "b"})
    assert (prompt.value.code, prompt.value.message) == (
        mcp_types.INVALID_PARAMS, "Unknown prompt: 'x'")
    assert (resource.value.code, resource.value.message, resource.value.data) == (
        mcp_types.INVALID_PARAMS, "Resource not found: 'file:///x'", {"uri": "file:///x"})
    assert completion.value.code == mcp_types.METHOD_NOT_FOUND


async def test_logging_set_level_and_ping_answer_empty():
    async with Client(BrowserPlugTools(_Facade()).mcp, mode="legacy") as client:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")      # the client side marks both deprecated
            assert await client.set_logging_level("info") == mcp_types.EmptyResult()
            await client.send_ping()


async def test_tools_list_is_the_registry_snapshot_in_order():
    tools = BrowserPlugTools(_Facade())
    async with Client(tools.mcp, mode="legacy") as client:
        listed = await client.list_tools()
    assert [t.name for t in listed.tools] == [s.name for s in tools.registry.snapshot()]
    assert listed.next_cursor is None
    assert all(t.title is None and t.output_schema is None and t.meta is None
               for t in listed.tools)


@pytest.mark.parametrize("arguments", [None, {}])
async def test_a_call_without_arguments_is_a_call_with_none(arguments):
    async def ping() -> str:
        return "pong"

    mcp = BrowserPlugMCPServer(ToolRegistry([ToolSpec.from_function(ping)]))
    ctx = type("Ctx", (), {"request_id": 1, "lifespan_context": {}})()
    params = mcp_types.CallToolRequestParams(name="ping", arguments=arguments)
    result = await mcp._on_call_tool(ctx, params)
    assert result.is_error is False and result.content[0].text == "pong"


def _raise_lines(caplog, name):
    """The WARNING lines the dispatcher logged for tool `name`, and every INFO+ line."""
    lines = [r.getMessage() for r in caplog.records if r.levelno >= logging.INFO]
    return [line for line in lines if line.startswith(f"tools/call {name!r} raised ")], lines


async def test_a_raising_tool_logs_where_it_raised_but_not_the_message(caplog):
    """The rotating log is the only record a detached server keeps, and nothing raises its
    level, so this WARNING is all an operator gets: the class and the frames locate the
    bug; the message (page data) stays with the client."""
    async def leaky() -> str:
        raise RuntimeError("https://example.org/?q=private")

    mcp = BrowserPlugMCPServer(ToolRegistry([ToolSpec.from_function(leaky)]))
    with caplog.at_level(logging.INFO):
        async with Client(mcp, mode="legacy") as client:
            result = await client.call_tool("leaky", {})
    assert result.is_error is True
    assert result.content[0].text == "Error calling tool 'leaky': https://example.org/?q=private"
    (line,), lines = _raise_lines(caplog, "leaky")
    assert line.startswith("tools/call 'leaky' raised RuntimeError at mcp_server.py:"), line
    assert re.search(r"mcp_server\.py:\d+ in _on_call_tool; ", line), line
    raised_at = leaky.__code__.co_firstlineno + 1
    assert line.endswith(f"; test_mcp_server.py:{raised_at} in leaky"), line
    assert not any("private" in text or "example.org" in text for text in lines), lines


async def test_a_chained_exception_logs_each_link_and_still_no_message(caplog):
    async def caused() -> str:
        try:
            {}["secret-key"]
        except KeyError as e:
            raise ValueError("secret-value") from e

    async def during() -> str:
        try:
            raise OSError("secret-path")
        except OSError:
            return "".join([None, "secret-join"])

    mcp = BrowserPlugMCPServer(ToolRegistry([ToolSpec.from_function(caused),
                                             ToolSpec.from_function(during)]))
    with caplog.at_level(logging.INFO):
        async with Client(mcp, mode="legacy") as client:
            await client.call_tool("caused", {})
            await client.call_tool("during", {})
    (caused_line,), lines = _raise_lines(caplog, "caused")
    (during_line,), _ = _raise_lines(caplog, "during")
    first = caused.__code__.co_firstlineno
    assert caused_line.startswith("tools/call 'caused' raised ValueError at "), caused_line
    assert caused_line.endswith(f"test_mcp_server.py:{first + 4} in caused | caused by KeyError "
                                f"at test_mcp_server.py:{first + 2} in caused"), caused_line
    assert during_line.startswith("tools/call 'during' raised TypeError at "), during_line
    assert " | while handling OSError at test_mcp_server.py:" in during_line, during_line
    assert not any("secret" in text for text in lines), lines


async def test_a_client_cancel_reaches_the_dispatcher_as_a_cancellation(caplog):
    """A cancelled tools/call stays cancelled: the dispatcher's `except Exception` lets the
    CancelledError through, so it is not turned into an 'Error calling tool' result, not
    logged as a raise and not filed as a verdict, and the session goes on serving.

    Cancelling the client's call task makes the SDK client send notifications/cancelled
    for that request id; the server's dispatcher then cancels the handler's scope, which is
    how an interrupted agent call arrives."""
    started, never = asyncio.Event(), asyncio.Event()
    tool_saw, handled, cancels, recorded = [], [], [], []

    async def hang() -> str:
        started.set()
        try:
            await never.wait()
        except asyncio.CancelledError:
            tool_saw.append("CancelledError")
            raise
        return "never"

    async def ping() -> str:
        return "pong"

    class Observed(BrowserPlugMCPServer):
        async def _on_call_tool(self, ctx, params):
            try:
                result = await super()._on_call_tool(ctx, params)
            except BaseException as e:
                handled.append((params.name, ctx.request_id, type(e).__name__))
                raise
            handled.append((params.name, ctx.request_id, "returned"))
            return result

    async def on_cancelled(ctx, params):
        cancels.append(params.request_id)

    mcp = Observed(ToolRegistry([ToolSpec.from_function(hang), ToolSpec.from_function(ping)]),
                   record=lambda *a: recorded.append(a))
    # Only observes: the dispatcher acts on notifications/cancelled before any handler.
    mcp.add_notification_handler("notifications/cancelled",
                                 mcp_types.CancelledNotificationParams, on_cancelled)
    with caplog.at_level(logging.INFO):
        async with Client(mcp, mode="legacy") as client:
            call = asyncio.create_task(client.call_tool("hang", {}))
            await asyncio.wait_for(started.wait(), 10)
            call.cancel()
            with pytest.raises(asyncio.CancelledError):
                await call
            for _ in range(500):
                if handled and cancels:
                    break
                await asyncio.sleep(0.01)
            after = await client.call_tool("ping", {})
    assert tool_saw == ["CancelledError"]
    name, request_id, how = handled[0]
    assert cancels == [request_id], "the cancel arrived as notifications/cancelled for this call"
    assert (name, how) == ("hang", "CancelledError"), \
        "the dispatcher must let the cancellation through, not answer it"
    assert after.is_error is False and after.content[0].text == "pong"
    assert [name for name, *_ in recorded] == ["ping"], "a cancelled call files no verdict"
    assert _raise_lines(caplog, "hang")[0] == []


# =============================================================================
# The production HTTP app behind the real wrapper
# =============================================================================

async def test_request_headers_reach_the_tool_and_the_outcome_is_filed_under_the_session():
    seen = []

    async def whoami() -> str:
        return (f"pid={request_context.header('x-browserplug-client-pid')} "
                f"session={request_context.header('mcp-session-id')}")

    mcp = BrowserPlugMCPServer(ToolRegistry([ToolSpec.from_function(whoami)]),
                               record=lambda *a: seen.append(a))
    server = _server()
    app = mcp.http_app()
    pids, texts = ("4242", "5151"), []
    async with app.router.lifespan_context(app):
        for pid in pids:
            async with httpx2.AsyncClient(
                    transport=httpx2.ASGITransport(app=server._wrap_client_tracking(app)),
                    base_url="http://127.0.0.1",
                    headers={"x-browserplug-client-pid": pid}) as http:
                transport = streamable_http_client("http://127.0.0.1/mcp", http_client=http)
                async with Client(transport, mode="legacy") as client:
                    result = await client.call_tool("whoami", {})
            texts.append(result.content[0].text)
    sessions = [text.split(" session=")[1] for text in texts]
    assert texts == [f"pid={pid} session={s}" for pid, s in zip(pids, sessions)]
    assert "None" not in sessions and len(set(sessions)) == 2
    # Each verdict is filed under (its own session header, its JSON-RPC id).
    assert [call[0] for _name, _outcome, call in seen] == sessions
    assert request_context.current_headers() is None, "the binding ends with the request"


async def test_a_tools_verdict_reaches_health_last_command_end_to_end():
    """The real server, its real tools and record sink, over the production HTTP app: the
    verdict of a failing call lands on the record the ASGI wrapper opened for THAT call
    (session, JSON-RPC id), which is what /health's last_command.outcome shows."""
    server = _server()
    app = server.mcp_app.http_app()
    posted = []

    async def note(request):
        if request.method == "POST":
            body = json.loads(request.content)
            if body.get("method") == "tools/call":
                posted.append((request.headers.get("mcp-session-id"), body["id"]))

    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app=server._wrap_client_tracking(app)),
                base_url="http://127.0.0.1", event_hooks={"request": [note]}) as http:
            transport = streamable_http_client("http://127.0.0.1/mcp", http_client=http)
            async with Client(transport, mode="legacy") as client:
                result = await client.call_tool("tabs", {"action": "list"})
    assert result.is_error is True, "no browser is connected"
    ((session, request_id),) = posted
    record = server.telemetry._calls[(session[:8], str(request_id))]
    assert record["tool"] == "tabs"
    assert record["outcome"]["code"] == EXTENSION_DISCONNECTED
    assert server.get_health_info()["last_command"]["outcome"]["code"] == EXTENSION_DISCONNECTED


async def test_http_app_pins_no_idle_expiry_sse_replies_and_the_body_cap():
    """The transport settings http_app passes on purpose. An idle expiry would answer a
    quiet HTTP-direct client's next call 404; json replies and a different cap are not
    what any client has been served."""
    mcp = BrowserPlugMCPServer(ToolRegistry())
    app = mcp.http_app()
    assert mcp.session_manager.session_idle_timeout is None
    assert mcp.session_manager.json_response is False
    init = _init()
    async with app.router.lifespan_context(app):
        async with _http(app) as http:
            reply = await http.post("/mcp", headers=MCP_HEADERS, content=init)
            at_cap = await http.post("/mcp", headers=MCP_HEADERS,
                                     content=init.ljust(MAX_REQUEST_BODY).encode())
            over = await http.post("/mcp", headers=MCP_HEADERS,
                                   content=init.ljust(MAX_REQUEST_BODY + 1).encode())
    assert reply.status_code == 200
    assert reply.headers["content-type"].startswith("text/event-stream")
    assert MAX_REQUEST_BODY == 4 * 1024 * 1024
    assert at_cap.status_code == 200, at_cap.text[:200]
    assert over.status_code == 413, over.text[:200]


async def test_a_lost_session_reads_as_lost_to_the_stdio_relay():
    """The relay rebuilds its session on these two answers, and only on them
    (launcher/stdio_relay.py _session_lost)."""
    from launcher.stdio_relay import _session_lost

    server = _server()
    app = server.mcp_app.http_app()
    tools_list = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    async with app.router.lifespan_context(app):
        async with _http(server._wrap_client_tracking(app)) as http:
            unknown = await http.post("/mcp", content=tools_list,
                                      headers=dict(MCP_HEADERS, **{"mcp-session-id": "deadbeef"}))
            headerless = await http.post("/mcp", content=tools_list, headers=MCP_HEADERS)
    assert unknown.status_code == 404, unknown.text
    assert json.loads(unknown.text)["error"]["message"] == "Session not found"
    assert _session_lost(unknown.status_code, _frames(unknown)) is True
    assert headerless.status_code == 400, headerless.text
    assert "Missing session ID" in json.loads(headerless.text)["error"]["message"]
    assert _session_lost(headerless.status_code, _frames(headerless)) is True


async def test_a_slash_path_is_redirected_to_mcp():
    """POST /mcp/ answers 307 to /mcp, which clients follow; the timing stamp in
    HttpEndpoints ignores it."""
    server = _server()
    app = server.mcp_app.http_app()
    async with app.router.lifespan_context(app):
        async with _http(server._wrap_client_tracking(app)) as http:
            reply = await http.post("/mcp/", headers=MCP_HEADERS, content=_init())
    assert reply.status_code == 307
    assert reply.headers["location"].endswith("/mcp")
