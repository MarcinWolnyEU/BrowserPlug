"""server/tools/list_changed.py with the real SDK and no browser: who hears a tool-surface
change, when, and what holds the stop.

The tool surface is reshaped the way production reshapes it, through
BrowserPlugTools.refresh_browsers_tool() with the roster faked, and always from a thread
that is not the one serving the client (production changes arrive on the WebSocket loop).
A Streamable HTTP push only reaches a session holding its GET stream, and a GET stream
never completes under httpx2.ASGITransport, so the HTTP cases run uvicorn on a port of
their own; GetStreams says when the client's GET stream is open, so no push is raced.
"""

import asyncio
import json
import logging
import threading
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx2
import mcp_types
import pytest
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.server.subscriptions import ToolsListChanged
from mcp.shared.exceptions import MCPError
from mcp.shared.memory import create_client_server_memory_streams
from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS

import test_imports  # noqa: F401  (path setup)
from port_coordinator import allocate_port
from server.runtime.lifecycle import LifecycleHooks, ServerLifecycle
from server.server import BrowserPlugServer
from server.tools import list_changed
from server.tools import list_changed_policy
from server.tools import mcp_server as mcp_server_mod
from server.tools import mcp_tools as mcp_tools_mod

logger = logging.getLogger(__name__)

MCP_HEADERS = {"content-type": "application/json",
               "accept": "application/json, text/event-stream"}
NINE = ["tabs", "navigate", "page_read", "page_snapshot", "page_act", "page_eval",
        "page_screenshot", "requests", "server_info"]


# =============================================================================
# Helpers (test_e2e_list_changed.py imports them)
# =============================================================================

async def wait_until(predicate, what, timeout=10.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        await asyncio.sleep(0.02)


class Heard:
    """A client message_handler that keeps every notifications/tools/list_changed."""

    def __init__(self):
        self.at = []

    async def __call__(self, message):
        if isinstance(message, mcp_types.ToolListChangedNotification):
            self.at.append(time.monotonic())

    def __len__(self):
        return len(self.at)


class GetStreams:
    """ASGI wrapper counting the GET /mcp streams that are open (answered 200, not yet
    finished): a push only reaches a session once its GET stream is open."""

    def __init__(self, app):
        self.app = app
        self.open = 0

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") != "GET":
            await self.app(scope, receive, send)
            return
        started = False

        async def watch(message):
            nonlocal started
            if message["type"] == "http.response.start" and message["status"] == 200:
                started = True
                self.open += 1
            await send(message)

        try:
            await self.app(scope, receive, watch)
        finally:
            if started:
                self.open -= 1


@asynccontextmanager
async def serving(app):
    """uvicorn serving `app` on its own thread and loop, as lifecycle.start_mcp_server
    runs it. The listen streams are closed before the stop, as stop_mcp_server does.

    The join is not asserted here: when a client resets a connection, asyncio on Windows
    can leave uvicorn's Server.wait_closed() waiting forever (see the stop test below), and
    the thread is a daemon, so the test moves on after a short wait. A thread still alive
    then is logged and left in `live.wedged`, so a pattern of them stays visible. The stop
    test, which needs no client reset, asserts the join."""
    port = allocate_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                           log_level="error"))
    thread = threading.Thread(target=server.run, name="list-changed-uvicorn", daemon=True)
    thread.start()
    await wait_until(lambda: server.started or not thread.is_alive(), "uvicorn listening")
    assert server.started, "uvicorn did not start"
    live = SimpleNamespace(url=f"http://127.0.0.1:{port}/mcp", thread=thread,
                           uvicorn=server, wedged=False)
    try:
        yield live
    finally:
        list_changed.close_streams(thread)
        server.should_exit = True
        await asyncio.to_thread(thread.join, 3)
        live.wedged = thread.is_alive()
        if live.wedged:
            logger.warning("uvicorn thread %s did not exit within 3 s of should_exit",
                           thread.name)


def http_client():
    return httpx2.AsyncClient(trust_env=False, timeout=httpx2.Timeout(10, read=300))


def names(listed):
    return [tool.name for tool in listed.tools]


class Roster:
    """Stands in for browsers.tool_description: None hides `browsers`, text shows it."""

    def __init__(self, monkeypatch):
        self.text = None
        monkeypatch.setattr(mcp_tools_mod.browsers_tool, "tool_description",
                            lambda _calls: self.text)


