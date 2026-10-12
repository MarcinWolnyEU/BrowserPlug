"""The whole client path, once, over real transports.

    stdin JSON-RPC -> launcher.py -> stdio_relay -> HTTP POST /mcp -> ASGI wrapper
      -> MCP server -> tool -> extension WebSocket (encrypted) -> fake extension -> back

Every other test cuts this chain somewhere: the relay tests use a stub backend, the tool
tests call the tool function, the integration harness awaits `tool.fn(...)` directly (no
JSON-RPC, no argument validation, no middleware, no HTTP). Here nothing between the client's
stdin and the extension's socket is faked.

What is real: the launcher as a subprocess (it seeks the server through the
BROWSERPLUG_MCP_PORT pin, so it also exercises `ensure_server`), a BrowserPlugServer started
the way the process entry point starts it (`start_server`: uvicorn thread plus the extension
WebSocket listener) on ports allocated for this test, and an extension that speaks the
encrypted handshake (secure_ext). What is fake: the browser behind the extension, i.e. the
answers to `tabs.list`.

The fixtures are reusable on purpose: later phases add relay-session cases (the session
race, the double initialize replay) as more tests that take `relay` and `live_server`.
"""

import asyncio
import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest_asyncio
import websockets

import test_imports  # noqa: F401
from port_coordinator import allocate_port
from secure_ext import SecureClient, device_token
from browserplug_shared.mcp_port import MCP_PORT_ENV
from server.server import BrowserPlugServer

ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = ROOT / "launcher" / "launcher.py"
# Runs LAUNCHER with this process (the fixture server) declared as ours; see the file.
LAUNCHER_AS_OURS = Path(__file__).resolve().parent / "launcher_as_ours.py"

CLIENT_INFO = {"name": "e2e-test-client", "version": "0"}
PROTOCOL_VERSION = "2025-06-18"

TABS = {
    "tabs": [{"id": 11, "windowId": 1, "active": True, "title": "Fixture Page",
              "url": "https://example.org/fixture", "lastAccessed": int(time.time() * 1000)}],
    "windows": [{"id": 1, "focused": True}],
    "lastFocusedWindowId": 1,
}


