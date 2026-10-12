"""The proxy transport's handshake gate (server/proxy/front.py ProxyServer._handshake_gate).

The gate is what makes a proxy with no server behind it fail its client's connect instead
of opening a session that lists nothing (see front.py's docstring). Ported from the
pre-SDK tests of the launcher's handshake-gate module: both hooks (initialize and
server/discover) pass a healthy handshake through, a transport failure becomes one
MCPError naming the backend, the probe is bounded, a backend's own protocol error and a
cancellation pass through untouched, and a real proxy over a healthy backend still
connects in both protocol eras with the gate firing in each. The gate now also carries the
backend's instructions into the front handshake (the old proxy's metadata middleware did
that with a second backend connection).

Also here, the control for test_launcher.py's TestBackendlessProxyHandshake (a real proxy
against a dead port fails the client's handshake in both eras): without the gate, that
proxy would connect and list nothing.
"""

import asyncio
import socket
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import httpx2
import pytest
from mcp import Client
from mcp.shared.exceptions import MCPError

from proxy_doubles import (BACKEND_INSTRUCTIONS, BACKEND_URL, ASGIBackend, FakeBackend,
                           backend_server, config)
from server.proxy import policy
from server.proxy.backend import Backend, BackendUnreachable
from server.proxy.front import ProxyServer

HOOKS = ("initialize", "server/discover")


class _Ctx:
    def __init__(self, method):
        self.method = method
        self.session = None
        self.params = {}


def _gate(backend):
    return ProxyServer(config(), backend=backend, report_timing=AsyncMock())


async def _never_answers():
    await asyncio.Event().wait()


def _dead_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class TestGate:

    @pytest.mark.parametrize("hook", HOOKS, ids=["on_initialize", "on_discover"])
    async def test_a_reachable_backend_lets_the_handshake_through(self, hook):
        server = _gate(FakeBackend())
        call_next = AsyncMock(return_value={"protocolVersion": "2025-11-25"})
        context = _Ctx(hook)
        result = await server._handshake_gate(context, call_next)
        call_next.assert_awaited_once_with(context)
        assert result == {"protocolVersion": "2025-11-25", "instructions": BACKEND_INSTRUCTIONS}

    @pytest.mark.parametrize("hook", HOOKS, ids=["on_initialize", "on_discover"])
    async def test_an_unreachable_backend_fails_the_handshake_naming_it(self, hook):
        error = BackendUnreachable(httpx2.ConnectError("All connection attempts failed"))

        async def refused():
            raise error

        server = _gate(FakeBackend(probe=refused))
        call_next = AsyncMock()
        with pytest.raises(MCPError) as failure:
            await server._handshake_gate(_Ctx(hook), call_next)
        call_next.assert_not_awaited()      # the front handshake never answers success
        assert failure.value.code == policy.INTERNAL_ERROR
        assert BACKEND_URL in failure.value.message
        assert "All connection attempts failed" in failure.value.message
        assert "preflight.py" in failure.value.message
        assert failure.value.__cause__ is error

    async def test_a_backend_that_never_answers_fails_within_the_bound(self, monkeypatch):
        monkeypatch.setattr(policy, "PROBE_TIMEOUT_SECONDS", 0.2)
        server = _gate(FakeBackend(probe=_never_answers))
        async with asyncio.timeout(10):
            with pytest.raises(MCPError) as failure:
                await server._handshake_gate(_Ctx("initialize"), AsyncMock())
        assert BACKEND_URL in failure.value.message
        assert "TimeoutError" in failure.value.message

    async def test_a_backend_protocol_error_passes_through_unchanged(self):
        refused = MCPError(code=-32602, message="backend refused initialize")

        async def probe():
            raise refused

        server = _gate(FakeBackend(probe=probe))
        with pytest.raises(MCPError) as failure:
            await server._handshake_gate(_Ctx("initialize"), AsyncMock())
        assert failure.value is refused

    async def test_cancellation_is_not_converted(self):
        """Only Exception is translated; a cancelled handshake stays cancelled."""
        async def cancelled():
            raise asyncio.CancelledError()

        server = _gate(FakeBackend(probe=cancelled))
        with pytest.raises(asyncio.CancelledError):
            await server._handshake_gate(_Ctx("initialize"), AsyncMock())

    async def test_other_requests_pass_without_a_probe(self):
        backend = FakeBackend()
        server = _gate(backend)
        call_next = AsyncMock(return_value={"tools": []})
        assert await server._handshake_gate(_Ctx("tools/list"), call_next) == {"tools": []}
        assert backend.calls == []

    async def test_the_proxys_own_instructions_would_win(self):
        server = _gate(FakeBackend())
        call_next = AsyncMock(return_value={"instructions": "the front's own"})
        result = await server._handshake_gate(_Ctx("initialize"), call_next)
        assert result["instructions"] == "the front's own"


@asynccontextmanager
async def _healthy_backend():
    app = backend_server().http_app()
    async with app.router.lifespan_context(app):
        yield ASGIBackend(app)


class TestGateOnARealProxy:

    @pytest.mark.parametrize("mode", ["legacy", "auto"])
    async def test_a_healthy_backend_connects_through_the_gate_in_both_eras(self, mode):
        """The gate must fire in each era (initialize, server/discover) and still let a
        healthy handshake complete; a gate that silently never ran would pass the connect
        assertion alone."""
        async with _healthy_backend() as backend:
            server = ProxyServer(config(), backend=backend, report_timing=AsyncMock())
            probes = []
            real = server.require_backend

            async def counting():
                probes.append(1)
                return await real()

            server.require_backend = counting
            async with asyncio.timeout(30):
                async with Client(server, mode=mode) as client:
                    listed = [t.name for t in (await client.list_tools()).tools]
                    assert client.instructions == BACKEND_INSTRUCTIONS
        assert listed == ["echo", "degraded", "boom", "picture"]
        assert probes, f"the gate never ran on the {mode} handshake"


class TestBackendlessProxyHandshake:
    """A proxy with nothing behind its backend URL, end to end through the SDK client."""

    async def test_control_without_the_gate_the_proxy_connects_empty(self):
        """Why the gate exists: without it, a backendless proxy connects and lists nothing
        (tools/list answers [] when the backend cannot be listed, as the old proxy's did)."""
        url = f"http://127.0.0.1:{_dead_port()}/mcp"
        server = ProxyServer(config(backend_url=url), backend=Backend(config(backend_url=url)),
                             report_timing=AsyncMock())
        server.middleware = [m for m in server.middleware
                             if getattr(m, "__name__", "") != "_handshake_gate"]
        async with asyncio.timeout(30):
            async with Client(server, mode="legacy") as client:
                assert (await client.list_tools()).tools == []
