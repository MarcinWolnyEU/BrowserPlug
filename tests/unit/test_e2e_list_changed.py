"""tools/list_changed end to end, over real transports.

A BrowserPlugServer started the way the process entry point starts it (uvicorn thread plus
the extension WebSocket listener), encrypted fake extensions (secure_ext), the production
HTTP wrapper with its era gate, SDK clients over TCP, and the stdio relay. The surface
change is real: a second browser connecting registers the `browsers` tool on the
WebSocket loop, while the clients' sessions live on uvicorn's.

The relay runs in-process (StdioRelay with a 0.2 s poll instead of the launcher's 3 s), so
"exactly one notice, and only the relay's own" costs well under a second of polling.
"""

import asyncio
import json
import os
import queue
import threading

import pytest_asyncio
import websockets
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS

import test_imports  # noqa: F401  (path setup)
from launcher.stdio_relay import StdioRelay
from port_coordinator import allocate_port
from secure_ext import SecureClient, device_token
from server.server import BrowserPlugServer
from server.tools.mcp_server import BrowserPlugMCPServer
from test_e2e_stdio_path import LiveServer, initialize_frame, wait_for
from test_list_changed import NINE, GetStreams, Heard, http_client, names, wait_until

NOTICE = {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}


@pytest_asyncio.fixture
async def live(monkeypatch):
    """test_e2e_stdio_path's live server, with GetStreams between the HTTP wrapper and
    the MCP app so a test can wait for a client's GET stream before it changes anything."""
    apps = []
    serve = BrowserPlugMCPServer.http_app

    def http_app(self, **kwargs):
        apps.append(GetStreams(serve(self, **kwargs)))
        return apps[-1]

    monkeypatch.setattr(BrowserPlugMCPServer, "http_app", http_app)
    ws_port, mcp_port = allocate_port(), allocate_port()
    server = BrowserPlugServer(host="localhost", port=ws_port, mcp_port=mcp_port,
                               start_mcp=True)
    running = LiveServer(server, ws_port, mcp_port)
    running.task = asyncio.create_task(server.start_server())
    try:
        await wait_for(lambda: server.lifecycle.websocket_server is not None,
                       "the extension WebSocket listener")
        await wait_for(lambda: server.lifecycle.mcp_thread is not None
                       and server.lifecycle.mcp_thread.is_alive(), "the MCP HTTP thread")
        await wait_for(lambda: apps, "the MCP app")
        running.get_streams = apps[0]
        running.url = f"http://127.0.0.1:{mcp_port}/mcp"
        yield running
    finally:
        await running.stop()


async def attach_second_browser(live, instance_id="e2e-ff-2"):
    """A second encrypted extension: the server registers the `browsers` tool."""
    sock = await websockets.connect(f"ws://127.0.0.1:{live.ws_port}")
    hello = {"type": "extension_hello",
             "data": {"version": "1.4.0", "instance_id": instance_id, "tokenBootstrap": True}}
    await SecureClient.connect(sock, device_token(live.server), hello)
    await wait_for(lambda: instance_id in live.server.browsers, "the second browser")
    return sock


# --- the SDK over HTTP ------------------------------------------------------------------

async def test_a_second_browser_reaches_http_clients_as_one_notice_each_way(live):
    """A legacy client holding its GET stream, and an auto-negotiating one (what Claude
    Code's HTTP transport does) holding a subscriptions/listen stream, each hear one
    notification when the second browser connects and one when it leaves; each re-list
    shows the surface that caused it."""
    await live.attach_extension()
    legacy_heard, listened = Heard(), []
    async with http_client() as http_legacy, http_client() as http_auto:
        async with Client(streamable_http_client(live.url, http_client=http_legacy),
                          mode="legacy", message_handler=legacy_heard) as legacy, \
                Client(streamable_http_client(live.url, http_client=http_auto)) as auto:
            # The server serves 2026-07-28 under its default era (`all`), so auto
            # negotiation ends there, with no session and no GET stream.
            assert legacy.protocol_version in HANDSHAKE_PROTOCOL_VERSIONS
            assert auto.protocol_version == "2026-07-28"
            assert names(await legacy.list_tools()) == NINE
            await wait_until(lambda: live.get_streams.open == 1, "the legacy GET stream")

            async with auto.listen(tools_list_changed=True) as subscription:
                async def consume():
                    async for event in subscription:
                        listened.append(event)

                reader = asyncio.create_task(consume())
                second = await attach_second_browser(live)
                try:
                    await wait_until(lambda: len(legacy_heard) and listened,
                                     "both clients told of the connect", timeout=3)
                    await asyncio.sleep(1.0)
                    assert (len(legacy_heard), len(listened)) == (1, 1)
                    assert names(await legacy.list_tools()) == NINE + ["browsers"]
                    assert names(await auto.list_tools()) == NINE + ["browsers"]
                finally:
                    await second.close()
                await wait_until(lambda: len(legacy_heard) == 2 and len(listened) == 2,
                                 "both clients told of the leave", timeout=3)
                await asyncio.sleep(1.0)
                assert (len(legacy_heard), len(listened)) == (2, 2)
                assert names(await legacy.list_tools()) == NINE
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)