def initialize_frame(msg_id=1):
    return {"jsonrpc": "2.0", "id": msg_id, "method": "initialize",
            "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                       "clientInfo": CLIENT_INFO}}


def call_frame(msg_id, name, arguments=None):
    return {"jsonrpc": "2.0", "id": msg_id, "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}}}


# --- the server side ------------------------------------------------------------------

class LiveServer:
    """A BrowserPlugServer serving for real, plus the one fake extension attached to it."""

    def __init__(self, server, ws_port, mcp_port):
        self.server = server
        self.ws_port = ws_port
        self.mcp_port = mcp_port
        self.task = None
        self.sock = None
        self.extension = None
        self.requests = []          # every request frame the extension received
        self.answer = self.default_answer
        self._reader = None

    @staticmethod
    def default_answer(request):
        """What the fake browser says to a request: tabs.list gets a tab, all else an error."""
        if request.get("action") == "tabs.list":
            return {"type": "response", "data": TABS}
        return {"type": "error", "data": {"message": f"fixture has no {request.get('action')}"}}

    async def attach_extension(self, instance_id="e2e-ff"):
        """Connect a fake extension through the encrypted handshake and start answering."""
        self.sock = await websockets.connect(f"ws://127.0.0.1:{self.ws_port}")
        hello = {"type": "extension_hello",
                 "data": {"version": "1.4.0", "instance_id": instance_id,
                          "tokenBootstrap": True}}
        self.extension = await SecureClient.connect(
            self.sock, device_token(self.server), hello)
        await wait_for(lambda: instance_id in self.server.browsers,
                       "the hello registering the fake browser")
        self._reader = asyncio.create_task(self._answer_forever())

    async def _answer_forever(self):
        try:
            while True:
                frame = await self.extension.recv()
                if frame.get("type") != "request":
                    continue    # acks and pings are the server talking, not a request
                self.requests.append(frame)
                reply = {"id": frame["id"], "action": frame.get("action"),
                         **self.answer(frame)}
                await self.extension.send(reply)
        except (websockets.exceptions.ConnectionClosed, asyncio.CancelledError):
            return

    async def stop(self):
        """Tear down in the order that leaves no overlapped I/O behind. A proactor loop
        (Windows) will not close while a listener or a socket it owns is still being
        torn down, and that shows up as a hang in the fixture's loop teardown, not as a
        failure, so every close here is awaited."""
        if self._reader is not None:
            self._reader.cancel()
            await asyncio.gather(self._reader, return_exceptions=True)
        if self.sock is not None:
            await self.sock.close()
        listener = self.server.lifecycle.websocket_server
        self.server.lifecycle.stop()
        if listener is not None:
            await asyncio.wait_for(listener.wait_closed(), 10)
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)


async def wait_for(predicate, what, timeout=10.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        await asyncio.sleep(0.02)


@pytest_asyncio.fixture
async def live_server():
    """A real server on ports of this test's own, started as the entry point starts it."""
    ws_port, mcp_port = allocate_port(), allocate_port()
    server = BrowserPlugServer(host="localhost", port=ws_port, mcp_port=mcp_port,
                               start_mcp=True)
    live = LiveServer(server, ws_port, mcp_port)
    live.task = asyncio.create_task(server.start_server())
    try:
        await wait_for(lambda: server.lifecycle.websocket_server is not None,
                       "the extension WebSocket listener")
        await wait_for(lambda: server.lifecycle.mcp_thread is not None
                       and server.lifecycle.mcp_thread.is_alive(), "the MCP HTTP thread")
        assert server.mcp_port == mcp_port, "a relocated MCP port would miss the pin below"
        yield live
    finally:
        await live.stop()


# --- the client side ------------------------------------------------------------------

class Relay:
    """The launcher as an MCP client's subprocess: JSON lines in, JSON lines out."""

    def __init__(self, mcp_port, extra_env=None):
        env = dict(os.environ)
        env[MCP_PORT_ENV] = str(mcp_port)     # a pin: find that server, never spawn one
        env["BROWSERPLUG_TEST_OURS_PID"] = str(os.getpid())   # the fixture server is ours
        env.update(extra_env or {})
        self.proc = subprocess.Popen(
            [sys.executable, str(LAUNCHER_AS_OURS), str(LAUNCHER)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=env, cwd=str(ROOT))
        self.frames = queue.Queue()
        self.stderr_lines = []
        self.notifications = []
        self.parked = {}
        self._threads = [
            threading.Thread(target=self._pump_stdout, daemon=True),
            threading.Thread(target=self._pump_stderr, daemon=True)]
        for t in self._threads:
            t.start()

    def _pump_stdout(self):
        for line in self.proc.stdout:
            self.frames.put(line.decode("utf-8", "replace").rstrip("\r\n"))
        self.frames.put(None)

    def _pump_stderr(self):
        for line in self.proc.stderr:
            self.stderr_lines.append(line.decode("utf-8", "replace").rstrip())

    def write(self, frame):
        """One line on the launcher's stdin, exactly as an MCP client writes it."""
        data = frame if isinstance(frame, str) else json.dumps(frame)
        self.proc.stdin.write(data.encode("utf-8") + b"\n")
        self.proc.stdin.flush()

    async def read_frame(self, timeout=20.0):
        """The next line on stdout, parsed. Never blocks the event loop the server shares.

        A frame that does not come is a failure that names what the launcher said, not a
        test that hangs until the runner's timeout kills it without a word."""
        try:
            raw = await asyncio.to_thread(self.frames.get, True, timeout)
        except queue.Empty:
            raise AssertionError(
                f"no frame from the launcher within {timeout:.0f}s; its stderr: "
                + " | ".join(self.stderr_lines)) from None
        assert raw is not None, ("the launcher closed stdout: "
                                 + " | ".join(self.stderr_lines))
        return json.loads(raw)

    async def response_to(self, msg_id, timeout=20.0):
        """The reply carrying `msg_id`. Replies to other ids that arrive first are parked
        for their own `response_to`, since overlapping calls finish in any order; anything
        with a method (a notification) goes to `notifications`."""
        deadline = time.monotonic() + timeout
        while msg_id not in self.parked:
            frame = await self.read_frame(max(0.1, deadline - time.monotonic()))
            if "method" in frame:
                self.notifications.append(frame)
            else:
                self.parked[frame.get("id")] = frame
        return self.parked.pop(msg_id)

    async def handshake(self, msg_id=1):
        """initialize + notifications/initialized. Returns the initialize reply."""
        self.write(initialize_frame(msg_id))
        reply = await self.response_to(msg_id)
        self.write({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return reply

    async def call(self, msg_id, name, arguments=None):
        self.write(call_frame(msg_id, name, arguments))
        return await self.response_to(msg_id)

    def assert_adopted_without_spawning(self):
        """The relay found the fixture server and started no server of its own.

        The launcher only adopts a /health whose server_pid is this checkout's server
        (mcp_port.server_pid_is_ours). Without the seam in `launcher_as_ours`, the launcher
        walks past the in-process fixture, logs that it ignored it, and may spawn a real
        server on the pinned port. The tests still pass that way, on a path nobody meant."""
        text = " | ".join(self.stderr_lines)
        assert "ignoring a BrowserPlug-looking" not in text, text
        assert "spawning a detached instance" not in text, text
        assert "spawned server" not in text, text

    def close(self):
        """What a client exiting does: close stdin. Returns the exit code."""
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            code = self.proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            raise
        for t in self._threads:
            t.join(timeout=5)      # the pumps end at EOF once the process is gone
        self.proc.stdout.close()
        self.proc.stderr.close()
        return code


@pytest_asyncio.fixture
async def relay(live_server):
    r = Relay(live_server.mcp_port)
    try:
        yield r
    finally:
        if r.proc.poll() is None:
            await asyncio.to_thread(r.close)
    r.assert_adopted_without_spawning()


def text_of(reply):
    """The concatenated text content of a tools/call result."""
    return "".join(c.get("text", "") for c in reply["result"]["content"])


# --- the tests ------------------------------------------------------------------------

async def test_a_tool_call_crosses_every_layer_and_comes_back(live_server, relay):
    await live_server.attach_extension()

    init = await relay.handshake(msg_id=7)
    assert init["id"] == 7 and "error" not in init, init
    assert init["result"]["serverInfo"]["name"] == "BrowserPlug", \
        "the MCP server answered initialize itself"

    reply = await relay.call(42, "tabs", {"action": "list"})

    assert reply["id"] == 42, "the reply is tied to the client's own id"
    assert reply["result"].get("isError") in (None, False), reply
    body = text_of(reply)
    assert "Fixture Page" in body and "https://example.org/fixture" in body, body
    # The extension really was asked, over the wire, in the shape the extension handles.
    assert [r["action"] for r in live_server.requests] == ["tabs.list"]
    assert live_server.requests[0]["type"] == "request"
    assert live_server.requests[0]["id"], "the server mints a request id"
    assert await asyncio.to_thread(relay.close) == 0


async def test_tools_list_reaches_the_client_through_the_relay(live_server, relay):
    await relay.handshake()
    relay.write({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    reply = await relay.response_to(2)
    names = {t["name"] for t in reply["result"]["tools"]}
    assert {"tabs", "navigate", "page_read", "page_snapshot", "page_act", "page_eval",
            "page_screenshot", "requests", "server_info"} <= names, sorted(names)


async def test_the_extensions_error_becomes_a_tool_error_not_a_transport_error(
        live_server, relay):
    """An extension that refuses the request is a failed TOOL CALL (isError with the
    reason as text), and the JSON-RPC exchange itself succeeds."""
    await live_server.attach_extension()
    live_server.answer = lambda request: {
        "type": "error", "data": {"message": "fixture refuses everything"}}
    await relay.handshake()

    reply = await relay.call(5, "tabs", {"action": "list"})

    assert reply["id"] == 5 and "error" not in reply, reply
    assert reply["result"]["isError"] is True
    assert "fixture refuses everything" in text_of(reply)


async def test_a_call_with_no_extension_attached_says_so(live_server, relay):
    """No browser connected is the failure an agent meets most: it must come back as a
    tool error that names the cause, not as a hang or a JSON-RPC error."""
    await relay.handshake()

    reply = await relay.call(9, "tabs", {"action": "list"})

    assert reply["id"] == 9 and "error" not in reply, reply
    assert reply["result"]["isError"] is True
    assert "extension" in text_of(reply).lower()


async def test_replies_keep_their_own_ids_when_calls_overlap(live_server, relay):
    """Two calls in flight at once, answered out of order by the extension, each land on
    the id that asked. The relay runs each frame on its own worker, so this is the case
    where a mixed-up id would show."""
    await live_server.attach_extension()
    await relay.handshake()
    relay.write(call_frame(21, "tabs", {"action": "list"}))
    relay.write(call_frame(22, "tabs", {"action": "find", "url": "fixture"}))

    first, second = await relay.response_to(21), await relay.response_to(22)

    assert "Fixture Page" in text_of(first)
    assert "match" in text_of(second) and "Fixture Page" in text_of(second)


async def test_a_result_holding_a_unicode_line_separator_reaches_the_client_whole(
        live_server, relay):
    """The server's SSE body carries U+2028 (and friends) raw, since JSON allows it and
    SSE splits only at CR and LF. A relay that cut frames with str.splitlines() delivered
    half a frame, and the client waited for this id until its own timeout."""
    seps = "  \x85"
    tabs = json.loads(json.dumps(TABS))
    tabs["tabs"][0]["title"] = f"Fixture{seps}Page"
    live_server.answer = lambda request: {"type": "response", "data": tabs}
    await live_server.attach_extension()
    await relay.handshake()

    reply = await relay.call(51, "tabs", {"action": "list"})

    assert reply["id"] == 51 and "error" not in reply, reply
    assert f"Fixture{seps}Page" in text_of(reply), text_of(reply)


def forget_server_sessions():
    """What a /restart does to the MCP sessions: the server no longer knows any of them.
    Returns the session manager, so a test can count the sessions created afterwards."""
    import gc
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    managers = [o for o in gc.get_objects() if isinstance(o, StreamableHTTPSessionManager)
                and o._server_instances]
    assert len(managers) == 1, "expected the one live session manager"
    manager = managers[0]
    manager._server_instances.clear()
    manager._session_owners.clear()
    return manager


async def test_two_concurrent_calls_after_a_restart_rebuild_the_session_once(
        live_server, relay):
    """Both calls hit the dead session together. One rebuilds, the other must wait for it
    and retry (not go out with no session id and fail -32600), and the server must end up
    with exactly one new session, not an orphan from a second initialize replay."""
    await live_server.attach_extension()
    await relay.handshake()
    manager = forget_server_sessions()
    relay.write(call_frame(31, "tabs", {"action": "list"}))
    relay.write(call_frame(32, "tabs", {"action": "list"}))

    first, second = await relay.response_to(31), await relay.response_to(32)

    for reply in (first, second):
        assert "error" not in reply and reply["result"].get("isError") in (None, False), reply
        assert "Fixture Page" in text_of(reply)
    assert len(manager._server_instances) == 1, list(manager._server_instances)