def reshape(tools, roster, *texts):
    """Set the roster to each of `texts` in turn and refresh, like a browser connecting
    or leaving. Returns the thread it ran on."""
    for text in texts:
        roster.text = text
        tools.refresh_browsers_tool()
    return threading.current_thread()


def _server():
    return BrowserPlugServer(host="localhost", port=allocate_port(), start_mcp=False)


def _frames(response):
    """The JSON-RPC messages in an SSE or JSON reply, parsed the way the stdio relay does."""
    from launcher.stdio_relay import messages_from
    return [json.loads(m) for m in messages_from(
        {"Content-Type": response.headers.get("content-type", "")}, response.text)]


def _initialize(msg_id=1):
    return {"jsonrpc": "2.0", "id": msg_id, "method": "initialize",
            "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                       "clientInfo": {"name": "raw", "version": "1"}}}


async def raw_session(http, url):
    """initialize + notifications/initialized over plain POSTs, as the stdio relay does:
    a session that never opens a GET stream. Returns its headers."""
    reply = await http.post(url, headers=MCP_HEADERS, json=_initialize())
    assert reply.status_code == 200, reply.text
    headers = dict(MCP_HEADERS, **{"mcp-session-id": reply.headers["mcp-session-id"],
                                   "mcp-protocol-version": "2025-11-25"})
    done = await http.post(url, headers=headers,
                           json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert done.status_code == 202, done.text
    return headers


@pytest.fixture
def fast_debounce(monkeypatch):
    """A quiet period and cap short enough for the broadcast cases, which wait on state
    rather than on a clock."""
    monkeypatch.setattr(list_changed_policy, "QUIET_SECONDS", 0.05)
    monkeypatch.setattr(list_changed_policy, "MAX_WAIT_SECONDS", 0.2)


@pytest.fixture
def small_debounce(monkeypatch):
    """A quiet period and cap short enough for the timing cases."""
    monkeypatch.setattr(list_changed_policy, "QUIET_SECONDS", 0.1)
    monkeypatch.setattr(list_changed_policy, "MAX_WAIT_SECONDS", 0.5)


# =============================================================================
# HTTP: the production app, uvicorn on its own thread
# =============================================================================

async def test_a_change_on_another_thread_reaches_an_http_client_once(monkeypatch):
    """The glue: BrowserPlugTools' hook, the notifier bound in the app lifespan on
    uvicorn's loop, the middleware's record, the push on the GET stream, all behind the
    production ASGI wrapper. One change, one notification, and the re-list shows it."""
    server = _server()
    tools, roster = server.mcp_tools, Roster(monkeypatch)
    app = GetStreams(server._wrap_client_tracking(tools.mcp.http_app()))
    heard = Heard()
    async with serving(app) as live, http_client() as http:
        async with Client(streamable_http_client(live.url, http_client=http), mode="legacy",
                          message_handler=heard) as client:
            assert names(await client.list_tools()) == NINE
            await wait_until(lambda: app.open == 1, "the client's GET stream")
            ran_on = await asyncio.to_thread(reshape, tools, roster, "two browsers")
            assert ran_on is not live.thread and ran_on is not threading.main_thread()
            await wait_until(lambda: len(heard) >= 1, "tools/list_changed", timeout=3)
            await asyncio.sleep(1.0)
            assert len(heard) == 1, "one change, one notification"
            assert names(await client.list_tools()) == NINE + ["browsers"]
    assert tools.mcp.list_changed.session_count() == 0, "the DELETE on close forgot it"


async def test_a_session_without_a_get_stream_hears_nothing_extra(monkeypatch):
    """The stdio relay's shape: POSTs only. While a GET-holding client is pushed to, every
    frame on the relay-shaped session's POSTs is the reply it asked for, before, during and
    after the push, so nothing duplicates the relay's own /health synthesis."""
    server = _server()
    tools, roster = server.mcp_tools, Roster(monkeypatch)
    app = GetStreams(server._wrap_client_tracking(tools.mcp.http_app()))
    heard, frames = Heard(), []
    async with serving(app) as live, http_client() as http:
        async with Client(streamable_http_client(live.url, http_client=http), mode="legacy",
                          message_handler=heard):
            await wait_until(lambda: app.open == 1, "the GET-holding client's stream")
            relay = await raw_session(http, live.url)
            await wait_until(lambda: tools.mcp.list_changed.session_count() == 2,
                             "both sessions on the book")

            async def tools_list(msg_id):
                reply = await http.post(live.url, headers=relay, json={
                    "jsonrpc": "2.0", "id": msg_id, "method": "tools/list"})
                assert reply.status_code == 200, reply.text
                frames.extend(_frames(reply))

            await tools_list(1)
            change = asyncio.create_task(asyncio.to_thread(reshape, tools, roster, "two"))
            msg_id = 2
            while len(heard) < 1:
                await tools_list(msg_id)
                msg_id += 1
                assert msg_id < 500, "no push reached the GET-holding client"
            await change
            for _ in range(10):
                await tools_list(msg_id)
                msg_id += 1
                await asyncio.sleep(0.05)
    assert len(heard) == 1
    assert [f.get("method") for f in frames if "method" in f] == [], "only replies"
    assert [f["id"] for f in frames] == list(range(1, msg_id))


async def test_a_delete_forgets_the_session_and_a_refused_one_does_not():
    """SessionEndLayer: a 2xx DELETE ends the session on the book, and so does a 404 (the
    SDK's answer for a session it lost); a DELETE HostOriginLayer refuses never reaches
    the layer."""
    mcp = BrowserPlugServer(host="localhost", port=allocate_port(),
                            start_mcp=False).mcp_app
    app = mcp.http_app()
    url = "http://127.0.0.1/mcp"
    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),
                                      base_url="http://127.0.0.1") as http:
            first = await raw_session(http, url)
            second = await raw_session(http, url)
            # The 202 for notifications/initialized precedes its handling.
            await wait_until(lambda: mcp.list_changed.session_count() == 2, "both recorded")
            refused = await http.delete(url, headers=dict(first, origin="http://evil.example"))
            assert (refused.status_code, refused.text) == (403, "Forbidden Origin")
            assert mcp.list_changed.session_count() == 2
            # A session the SDK no longer has (its transport died) answers 404, which ends
            # it on the book; the live sessions stay.
            lost = "f" * 32
            mcp.list_changed.book.note(lost, object(), asyncio.get_running_loop())
            unknown = await http.post(url, json={"jsonrpc": "2.0", "id": 9, "method": "ping"},
                                      headers=dict(first, **{"mcp-session-id": lost}))
            assert unknown.status_code == 404
            assert sorted(mcp.list_changed.book.keys()) == sorted(
                [first["mcp-session-id"], second["mcp-session-id"]])
            deleted = await http.delete(url, headers=first)
            assert deleted.status_code == 200, deleted.text
            assert sorted(mcp.list_changed.book.keys()) == [second["mcp-session-id"]]
            # The deleted id now answers 404, which forgets nothing more.
            gone = await http.post(url, json={"jsonrpc": "2.0", "id": 10, "method": "ping"},
                                   headers=first)
            assert gone.status_code == 404
            assert mcp.list_changed.session_count() == 1


