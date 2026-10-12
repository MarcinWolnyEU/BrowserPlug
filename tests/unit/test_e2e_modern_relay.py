"""MCP 2026-07-28 through the REAL launcher, end to end.

    SDK client (stdio, mode="auto" or "legacy") -> launcher.py -> stdio_relay + relay_era
      -> POST /mcp -> ASGI wrapper (era gate, connection identity) -> MCP server -> tool

Every other test cuts this chain: test_relay_era.py and TestModern in test_stdio_relay.py
drive the relay against fake backends, test_modern_http.py talks to the HTTP wrapper, and
test_e2e_stdio_path.py runs the launcher with hand-written JSON-RPC frames of the handshake
era. Here the client is the SDK's own, so the headers the relay synthesizes are judged by the
SDK's own server-side validation, and the launcher is a subprocess exactly as an MCP client
starts it. The server is one of two kinds:

  * a BrowserPlugServer in this process (test_e2e_stdio_path's `live_server`: encrypted fake
    extensions, so a second browser can connect and change the tool surface for real);
  * a real server.py process on a sandbox's ports (test_server_process_lifecycle.Sandbox),
    which can be killed and respawned, so `server_pid` moves for real.

The relay polls /health every 3 s and takes its first observation as a baseline, so a test
that wants a change announced waits one poll period after the launcher starts.
"""

import asyncio
import os
import sys
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.shared.subscriptions import ToolsListChanged
from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS

import test_imports  # noqa: F401
from browserplug_shared import mcp_era
from browserplug_shared.config_store import STATE_DIR_ENV
from browserplug_shared.mcp_port import MCP_PORT_ENV
from launcher.stdio_relay import TOOLS_POLL_SECONDS
from server.tools.tool_registry import ToolSpec
from test_e2e_list_changed import attach_second_browser
from test_e2e_stdio_path import LAUNCHER, LAUNCHER_AS_OURS, ROOT, live_server  # noqa: F401
from test_list_changed import NINE, Heard, names, wait_until
from test_relay_recovery_process import STUB, WRAPPER, kill_server
from test_server_process_lifecycle import SERVER_SCRIPT, sandbox  # noqa: F401

MODERN = "2026-07-28"
HANDSHAKE = HANDSHAKE_PROTOCOL_VERSIONS[-1]     # what a handshake client ends on: 2025-11-25


@pytest.fixture(autouse=True)
def era_unset(monkeypatch):
    """The server in these tests reads its era from the environment when it starts: a value
    exported in the developer's shell must not decide it."""
    monkeypatch.delenv(mcp_era.ERA_ENV, raising=False)


@pytest.fixture
def server_era(request, monkeypatch):
    """Sets the era of the in-process server. A test lists it BEFORE `live_server`, so the
    environment is in place when the server starts."""
    monkeypatch.setenv(mcp_era.ERA_ENV, request.param)
    return request.param


def text_of(result):
    return "".join(block.text for block in result.content if hasattr(block, "text"))


class Launched:
    """An SDK client connected to a launcher subprocess, and what that launcher said on stderr."""

    def __init__(self, client, stderr_path, spawned_at):
        self.client = client
        self.stderr_path = stderr_path
        self.spawned_at = spawned_at

    def stderr(self):
        return Path(self.stderr_path).read_text(encoding="utf-8", errors="replace")

    async def after_first_poll(self):
        """Wait until the relay's watcher has taken its baseline (one poll period in)."""
        left = self.spawned_at + TOOLS_POLL_SECONDS + 1.5 - time.monotonic()
        if left > 0:
            await asyncio.sleep(left)


@asynccontextmanager
async def connected(params, stderr_path, mode="auto", message_handler=None):
    spawned_at = time.monotonic()
    with open(stderr_path, "w", encoding="utf-8") as errlog:
        async with Client(stdio_client(params, errlog=errlog), mode=mode,
                          message_handler=message_handler) as client:
            yield Launched(client, stderr_path, spawned_at)