# --- the stdio relay ----------------------------------------------------------------------

class RelayStdout:
    """The relay's stdout as parsed frames, readable from the test's loop."""

    def __init__(self):
        self.frames = queue.Queue()
        self._partial = b""

    def write(self, data):
        self._partial += data
        while b"\n" in self._partial:
            line, self._partial = self._partial.split(b"\n", 1)
            if line.strip():
                self.frames.put(json.loads(line))

    def flush(self):
        pass

    async def next(self, timeout=10.0):
        try:
            return await asyncio.to_thread(self.frames.get, True, timeout)
        except queue.Empty:
            raise AssertionError(f"no frame from the relay within {timeout:.0f}s") from None


class RelayStdin:
    """The relay's stdin: one JSON line per send, end of input on close."""

    def __init__(self):
        self._lines = queue.Queue()

    def send(self, frame):
        self._lines.put(json.dumps(frame).encode("utf-8") + b"\n")

    def close(self):
        self._lines.put(None)

    def __iter__(self):
        while (line := self._lines.get()) is not None:
            yield line


class PollCountingRelay(StdioRelay):
    """StdioRelay counting its /health polls: the watcher records its baseline on the
    first poll and only a later poll can see a change."""

    polls = 0

    def health(self):
        try:
            return super().health()
        finally:
            self.polls += 1


async def test_the_relay_hears_exactly_its_own_notice(live):
    """The relay holds no GET stream, so the server's push never reaches it: its client
    gets exactly one notifications/tools/list_changed for the change, the one the relay
    synthesizes from /health, while an SDK client holding a GET stream is pushed to."""
    await live.attach_extension()
    out, stdin, logged = RelayStdout(), RelayStdin(), []
    relay = PollCountingRelay(live.mcp_port, lambda *a: logged.append(" ".join(map(str, a))),
                              out=out, poll_seconds=0.2, client_pid=os.getpid())
    runner = threading.Thread(target=relay.run, kwargs={"stdin": stdin},
                              name="relay-under-test", daemon=True)
    runner.start()
    second = None
    try:
        stdin.send(initialize_frame(1))
        assert (await out.next())["id"] == 1
        stdin.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        stdin.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert [t["name"] for t in (await out.next())["result"]["tools"]] == NINE

        pushed = Heard()
        async with http_client() as http:
            async with Client(streamable_http_client(live.url, http_client=http),
                              mode="legacy", message_handler=pushed):
                await wait_until(lambda: live.get_streams.open == 1, "the GET stream")
                await wait_until(lambda: relay.polls >= 2, "the relay's /health baseline")
                second = await attach_second_browser(live)
                await wait_until(lambda: len(pushed) == 1, "the server's push", timeout=3)
                assert await out.next(timeout=5) == NOTICE
                stdin.send({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
                reply = await out.next()
                assert reply["id"] == 3
                assert [t["name"] for t in reply["result"]["tools"]] == NINE + ["browsers"]
                polls = relay.polls
                await wait_until(lambda: relay.polls >= polls + 5, "five more polls")
                assert out.frames.empty(), f"an extra frame: {out.frames.get_nowait()}"
                assert len(pushed) == 1
        assert len([line for line in logged if "tool surface changed" in line]) == 1, logged
    finally:
        if second is not None:
            await second.close()
        stdin.close()
        await asyncio.to_thread(runner.join, 20)
    assert not runner.is_alive(), "the relay did not stop at the end of its input"
