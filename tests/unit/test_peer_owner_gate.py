"""S3: a loopback peer owned by ANOTHER OS user must not get /mcp, /ws-token or /health.

The fakes stand in for the process table (pid lookup + psutil owner), which is the one
sanctioned mock here: a second OS account cannot be conjured inside a unit test. The
gate tests drive the real `_wrap_client_tracking` wrapper.
"""

import json
import socket
import sys
import types

import pytest

from server.reporting import client_identity
from server.server import BrowserPlugServer

ME = "DESKTOP-marcin"
OTHER = "DESKTOP-guest"


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeProc:
    def __init__(self, pid=None):
        self.pid = pid

    def username(self):
        if self.pid is None:
            return ME
        owner = FakeProc.owners[self.pid]
        if isinstance(owner, Exception):
            raise owner
        return owner

    owners = {}


@pytest.fixture
def procs(monkeypatch):
    """peer port -> pid -> owner tables the fakes read."""
    ports = {}
    FakeProc.owners = {}
    lookups = []

    def pid_for_port(port):
        lookups.append(port)
        return ports.get(port)

    monkeypatch.setattr(client_identity, "pid_for_local_port", pid_for_port)
    monkeypatch.setattr("server.web.peer_owner._server_owner", None)  # the once-read cache
    fake_psutil = types.SimpleNamespace(
        Process=lambda pid=None: FakeProc(pid),
        AccessDenied=type("AccessDenied", (Exception,), {}),
    )
    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)
    return types.SimpleNamespace(ports=ports, owners=FakeProc.owners, lookups=lookups)


async def _get(app, path, client):
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    await app({"type": "http", "method": "GET", "path": path, "headers": [],
               "client": client}, receive, send)
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, body


def _app(reached):
    server = BrowserPlugServer(host="localhost", port=_free_port(), start_mcp=False,
                               ws_token="t" * 8)

    async def inner(scope, receive, send):
        reached.append(scope["path"])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    return server._wrap_client_tracking(inner)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/mcp", "/ws-token", "/health"])
async def test_a_loopback_peer_owned_by_another_user_is_refused(procs, path):
    procs.ports[50001] = 900
    procs.owners[900] = OTHER
    reached = []
    status, body = await _get(_app(reached), path, ("127.0.0.1", 50001))
    assert status == 403, body
    assert reached == []
    assert "user" in json.loads(body)["error"]


@pytest.mark.asyncio
async def test_a_loopback_peer_owned_by_the_same_user_is_served(procs):
    procs.ports[50002] = 901
    procs.owners[901] = ME.upper()  # Windows account names compare case-insensitively
    reached = []
    status, _ = await _get(_app(reached), "/mcp", ("127.0.0.1", 50002))
    assert status == 200 and reached == ["/mcp"]


@pytest.mark.asyncio
async def test_an_unresolvable_peer_fails_open(procs):
    """No pid for the port (socket already closed, no psutil rights): serve, as before."""
    reached = []
    status, _ = await _get(_app(reached), "/mcp", ("127.0.0.1", 50003))
    assert status == 200


@pytest.mark.asyncio
async def test_a_pid_whose_owner_cannot_be_read_fails_open(procs):
    procs.ports[50004] = 902
    procs.owners[902] = RuntimeError("process vanished")
    status, _ = await _get(_app([]), "/mcp", ("127.0.0.1", 50004))
    assert status == 200


@pytest.mark.asyncio
async def test_the_verdict_is_cached_per_port_and_pid(procs):
    procs.ports[50005] = 903
    procs.owners[903] = OTHER
    app = _app([])
    for _ in range(3):
        status, _ = await _get(app, "/mcp", ("127.0.0.1", 50005))
        assert status == 403
    assert procs.lookups.count(50005) == 1


@pytest.mark.asyncio
async def test_a_remote_peer_never_reaches_the_owner_lookup(procs):
    status, _ = await _get(_app([]), "/health", ("192.168.30.171", 44444))
    assert status == 403
    assert procs.lookups == []


def test_the_decision_function_directly(procs):
    from server.web import peer_owner
    procs.ports[7] = 1
    procs.owners[1] = OTHER
    assert peer_owner.resolve_peer(7)[0] is True
    procs.owners[1] = ME
    assert peer_owner.resolve_peer(7)[0] is False
    assert peer_owner.resolve_peer(None)[0] is False