def fixture_server_params(live, tmp_path, name, relay_era="all"):
    """The launcher as a client starts it, pinned to the in-process fixture server (declared
    ours, as test_e2e_stdio_path does) and told which era to carry."""
    env = dict(os.environ)
    env.update({MCP_PORT_ENV: str(live.mcp_port), "BROWSERPLUG_TEST_OURS_PID": str(os.getpid()),
                mcp_era.ERA_ENV: relay_era, STATE_DIR_ENV: str(tmp_path / f"state-{name}"),
                "PYTHONIOENCODING": "utf-8"})
    return StdioServerParameters(command=sys.executable,
                                 args=[str(LAUNCHER_AS_OURS), str(LAUNCHER)], env=env,
                                 cwd=str(ROOT))


def launcher_connected(live, tmp_path, name, relay_era="all", **kwargs):
    return connected(fixture_server_params(live, tmp_path, name, relay_era),
                     tmp_path / f"{name}.stderr", **kwargs)


def assert_adopted_without_spawning(launched):
    text = launched.stderr()
    assert "ignoring a BrowserPlug-looking" not in text, text
    assert "spawning a detached instance" not in text, text


def register_slow_tool(server):
    """A tool that notes when it starts and when it is cancelled, to see a cancel reach it."""
    events = []

    async def slow(seconds: float = 30.0) -> str:
        """Sleeps, noting its start and whether it was cancelled.

        Args:
            seconds: How long to sleep.
        """
        events.append("slow started")
        try:
            await asyncio.sleep(seconds)
        except asyncio.CancelledError:
            events.append("slow cancelled")
            raise
        events.append("slow finished")
        return "slept"

    server.mcp_tools.registry.put(ToolSpec.from_function(slow))
    return events


# --- one launcher per era, side by side ---------------------------------------------------

async def test_a_modern_and_a_handshake_client_are_both_served_by_the_same_launcher_code(
        live_server, tmp_path):
    """initialize or discover, tools/list, tools/call, a cancel, and the change notice that
    reaches each client in the form its era allows, over the real launcher."""
    await live_server.attach_extension()
    events = register_slow_tool(live_server.server)
    heard = Heard()
    async with launcher_connected(live_server, tmp_path, "modern") as modern, \
            launcher_connected(live_server, tmp_path, "handshake", mode="legacy",
                               message_handler=heard) as legacy:
        # -- the opening exchange
        assert modern.client.protocol_version == MODERN
        assert legacy.client.protocol_version == HANDSHAKE
        assert modern.client.server_info.name == "BrowserPlug"

        # -- tools/list and tools/call
        for launched in (modern, legacy):
            assert names(await launched.client.list_tools()) == NINE + ["slow"]
            tabs = await launched.client.call_tool("tabs", {"action": "list"})
            assert "Fixture Page" in text_of(tabs), text_of(tabs)
        assert [r["action"] for r in live_server.requests] == ["tabs.list", "tabs.list"]
        # A non-ASCII tool name (an unknown one: the server answers it) travels as the
        # Mcp-Name sentinel, and the SDK server's header check lets it through.
        unknown = await modern.client.call_tool("café", {})
        assert unknown.is_error and "café" in text_of(unknown)

        # -- a cancel reaches the tool, and the connection serves on
        call = asyncio.create_task(modern.client.call_tool("slow", {"seconds": 30}))
        await wait_until(lambda: "slow started" in events, "the slow tool to start")
        call.cancel()
        with suppress(asyncio.CancelledError):
            await call
        await wait_until(lambda: "slow cancelled" in events, "the server to cancel the tool",
                         timeout=10)
        assert "slow finished" not in events
        assert names(await modern.client.list_tools()) == NINE + ["slow"]

        # -- a second browser changes the tool surface: each client hears it its own way
        await modern.after_first_poll()
        await legacy.after_first_poll()
        async with modern.client.listen(tools_list_changed=True) as subscription:
            second = await attach_second_browser(live_server)
            try:
                event = await asyncio.wait_for(subscription.__anext__(), 15)
                assert isinstance(event, ToolsListChanged)
                await wait_until(lambda: len(heard) == 1, "the handshake client's notice",
                                 timeout=10)
                assert "browsers" in names(await modern.client.list_tools())
                assert "browsers" in names(await legacy.client.list_tools())
            finally:
                await second.close()

    modern_log, legacy_log = modern.stderr(), legacy.stderr()
    assert_adopted_without_spawning(modern)
    assert_adopted_without_spawning(legacy)
    assert "MCP era all (env)" in modern_log
    assert "cancelled request" in modern_log, modern_log
    assert "told 1 listen stream" in modern_log, modern_log
    assert "sent tools/list_changed" in legacy_log, legacy_log
    assert "backend session was gone" not in modern_log, "a modern client has no session to rebuild"