async def test_a_handshake_session_cannot_listen():
    """subscriptions/listen exists only at 2026-07-28: a handshake session gets -32601,
    and its initialize capabilities are what clients have always been sent."""
    mcp = BrowserPlugServer(host="localhost", port=allocate_port(),
                            start_mcp=False).mcp_app
    app = mcp.http_app()
    url = "http://127.0.0.1/mcp"
    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),
                                      base_url="http://127.0.0.1") as http:
            init = await http.post(url, headers=MCP_HEADERS, json=_initialize())
            headers = await raw_session(http, url)
            reply = await http.post(url, headers=headers, json={
                "jsonrpc": "2.0", "id": 3, "method": "subscriptions/listen",
                "params": {"notifications": {"toolsListChanged": True}}})
    (result,) = _frames(init)
    assert result["result"]["capabilities"]["tools"] == {"listChanged": True}
    (error,) = _frames(reply)
    assert error["error"]["code"] == mcp_types.METHOD_NOT_FOUND, error


async def test_every_live_binding_serves_its_own_loop(monkeypatch):
    """Two uvicorn threads serving the same server (a restart's replacement starting
    while the old one has not exited): each client hears one push, from its own loop.
    After the first stops, the second still pushes."""
    server = _server()
    tools, roster = server.mcp_tools, Roster(monkeypatch)
    first = GetStreams(server._wrap_client_tracking(tools.mcp.http_app()))
    second = GetStreams(server._wrap_client_tracking(tools.mcp.http_app()))
    heard_first, heard_second = Heard(), Heard()
    async with serving(second) as live_second, http_client() as http_second:
        async with Client(streamable_http_client(live_second.url, http_client=http_second),
                          mode="legacy", message_handler=heard_second):
            await wait_until(lambda: second.open == 1, "the second GET stream")
            async with serving(first) as live_first, http_client() as http_first:
                async with Client(streamable_http_client(live_first.url,
                                                         http_client=http_first),
                                  mode="legacy", message_handler=heard_first):
                    await wait_until(lambda: first.open == 1, "the first GET stream")
                    await asyncio.to_thread(reshape, tools, roster, "two")
                    await wait_until(lambda: len(heard_first) and len(heard_second),
                                     "both clients told", timeout=3)
                    await asyncio.sleep(0.6)
                    assert (len(heard_first), len(heard_second)) == (1, 1)
            # The first uvicorn is gone; the second binding still serves.
            await asyncio.to_thread(reshape, tools, roster, None)
            await wait_until(lambda: len(heard_second) == 2, "the second told again",
                             timeout=3)


