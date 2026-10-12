"""server/proxy/backend.py: the proxy transport's backend leg, the SDK client.

Against a real BrowserPlugMCPServer app in-process (proxy_doubles.ASGIBackend), so the SDK's
streamable HTTP client, the session manager and the SSE answers are all real; only the TCP
socket is not. Ported from the pre-SDK test_launcher.py TestProbesIgnoreProxyEnv and
TestProxyWiring: the backend's httpx2 client is proxy-blind, each request gets a fresh
session (a server restart is then transparent), the client-pid header rides every request,
and the backend is pinned to the handshake era.
"""

import socket
from contextlib import asynccontextmanager

import httpx2
import mcp_types
import pytest
from mcp.shared.exceptions import MCPError

from browserplug_shared.version import CLIENT_PID_HEADER
from proxy_doubles import DEGRADED_META, ASGIBackend, backend_server, config
from server.proxy import backend as backend_mod
from server.proxy.backend import Backend, BackendUnreachable


def _dead_port():
    with socket.socket() as probe:                      # a port with nothing behind it
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@asynccontextmanager
async def running_backend():
    """An ASGIBackend in front of a running backend app, its exchanges in the task group
    the proxy holds for a connection (Backend.running). A context manager, not a fixture:
    the app's lifespan must be entered and left in the test's own task."""
    app = backend_server().http_app()
    async with app.router.lifespan_context(app):
        live = ASGIBackend(app)
        async with live.running():
            yield live


# ---- the httpx2 client ------------------------------------------------------------------

def test_the_backend_client_is_proxy_blind_and_carries_the_client_pid():
    client = Backend(config()).http_client()     # lazy: constructing it opens nothing
    # mcp>=2 drives the client with httpx2; an httpx client fails inside the transport.
    assert isinstance(client, httpx2.AsyncClient)
    assert client._trust_env is False
    assert client.follow_redirects is True
    assert client.headers[CLIENT_PID_HEADER] == "4242"
    assert (client.timeout.connect, client.timeout.read) == (30.0, 300.0)


def test_the_backend_client_ignores_a_real_proxy_in_the_env(monkeypatch):
    """The behavioural guarantee, not just the flag: with HTTP_PROXY exported, the backend
    client builds no proxy mounts. httpx reads env proxies ONLY when trust_env is true, so
    this proves both that the flag took and that it is the flag that matters."""
    for var in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY"):
        monkeypatch.setenv(var, "http://127.0.0.1:1")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)

    client = Backend(config()).http_client()
    assert client._trust_env is False
    proxy_mounts = [k for k in getattr(client, "_mounts", {}) if k.pattern != "all://"]
    assert not proxy_mounts, f"backend client picked up env proxies: {proxy_mounts}"


# ---- sessions ---------------------------------------------------------------------------

async def test_every_request_gets_a_session_of_its_own_and_closes_it():
    """A reused session would outlive a server restart (its id dies with the server) and
    leak one request's state into the next. Each request: initialize, the request, DELETE."""
    async with running_backend() as live:
        first = await live.call_tool("echo", {"text": "a"}, None)
        second = await live.call_tool("echo", {"text": "b"}, None)
        assert first["content"][0]["text"] == "a" and second["content"][0]["text"] == "b"
        sessions = live.sessions()
        assert len(sessions) == 2 and len(set(sessions)) == 2 and None not in sessions
        assert len(live.clients) == 2 and live.clients[0] is not live.clients[1]
        posts = [h for m, _p, h in live.requests if m == "POST"]
        assert posts and all(h.get(CLIENT_PID_HEADER.lower()) == "4242" for h in posts), (
            "the server keys the client on this header: the TCP peer is the proxy")
        assert all(p == "/mcp" for _m, p, _h in live.requests), "no /mcp/ redirect hop"


async def test_the_backend_session_speaks_the_handshake_era():
    """The 2026-07-28 era sends no Mcp-Session-Id, and the server's client tracking keys on
    it; the backend is pinned to initialize."""
    async with running_backend() as live:
        async with live.session() as session:
            assert session.protocol_version == "2025-11-25"
            assert session.initialize_result is not None
        first_post = next(h for m, _p, h in live.requests if m == "POST")
        assert "mcp-session-id" not in first_post, "initialize opens the session"


