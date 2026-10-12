"""The proxy transport end to end: real launcher, real proxy child, real server, SDK client.

    SDK stdio client -> launcher.py (BROWSERPLUG_LAUNCHER_RELAY=0) -> server/proxy child
      -> streamable HTTP -> server.py on the sandbox's ports

Nothing in that chain is a double. The server is a real `server/server.py` process
fenced off by test_server_process_lifecycle.Sandbox (scratch state dir and token, ports
outside 2830..2942 and 8772, BROWSERPLUG_SERVER_PYTHON = this interpreter, so the proxy
child and the /restart replacement run here too). The launcher runs through a small
wrapper for one reason: its spawn target is a stub that adds the sandbox's WebSocket port,
so even a launcher that had to spawn could never reach for the live server's :8772. The
wrapper is given the real launcher's path as an argument, which is how the server's
process walk (client_identity) recognises it as the launcher hop above the proxy.

The session: initialize, tools/list, a tools/call, a call to a renamed tool, then a
signed /restart of the server under the open session, the tools/list_changed the proxy
pushes for it, and calls on the replacement, with no reconnect.

The same chain with a client that asks for 2026-07-28 (mode="auto") and the era setting in
the proxy child's environment: under `all` the client ends on 2026-07-28 and hears the
restart on its `subscriptions/listen` stream, under `handshake` the proxy refuses the
probe and the client ends on 2025-11-25 with the bare notice. test_proxy_front.py covers the
front on a pipe pair with a faked backend; only this runs the launcher, the child and a
real server together.

Below it, the proxy process on its own (server/proxy/__main__.py, no server): it exits 0
when its stdin closes, with or without a session behind it. An SDK client hides a proxy
that never exits, because it kills what is left two seconds after closing stdin; the
launcher, which waits on its child, would hang.
"""

import asyncio
import json
import socket
import subprocess
import sys
import threading
import time
import urllib.request

import mcp_types
import pytest
from mcp import Client, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.subscriptions import ListenNotSupportedError
from mcp.shared.subscriptions import SUBSCRIPTION_ID_META_KEY, ToolsListChanged

import test_imports  # noqa: F401
from browserplug_shared import mcp_era
from browserplug_shared.mcp_port import MCP_PORT_ENV
from browserplug_shared.version import __version__ as BROWSERPLUG_VERSION
from launcher.stdio_relay import TOOLS_POLL_SECONDS
from server.tools.tool_renames import renamed_tool_message

from test_server_process_lifecycle import ROOT, SERVER_SCRIPT, Sandbox, sandbox  # noqa: F401

LAUNCHER = ROOT / "launcher" / "launcher.py"

STUB = """\
import subprocess, sys
sys.exit(subprocess.call([sys.executable, {server!r}, *sys.argv[1:], "--port", {ws!r}]))
"""

# argv[1] is the real launcher's path: unused here, read by the server's process walk.
WRAPPER = """\
import json, os, sys
sys.path.insert(0, {root!r})
import launcher.launcher as launcher
from launcher.client_pid import client_pid
launcher.SERVER = {stub!r}
with open({ids!r}, "w") as f:
    json.dump({{"launcher_pid": os.getpid(), "client_pid": client_pid()}}, f)
sys.exit(launcher.main())
"""

TOOLS = {"tabs", "navigate", "page_read", "page_snapshot", "page_act", "page_eval",
         "page_screenshot", "requests", "server_info"}


def _text(result):
    return "".join(getattr(c, "text", "") for c in result.content)