# =============================================================================
# Debounce, in-process on the test's loop
# =============================================================================

async def test_a_burst_and_a_flip_back_each_give_one_and_a_no_op_gives_none(
        monkeypatch, small_debounce):
    tools, roster = _server().mcp_tools, Roster(monkeypatch)
    heard = Heard()
    async with tools.mcp.list_changed.running():
        async with Client(tools.mcp, mode="legacy", message_handler=heard):
            await asyncio.to_thread(reshape, tools, roster, *[f"r{i}" for i in range(6)])
            await asyncio.sleep(0.6)
            assert len(heard) == 1, "six changes inside the quiet period are one"

            before = tools.tools_signature
            # The flipping thread is joined from the loop's own thread, so both of its
            # surface_changed() calls are scheduled before either callback can run. Only a
            # signature read on the calling thread still sees the two states apart.
            flip = threading.Thread(target=reshape, args=(tools, roster, None, "r5"))
            flip.start()
            flip.join()
            assert tools.tools_signature == before
            await asyncio.sleep(0.6)
            assert len(heard) == 2, ("a change undone inside the window is announced: a "
                                     "client may have listed the surface in between")

            await asyncio.to_thread(tools._recompute_tools_signature)
            await asyncio.to_thread(reshape, tools, roster, "r5")
            await asyncio.sleep(0.6)
            assert len(heard) == 2, "a recompute that moved nothing pushes nothing"


async def test_steady_flapping_is_announced_within_the_cap(monkeypatch, small_debounce):
    """A change every 50 ms never leaves a quiet period of 100 ms. The 500 ms cap still
    announces while the flapping goes on."""
    tools, roster = _server().mcp_tools, Roster(monkeypatch)
    heard = Heard()

    def flap(until):
        i = 0
        while time.monotonic() < until:
            reshape(tools, roster, f"flap {i}")
            i += 1
            time.sleep(0.05)

    async with tools.mcp.list_changed.running():
        async with Client(tools.mcp, mode="legacy", message_handler=heard):
            started = time.monotonic()
            await asyncio.to_thread(flap, started + 1.5)
            stopped = time.monotonic()
            await asyncio.sleep(0.6)
    during = [t for t in heard.at if t < stopped]
    assert during, "the cap must announce before the flapping stops"
    assert during[0] - started < 1.0
    assert len(heard) <= 5


async def test_nothing_is_pushed_while_no_transport_is_bound(monkeypatch):
    tools, roster = _server().mcp_tools, Roster(monkeypatch)
    heard = Heard()
    async with Client(tools.mcp, mode="legacy", message_handler=heard):
        await asyncio.to_thread(reshape, tools, roster, "two")
        await asyncio.sleep(0.6)
        assert tools.mcp.list_changed.session_count() == 1, "recorded all the same"
    assert len(heard) == 0
    assert tools.mcp.list_changed.session_count() == 0, "the connection token is forgotten"


async def test_the_hook_and_the_middleware_never_raise():
    notifier = list_changed.ListChangedNotifier(lambda: 1 / 0)
    async with notifier.running():
        notifier.surface_changed()

    class BrokenSession:
        @property
        def client_params(self):
            raise RuntimeError("broken")

    ctx = SimpleNamespace(method="tools/list", protocol_version=HANDSHAKE_PROTOCOL_VERSIONS[-1],
                          session=BrokenSession(), request=None, lifespan_context={})

    async def call_next(c):
        return "answered"

    assert await notifier.middleware(ctx, call_next) == "answered"


# =============================================================================
# The broadcast engine, with fake sessions and no server
# =============================================================================