async def test_the_probe_returns_the_backends_instructions():
    async with running_backend() as live:
        result = await live.probe()
        assert result.instructions == "Backend instructions, forwarded by the proxy."
        assert len(live.sessions()) == 1


async def test_results_come_back_as_the_wire_dict_field_for_field():
    async with running_backend() as live:
        degraded = await live.call_tool("degraded", {}, None)
        assert degraded == {"content": [{"type": "text", "text": "partly"}], "isError": False,
                            "_meta": DEGRADED_META}
        failed = await live.call_tool("boom", {}, None)
        assert failed["isError"] is True
        assert failed["content"][0]["text"] == "Error calling tool 'boom': kaboom detail"
        picture = await live.call_tool("picture", {}, None)
        assert [c["type"] for c in picture["content"]] == ["text", "image"]
        assert picture["content"][1] == {"type": "image", "data": "aGk=", "mimeType": "image/png"}


async def test_a_list_is_read_whole():
    async with running_backend() as live:
        tools = await live.list_all("tools/list", "tools")
        assert [t["name"] for t in tools] == ["echo", "degraded", "boom", "picture"]
        assert await live.list_all("prompts/list", "prompts") == []


async def test_request_meta_reaches_the_backend_without_connection_keys():
    async with running_backend() as live:
        meta = backend_mod.forwardable_request_meta({
            "progressToken": 7, mcp_types.PROTOCOL_VERSION_META_KEY: "2026-07-28",
            mcp_types.CLIENT_INFO_META_KEY: {"name": "x", "version": "1"}})
        assert meta == {"progressToken": 7}
        assert backend_mod.forwardable_request_meta({mcp_types.CLIENT_INFO_META_KEY: {}}) is None
        result = await live.call_tool("echo", {}, meta)
        assert result["content"][0]["text"] == "pong"


def test_result_meta_drops_the_backends_connection_keys_only():
    stamped = {"content": [], "_meta": {mcp_types.SERVER_INFO_META_KEY: {"name": "b"},
                                        "browserplug/degraded": {"x": 1}}}
    assert backend_mod.forwardable_result_meta(stamped) == {
        "content": [], "_meta": {"browserplug/degraded": {"x": 1}}}
    only = {"content": [], "_meta": {mcp_types.SERVER_INFO_META_KEY: {"name": "b"}}}
    assert backend_mod.forwardable_result_meta(only) == {"content": []}
    plain = {"content": []}
    assert backend_mod.forwardable_result_meta(plain) is plain


# ---- failures ---------------------------------------------------------------------------

async def test_a_dead_backend_is_one_error_with_the_old_proxys_text():
    """The SDK reports a refused connect as an exception group around httpx2's error; the
    proxy shows the client the old proxy's text however the failure arrived."""
    dead = Backend(config(backend_url=f"http://127.0.0.1:{_dead_port()}/mcp"))
    with pytest.raises(BackendUnreachable) as failure:
        await dead.ping()
    assert str(failure.value) == "Client failed to connect: All connection attempts failed"
    assert isinstance(failure.value.cause, httpx2.ConnectError)


async def test_an_error_the_backend_answered_passes_through_as_it_is():
    async with running_backend() as live:
        with pytest.raises(MCPError) as failure:
            await live.send(mcp_types.GetPromptRequest(
                params=mcp_types.GetPromptRequestParams(name="nothing")))
        assert failure.value.code == -32602
        assert failure.value.message == "Unknown prompt: 'nothing'"


async def test_cancellation_is_not_turned_into_an_error():
    async with running_backend() as live:
        import anyio
        with anyio.move_on_after(0) as scope:
            await live.ping()
        assert scope.cancelled_caught, "the cancellation stayed a cancellation"

async def test_a_group_that_carries_a_cancellation_is_never_converted():
    """A failure that surfaces while a cancellation unwinds must not turn the cancellation
    into an ordinary error (the request would then answer instead of being cancelled)."""
    import anyio
    cancelled = anyio.get_cancelled_exc_class()
    refused = httpx2.ConnectError("refused")
    assert backend_mod._root_cause(ExceptionGroup("g", [refused])) is refused
    assert backend_mod._root_cause(BaseExceptionGroup("g", [refused, cancelled()])) is None
    assert backend_mod._root_cause(cancelled()) is None