def _clients(box):
    """The mcp_client rows of the sandbox server's full /health report."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f"http://127.0.0.1:{box.mcp_port}/health", timeout=10) as r:
        report = json.loads(r.read())
    found = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("kind") == "mcp_client":
                found.append(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(report)
    return found


async def _until(predicate, what, timeout=30.0):
    deadline = time.monotonic() + timeout
    while True:
        value = await asyncio.to_thread(predicate)
        if value:
            return value
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        await asyncio.sleep(0.25)


def _proxy_launcher(sandbox, tmp_path, **env_extra):
    """StdioServerParameters for the real launcher with the relay off (so the proxy child
    serves the client), pinned to the sandbox server, and the file its pids land in."""
    stub = tmp_path / "spawn_stub.py"
    stub.write_text(STUB.format(server=str(SERVER_SCRIPT), ws=str(sandbox.ws_port)))
    ids = tmp_path / "launcher_ids.json"
    wrapper = tmp_path / "launcher_wrapper.py"
    wrapper.write_text(WRAPPER.format(root=str(ROOT), stub=str(stub), ids=str(ids)))
    env = sandbox.env()
    env[MCP_PORT_ENV] = str(sandbox.mcp_port)
    env["BROWSERPLUG_LAUNCHER_RELAY"] = "0"
    env.update(env_extra)
    params = StdioServerParameters(command=sys.executable, args=[str(wrapper), str(LAUNCHER)],
                                   env=env, cwd=str(tmp_path))
    return params, ids


@pytest.mark.slow
async def test_a_proxy_session_survives_a_server_restart(sandbox, tmp_path):  # noqa: F811
    await asyncio.to_thread(sandbox.start)
    first = await asyncio.to_thread(sandbox.wait_healthy)
    params, ids = _proxy_launcher(sandbox, tmp_path)

    pushed = []

    async def on_message(message):
        if isinstance(message, mcp_types.ToolListChangedNotification):
            pushed.append(message)

    errlog = open(tmp_path / "launcher-stderr.txt", "w+", encoding="utf-8")
    try:
        async with asyncio.timeout(240):
            async with Client(stdio_client(params, errlog=errlog), mode="legacy",
                              message_handler=on_message) as client:
                # initialize: answered by the proxy, gated on the backend, with its text.
                assert client.server_info.name == "BrowserPlug"
                assert client.server_info.version == BROWSERPLUG_VERSION
                assert client.server_capabilities.tools.list_changed is True
                assert "BrowserPlug" in (client.instructions or ""), "the backend's"

                listed = {t.name for t in (await client.list_tools()).tools}
                assert TOOLS <= listed, sorted(listed)

                info = await client.call_tool("server_info", {})
                assert info.is_error is False, _text(info)
                assert str(first["server_pid"]) in _text(info), "the sandbox server answered"

                stale = await client.call_tool("page_navigate", {"url": "https://example.org"})
                assert stale.is_error is True
                assert _text(stale) == renamed_tool_message("page_navigate")

                # The process walk names the launcher (not the proxy child that holds the
                # socket) and ties it to the build the proxy's /health poll reported.
                launched = json.loads(ids.read_text())
                rows = await _until(
                    lambda: [r for r in _clients(sandbox)
                             if r.get("launcher_pid") == launched["launcher_pid"]
                             and r.get("launcher_version")],
                    "the launcher hop on the connection map")
                assert rows[0]["launcher_version"] == BROWSERPLUG_VERSION
                assert rows[0]["pid"] == launched["client_pid"]

                # A signed /restart under the open session: the replacement comes up on the
                # same port, the proxy pushes tools/list_changed, and the next calls work.
                status, body = await asyncio.to_thread(
                    sandbox.post, "/restart", b"", sandbox.signed("POST", "/restart"))
                assert status == 200, body
                second = await asyncio.to_thread(sandbox.wait_healthy, 60.0,
                                                 first["server_pid"])
                assert second["server_pid"] != first["server_pid"]
                deadline = time.monotonic() + 30
                while not pushed:
                    assert time.monotonic() < deadline, "no tools/list_changed after the restart"
                    await asyncio.sleep(0.2)

                relisted = {t.name for t in (await client.list_tools()).tools}
                assert TOOLS <= relisted
                again = await client.call_tool("server_info", {})
                assert again.is_error is False, _text(again)
                assert str(second["server_pid"]) in _text(again), "the replacement answered"
    finally:
        errlog.seek(0)
        stderr = errlog.read()
        errlog.close()
    assert "proxying stdio" in stderr, stderr
    assert "spawning a detached instance" not in stderr, stderr
    assert "tool surface changed" in stderr, stderr


@pytest.mark.slow
@pytest.mark.parametrize("era, version", [(mcp_era.ALL, "2026-07-28"),
                                          (mcp_era.HANDSHAKE, "2025-11-25")])
async def test_the_proxy_serves_a_2026_07_28_client_only_where_the_era_setting_allows(
        sandbox, tmp_path, era, version):  # noqa: F811
    await asyncio.to_thread(sandbox.start)
    first = await asyncio.to_thread(sandbox.wait_healthy)
    params, _ = _proxy_launcher(sandbox, tmp_path, **{mcp_era.ERA_ENV: era})

    bare = []

    async def on_message(message):
        # The SDK also tees a notification stamped with a subscription id (the copy delivered
        # on the listen stream) to the message handler, on its own task: only an unstamped
        # one is the bare broadcast this test is about.
        if isinstance(message, mcp_types.ToolListChangedNotification):
            meta = (message.params.meta if message.params is not None else None) or {}
            if SUBSCRIPTION_ID_META_KEY not in meta:
                bare.append(message)

    async def restart():
        status, body = await asyncio.to_thread(
            sandbox.post, "/restart", b"", sandbox.signed("POST", "/restart"))
        assert status == 200, body
        second = await asyncio.to_thread(sandbox.wait_healthy, 60.0, first["server_pid"])
        assert second["server_pid"] != first["server_pid"]
        return second

    errlog = open(tmp_path / "launcher-stderr.txt", "w+", encoding="utf-8")
    try:
        async with asyncio.timeout(240):
            async with Client(stdio_client(params, errlog=errlog), mode="auto",
                              message_handler=on_message) as client:
                assert client.protocol_version == version
                assert client.server_info.name == "BrowserPlug"
                assert TOOLS <= {t.name for t in (await client.list_tools()).tools}
                info = await client.call_tool("server_info", {})
                assert info.is_error is False, _text(info)
                assert str(first["server_pid"]) in _text(info)

                # The proxy takes its first /health reading as a baseline: a restart
                # before it would be a change from nothing, and announce nothing.
                await asyncio.sleep(TOOLS_POLL_SECONDS + 1.5)
                if era == mcp_era.ALL:
                    async with client.listen(tools_list_changed=True) as subscription:
                        second = await restart()
                        event = await asyncio.wait_for(subscription.__anext__(), 45)
                        assert isinstance(event, ToolsListChanged)
                    assert bare == [], "a 2026-07-28 client is told on its stream only"
                else:
                    with pytest.raises(ListenNotSupportedError):
                        async with client.listen(tools_list_changed=True):
                            pass
                    second = await restart()
                    deadline = time.monotonic() + 45
                    while not bare:
                        assert time.monotonic() < deadline, "no bare tools/list_changed"
                        await asyncio.sleep(0.2)

                assert TOOLS <= {t.name for t in (await client.list_tools()).tools}
                again = await client.call_tool("server_info", {})
                assert again.is_error is False, _text(again)
                assert str(second["server_pid"]) in _text(again), "the replacement answered"
                assert client.protocol_version == version
    finally:
        errlog.seek(0)
        stderr = errlog.read()
        errlog.close()
    assert "proxying stdio" in stderr, stderr
    assert "spawning a detached instance" not in stderr, stderr


PROXY_MAIN = ROOT / "server" / "proxy" / "__main__.py"

INITIALIZE = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                         "clientInfo": {"name": "t", "version": "0"}}}


def _dead_backend_url():
    with socket.socket() as probe:                      # a port with nothing behind it
        probe.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{probe.getsockname()[1]}/mcp"


def _proxy_process(url):
    return subprocess.Popen(
        [sys.executable, str(PROXY_MAIN), "--backend-url", url, "--client-pid", "1",
         "--launcher-pid", "1", "--launcher-version", "x"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def _exit_code(proc, seconds=15):
    try:
        return proc.wait(timeout=seconds)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        pytest.fail(f"the proxy was still running {seconds} s after its stdin closed")


def test_the_proxy_exits_when_stdin_closes_before_any_session():
    with _proxy_process(_dead_backend_url()) as proc:
        proc.stdin.close()
        assert _exit_code(proc) == 0, proc.stderr.read().decode(errors="replace")


def test_the_proxy_answers_then_exits_when_stdin_closes_after_a_session():
    """The initialize fails at the gate (no backend), the client reads that, then closes
    stdin: the proxy, its /health watcher included, must end."""
    url = _dead_backend_url()
    with _proxy_process(url) as proc:
        lines = []
        reader = threading.Thread(target=lambda: lines.append(proc.stdout.readline()),
                                  daemon=True)
        reader.start()
        proc.stdin.write(json.dumps(INITIALIZE).encode() + b"\n")
        proc.stdin.flush()
        reader.join(15)
        assert lines and lines[0], "no answer to the initialize"
        reply = json.loads(lines[0])
        assert reply["id"] == 1 and reply["error"]["code"] == -32603
        assert url in reply["error"]["message"]
        proc.stdin.close()
        assert _exit_code(proc) == 0, proc.stderr.read().decode(errors="replace")