def _note(notifier, **sessions):
    loop = asyncio.get_running_loop()
    for key, session in sessions.items():
        notifier.book.note(key, session, loop)


async def test_a_change_during_a_broadcast_is_announced_after_it(fast_debounce):
    """One broadcast at a time: a deadline that passes while a push is in flight makes the
    broadcast run again, and the second push sees the newest surface."""
    surface, pushes, release = {"v": "a"}, [], asyncio.Event()

    class Slow:
        async def send_tool_list_changed(self):
            pushes.append(surface["v"])
            await release.wait()

    notifier = list_changed.ListChangedNotifier(lambda: surface["v"])
    async with notifier.running() as binding:
        _note(notifier, slow=Slow())
        surface["v"] = "b"
        notifier.surface_changed()
        await wait_until(lambda: pushes == ["b"], "the first push in flight", timeout=3)
        surface["v"] = "c"
        notifier.surface_changed()
        await wait_until(lambda: binding._again, "the second deadline during the push",
                         timeout=3)
        assert pushes == ["b"], "no second broadcast runs beside the first"
        release.set()
        await wait_until(lambda: pushes == ["b", "c"], "the second push", timeout=3)
        await wait_until(lambda: not binding._broadcasting, "the broadcast to end", timeout=3)
        await asyncio.sleep(0.3)
    assert pushes == ["b", "c"]


async def test_a_session_that_stopped_reading_neither_holds_back_the_others_nor_the_next_change(
        monkeypatch, fast_debounce):
    monkeypatch.setattr(list_changed, "SEND_TIMEOUT_SECONDS", 0.3)
    surface, got = {"v": "a"}, []

    class Stuck:
        async def send_tool_list_changed(self):
            await asyncio.Event().wait()

    class Good:
        async def send_tool_list_changed(self):
            got.append(surface["v"])

    notifier = list_changed.ListChangedNotifier(lambda: surface["v"])
    async with notifier.running() as binding:
        _note(notifier, stuck=Stuck(), good=Good())
        for value in ("b", "c"):
            surface["v"] = value
            notifier.surface_changed()
            await wait_until(lambda: got and got[-1] == value, f"the push of {value}",
                             timeout=3)
            # The stuck session times out (0.3 s), which ends the broadcast; only then can
            # the next change be announced.
            await wait_until(lambda: not binding._broadcasting,
                             "the stuck push to time out", timeout=3)
    assert got == ["b", "c"]


async def test_a_session_whose_push_raises_is_skipped_and_the_notifier_stays_up(fast_debounce):
    """A client killed without a DELETE stays on the book, and a push to it raises."""
    surface, got = {"v": "a"}, []

    class Dead:
        async def send_tool_list_changed(self):
            raise RuntimeError("closed")

    class Good:
        async def send_tool_list_changed(self):
            got.append(surface["v"])

    notifier = list_changed.ListChangedNotifier(lambda: surface["v"])
    async with notifier.running() as binding:
        published, publish = [], binding.bus.publish

        async def counting_publish(event):
            published.append(event)
            await publish(event)

        binding.bus.publish = counting_publish
        _note(notifier, dead=Dead(), good=Good())     # the dead one is pushed first
        for value in ("b", "c"):
            surface["v"] = value
            notifier.surface_changed()
            await wait_until(lambda: got and got[-1] == value, f"the push of {value}",
                             timeout=3)
            await wait_until(lambda: not binding._broadcasting, "the broadcast to end",
                             timeout=3)
    assert got == ["b", "c"]
    assert len(published) == 2, "the listen streams are told even when a push failed"


class Bomb:
    """A session whose push escapes `_push`: the stand-in for a bug in a push, in the bus
    or in a later SDK."""

    async def send_tool_list_changed(self):
        raise AssertionError("never reached: the patched _push raises first")


@pytest.fixture
def bomb(monkeypatch):
    real = list_changed._push

    async def push(session):
        if isinstance(session, Bomb):
            raise RuntimeError("injected push failure")
        await real(session)

    monkeypatch.setattr(list_changed, "_push", push)