# --- version skew -----------------------------------------------------------------------------

@pytest.mark.parametrize("server_era, relay_era", [
    ("handshake", "all"),       # the relay offers 2026-07-28, the server refuses: -32022, fall back
    ("all", "direct"),          # the relay sends nothing modern: the server's gate answers the probe
    ("all", "handshake"),
], indirect=["server_era"])
async def test_a_client_ends_on_the_handshake_era_when_either_side_does_not_carry_the_modern_one(
        server_era, live_server, tmp_path, relay_era):
    await live_server.attach_extension()
    async with launcher_connected(live_server, tmp_path, "skew", relay_era) as launched:
        assert launched.client.protocol_version == HANDSHAKE
        assert names(await launched.client.list_tools()) == NINE
        tabs = await launched.client.call_tool("tabs", {"action": "list"})
        assert "Fixture Page" in text_of(tabs)
    assert_adopted_without_spawning(launched)
    if (server_era, relay_era) == ("handshake", "all"):
        assert "does not serve 2026-07-28" in launched.stderr(), launched.stderr()


# --- a server restart ---------------------------------------------------------------------

async def test_a_modern_client_keeps_its_connection_and_its_stream_across_a_server_restart(
        sandbox, tmp_path):
    """The relay outlives the server. A modern request needs no session, so nothing is
    rebuilt; the listen stream is the relay's own, so it survives; and the new server_pid is
    announced on it."""
    sandbox.start(**{mcp_era.ERA_ENV: "all"})
    first_pid = sandbox.wait_healthy()["server_pid"]
    stub = tmp_path / "spawn_stub.py"
    stub.write_text(STUB.format(server=str(SERVER_SCRIPT), ws=str(sandbox.ws_port)))
    wrapper = tmp_path / "launcher_wrapper.py"
    wrapper.write_text(WRAPPER.format(root=str(ROOT), stub=str(stub)))
    env = sandbox.env()
    env.update({MCP_PORT_ENV: str(sandbox.mcp_port), mcp_era.ERA_ENV: "all"})
    params = StdioServerParameters(command=sys.executable, args=[str(wrapper)], env=env,
                                   cwd=str(ROOT))
    async with connected(params, tmp_path / "restart.stderr") as launched:
        client = launched.client
        assert client.protocol_version == MODERN
        async with client.listen(tools_list_changed=True) as subscription:
            await launched.after_first_poll()
            assert await asyncio.to_thread(kill_server, sandbox) == first_pid

            # The server is gone. This request finds it so, the relay respawns it, and the
            # request (it only reads) is sent again on the new one.
            listed = await asyncio.wait_for(client.list_tools(), 90)
            assert names(listed) == NINE
            second_pid = sandbox.wait_healthy(not_pid=first_pid)["server_pid"]
            assert second_pid != first_pid

            event = await asyncio.wait_for(subscription.__anext__(), 30)
            assert isinstance(event, ToolsListChanged)
            info = await client.call_tool("server_info", {})
            assert not info.is_error, text_of(info)
        assert client.protocol_version == MODERN
    log = launched.stderr()
    assert "backend session was gone" not in log, "there was no session to rebuild"
    assert "told 1 listen stream" in log, log