async def test_a_broadcast_that_raises_is_contained_and_the_next_one_runs(
        bomb, fast_debounce, caplog):
    """An exception out of a broadcast would cancel the transport's task group. The block
    must exit cleanly, and the next change must still be announced."""
    surface, got = {"v": "a"}, []

    class Good:
        async def send_tool_list_changed(self):
            got.append(surface["v"])

    notifier = list_changed.ListChangedNotifier(lambda: surface["v"])
    with caplog.at_level(logging.WARNING, logger=list_changed.__name__):
        async with notifier.running() as binding:
            _note(notifier, bomb=Bomb(), good=Good())
            surface["v"] = "b"
            notifier.surface_changed()
            await wait_until(lambda: "broadcast failed" in caplog.text, "the failure logged",
                             timeout=3)
            await wait_until(lambda: not binding._broadcasting, "the broadcast to end",
                             timeout=3)
            notifier.book.forget("bomb")
            surface["v"] = "c"
            notifier.surface_changed()
            await wait_until(lambda: got and got[-1] == "c", "the next change announced",
                             timeout=3)


async def test_a_failing_broadcast_leaves_the_http_side_serving(
        bomb, fast_debounce, caplog, monkeypatch):
    """The consequence the containment prevents: the broadcast shares the Starlette
    lifespan's task group, so an escaping exception unwinds the SDK session manager and
    every later /mcp request answers 500, legacy and modern alike, while /health stays up."""
    server = _server()
    tools, roster = server.mcp_tools, Roster(monkeypatch)
    app = GetStreams(server._wrap_client_tracking(tools.mcp.http_app()))
    heard = Heard()
    with caplog.at_level(logging.WARNING, logger=list_changed.__name__):
        async with serving(app) as live, http_client() as http:
            async with Client(streamable_http_client(live.url, http_client=http),
                              mode="legacy", message_handler=heard) as client:
                assert names(await client.list_tools()) == NINE
                await wait_until(lambda: app.open == 1, "the client's GET stream")
                binding = list_changed._live[-1]
                assert binding.thread is live.thread
                tools.mcp.list_changed.book.note("bomb", Bomb(), binding.loop)
                await asyncio.to_thread(reshape, tools, roster, "two browsers")
                await wait_until(lambda: "broadcast failed" in caplog.text,
                                 "the failure logged", timeout=3)
                for mode in ("auto", "legacy"):
                    async with http_client() as fresh:
                        async with Client(streamable_http_client(live.url, http_client=fresh),
                                          mode=mode) as other:
                            assert names(await other.list_tools()) == NINE + ["browsers"]
                # The notifier works after the failure, too.
                tools.mcp.list_changed.forget("bomb")
                before = len(heard)
                await asyncio.to_thread(reshape, tools, roster, None)
                await wait_until(lambda: len(heard) > before, "the next change heard",
                                 timeout=3)


# =============================================================================
# 2026-07-28: subscriptions/listen
# =============================================================================

async def test_a_modern_client_hears_changes_on_listen(monkeypatch):
    tools, roster = _server().mcp_tools, Roster(monkeypatch)
    events = []
    async with tools.mcp.list_changed.running():
        async with Client(tools.mcp) as client:
            assert client.protocol_version == "2026-07-28"
            assert client.server_capabilities.tools.list_changed is True
            async with client.listen(tools_list_changed=True) as subscription:
                async def consume():
                    async for event in subscription:
                        events.append(event)

                reader = asyncio.create_task(consume())
                await asyncio.to_thread(reshape, tools, roster, "two")
                await wait_until(lambda: events, "a ToolsListChanged event", timeout=3)
                await asyncio.sleep(0.5)
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
    assert events == [ToolsListChanged()]


async def test_close_streams_ends_an_open_listen_stream_from_any_thread():
    """The stream finishes with its final result (the client's loop ends), not with an
    error, so the response completes and uvicorn is not held open by it."""
    tools = _server().mcp_tools
    notifier, ended = tools.mcp.list_changed, []
    async with notifier.running():
        async with Client(tools.mcp) as client:
            async with client.listen(tools_list_changed=True) as subscription:
                async def consume():
                    async for _event in subscription:
                        pass
                    ended.append(True)

                reader = asyncio.create_task(consume())
                await asyncio.sleep(0.1)
                assert not ended, "the stream must stay open until it is closed"
                await asyncio.to_thread(notifier.close_streams)
                await wait_until(lambda: ended, "the listen stream to end", timeout=3)
                await reader


async def test_a_listen_opened_after_close_streams_is_refused_not_held():
    """uvicorn serves requests until its next tick after should_exit, so a client that
    re-listens as soon as its stream ends gracefully must not be given a stream that
    nothing would end."""
    tools = _server().mcp_tools
    notifier = tools.mcp.list_changed
    loop_thread = threading.current_thread()
    async with notifier.running():
        async with Client(tools.mcp) as client:
            async with client.listen(tools_list_changed=True) as subscription:
                async def drain():
                    async for _event in subscription:
                        pass

                reader = asyncio.create_task(drain())
                await asyncio.to_thread(list_changed.close_streams,
                                          loop_thread)
                await asyncio.wait_for(reader, 3)
            with pytest.raises(MCPError) as refused:
                async with asyncio.timeout(3):
                    async with client.listen(tools_list_changed=True):
                        pass
    assert refused.value.message == list_changed.CLOSING_TEXT


async def test_close_streams_never_raises_into_the_stop():
    """lifecycle.stop_mcp_server sets should_exit right after it: one binding that cannot
    be closed must not skip that."""
    notifier = list_changed.ListChangedNotifier(lambda: 1)
    async with notifier.running() as binding:
        def broken():
            raise OSError("boom")

        binding.close_streams_threadsafe = broken
        list_changed.close_streams(threading.current_thread())


async def test_listen_is_refused_where_no_transport_publishes():
    """Outside a binding nothing would ever arrive on the stream, and a binding on
    another loop could not deliver to it safely."""
    tools = _server().mcp_tools
    async with Client(tools.mcp) as client:
        with pytest.raises(MCPError) as refused:
            async with client.listen(tools_list_changed=True):
                pass
    assert refused.value.message == list_changed.NOT_BOUND_TEXT


# =============================================================================
# --stdio: run_stdio() over a pipe pair
# =============================================================================

@asynccontextmanager
async def stdio_pipe(monkeypatch, mcp):
    """run_stdio() serving one end of an in-memory pipe; yields a client transport for the
    other end. Closing the client's write side is the EOF that ends run_stdio."""
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        @asynccontextmanager
        async def fake_stdio_server():
            yield server_streams

        monkeypatch.setattr(mcp_server_mod, "stdio_server", fake_stdio_server)
        serving_task = asyncio.create_task(mcp.run_stdio())

        @asynccontextmanager
        async def transport():
            try:
                yield client_streams
            finally:
                await client_streams[1].aclose()

        try:
            yield transport()
            await asyncio.wait_for(serving_task, 10)
        finally:
            if not serving_task.done():
                serving_task.cancel()
                await asyncio.gather(serving_task, return_exceptions=True)


async def test_run_stdio_pushes_to_a_handshake_client_and_forgets_it_at_eof(monkeypatch):
    tools, roster = _server().mcp_tools, Roster(monkeypatch)
    heard = Heard()
    async with stdio_pipe(monkeypatch, tools.mcp) as transport:
        async with Client(transport, mode="legacy", message_handler=heard) as client:
            assert names(await client.list_tools()) == NINE
            assert tools.mcp.list_changed.session_count() == 1
            await asyncio.to_thread(reshape, tools, roster, "two")
            await wait_until(lambda: len(heard) >= 1, "the push on the pipe", timeout=3)
            assert names(await client.list_tools()) == NINE + ["browsers"]
    assert len(heard) == 1
    assert tools.mcp.list_changed.session_count() == 0


async def test_run_stdio_serves_listen_to_a_modern_client(monkeypatch):
    tools, roster = _server().mcp_tools, Roster(monkeypatch)
    events = []
    async with stdio_pipe(monkeypatch, tools.mcp) as transport:
        async with Client(transport) as client:
            assert client.protocol_version == "2026-07-28"
            async with client.listen(tools_list_changed=True) as subscription:
                async def consume():
                    async for event in subscription:
                        events.append(event)

                reader = asyncio.create_task(consume())
                await asyncio.to_thread(reshape, tools, roster, "two")
                await wait_until(lambda: events, "a ToolsListChanged event", timeout=3)
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
    assert events == [ToolsListChanged()]


# =============================================================================
# Stop: lifecycle.stop_mcp_server with a listen stream open
# =============================================================================

def _lifecycle(http_app, mcp_port):
    return ServerLifecycle(LifecycleHooks(
        host=lambda: "127.0.0.1", port=lambda: 0, mcp_port=lambda: mcp_port,
        ws_host=lambda: "127.0.0.1", ws_host_source=lambda: "test",
        start_mcp=lambda: True, use_stdio=lambda: False,
        fatal_on_mcp_failure=lambda: False, http_app=http_app, run_stdio=lambda: None,
        ws_handler=lambda websocket: None, ws_process_request=lambda c, r: None,
        local_auth_mode=lambda: "test", token_source=lambda: "test",
        count_in_flight=lambda: 0, open_connections=lambda: [],
        close_registry=lambda *a, **k: None))


LISTEN_ID = "listen-1"


def _listen_request():
    """(headers, body) of the 2026-07-28 subscriptions/listen POST the SDK client sends."""
    meta = {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientInfo": {"name": "raw", "version": "1"},
            "io.modelcontextprotocol/clientCapabilities": {}}
    headers = {**MCP_HEADERS, "mcp-protocol-version": "2026-07-28",
               "mcp-method": "subscriptions/listen"}
    body = {"jsonrpc": "2.0", "id": LISTEN_ID, "method": "subscriptions/listen",
            "params": {"notifications": {"toolsListChanged": True}, "_meta": meta}}
    return headers, body


async def _stop_with_listen_open(*, relisten):
    """lifecycle.stop_mcp_server with a raw subscriptions/listen stream open. Returns
    (messages, refusals, seconds the stop took, whether uvicorn's thread had exited when
    it returned). With `relisten` the client opens another listen the moment its stream
    ends, until one is refused (an error body instead of a stream) or the connection is
    gone.

    The client is a plain httpx2 stream read to its end. The SDK client closes a finished
    response without draining it; on Windows the reset that follows can leave asyncio's
    Server.wait_closed() waiting, and uvicorn's thread then sits out the whole join
    whatever the stream did (8 stops in 150). That race has nothing to do with listen
    streams and would make a timing assertion flaky, so it is not used here."""
    tools = _server().mcp_tools
    port = allocate_port()
    lifecycle = _lifecycle(lambda: tools.mcp.http_app(), port)
    await lifecycle.start_mcp_server()
    await wait_until(lambda: lifecycle.mcp_server_instance is not None
                     and lifecycle.mcp_server_instance.started, "uvicorn listening")
    thread = lifecycle.mcp_thread
    messages, refusals, acknowledged = [], [], asyncio.Event()

    async def hold():
        headers, body = _listen_request()
        async with http_client() as http:
            for _attempt in range(100):
                try:
                    async with http.stream("POST", f"http://127.0.0.1:{port}/mcp",
                                           headers=headers, json=body) as response:
                        if "text/event-stream" not in response.headers["content-type"]:
                            refusals.append(json.loads(await response.aread()))
                            return
                        async for line in response.aiter_lines():
                            if line.startswith("data:"):
                                messages.append(json.loads(line[len("data:"):]))
                                acknowledged.set()
                except httpx2.TransportError:
                    return          # uvicorn is gone
                if not relisten:
                    return

    holder = asyncio.create_task(hold())
    try:
        await asyncio.wait_for(acknowledged.wait(), 10)
        started = time.monotonic()
        await asyncio.to_thread(lifecycle.stop_mcp_server)
        took = time.monotonic() - started
        await asyncio.wait_for(holder, 10)
    finally:
        if not holder.done():
            holder.cancel()
            await asyncio.gather(holder, return_exceptions=True)
        lifecycle.stop_mcp_server()
    return messages, refusals, took, not thread.is_alive()


async def test_a_stop_with_a_listen_stream_open_ends_it_and_returns_promptly():
    """uvicorn waits for every open response before its thread returns, and a listen
    stream never ends by itself: without the close in stop_mcp_server the stop sits out
    its whole 5 s join. The stream ends with its final result, not with a dropped
    connection.

    Served without HttpEndpoints in front (which would add the era gate): this is about
    the stop."""
    messages, _refusals, took, joined = await _stop_with_listen_open(relisten=False)
    assert messages[0]["method"] == "notifications/subscriptions/acknowledged", messages
    assert messages[-1]["id"] == LISTEN_ID and "result" in messages[-1], messages
    assert took < 2.0, f"the stop took {took:.2f}s"
    assert joined, "uvicorn's thread was still running when the stop returned"


async def test_a_client_that_re_listens_at_once_does_not_hold_the_stop():
    """The listen loop the SDK documents: a graceful end is answered with a new listen.
    uvicorn still serves requests until its next tick after should_exit, so a listen
    opened in that window must be refused, not held for the whole join."""
    _messages, refusals, took, joined = await _stop_with_listen_open(relisten=True)
    assert took < 2.0, f"the stop took {took:.2f}s; a late listen was held"
    assert joined, "uvicorn's thread was still running when the stop returned"
    # Whether a listen got in before uvicorn closed its listener depends on timing; one
    # that did was refused for the right reason.
    assert all(r["error"]["message"] == list_changed.CLOSING_TEXT for r in refusals), refusals
