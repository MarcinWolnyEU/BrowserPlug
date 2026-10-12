"""Who may call what on the MCP HTTP port once it follows the LAN/WAN bind.

The port used to answer 127.0.0.1 only, so the bind WAS the policy and `/health` never
checked a peer at all. It now binds wherever the WebSocket does, and http_auth.authorize
takes over: local peers free, remote peers token-authenticated, `/mcp` and `/ws-token`
refused to remote peers whatever they present.

The pure-policy tests carry the detail. The ASGI ones at the bottom exist because the
policy passing proves nothing about the gate being CALLED — that is the failure this
codebase keeps hitting (the StreamFilter arming code stayed dead through 343 green JS
tests while every helper it used was fine), and here the cost of an uncalled gate is an
open `/mcp` on the LAN.
"""

import json
import socket

import pytest

from secure_ext import signed_headers
from server.ws import http_auth
from server.server import BrowserPlugServer

TOKEN = "a" * 43
OTHER = "b" * 43


def authorize(path, peer, offered=None, expected=TOKEN):
    """`offered == expected` stands for a request carrying a valid X-BrowserPlug-Auth
    signature; test_request_signing.py covers the signature itself."""
    return http_auth.authorize(path, peer, signed=(offered == expected))


# --- the peer, before any endpoint runs ------------------------------------------------

@pytest.mark.parametrize("peer", ["127.0.0.1", "::1", "::ffff:127.0.0.1", "localhost", ""])
def test_a_local_peer_needs_no_token(peer):
    """The launcher polls /health with no token, and an extension bootstrapping its very
    first token has none to offer. Both predate this gate and must keep working."""
    decision, status, _ = authorize("/health", peer)
    assert decision == http_auth.ALLOW
    assert status == 200


def test_a_remote_peer_without_a_token_is_refused():
    decision, status, message = authorize("/health", "192.168.30.171")
    assert decision == http_auth.DENY_TOKEN
    assert status == 403
    assert "X-BrowserPlug-Auth" in message


def test_a_remote_peer_with_the_wrong_token_is_refused():
    decision, _, _ = authorize("/health", "192.168.30.171", offered=OTHER)
    assert decision == http_auth.DENY_TOKEN


def test_a_remote_peer_with_the_right_token_is_allowed():
    decision, status, message = authorize("/health", "192.168.30.171", offered=TOKEN)
    assert decision == http_auth.ALLOW
    assert (status, message) == (200, None)


# --- the two paths that stay on this machine whatever the setting says ------------------

def test_mcp_is_refused_to_a_remote_peer_even_with_a_valid_token():
    """The decision of 2026-09-02: a leaked token costs a look at the server's state, not
    the ability to drive a logged-in browser."""
    decision, status, _ = authorize("/mcp", "192.168.30.171", offered=TOKEN)
    assert decision == http_auth.DENY_LOOPBACK_ONLY
    assert status == 403


@pytest.mark.parametrize("path", ["/mcp", "/mcp/", "/mcp/messages", "/mcp/session/abc"])
def test_every_mcp_sub_path_is_loopback_only(path):
    """Prefix-matched on purpose: the sub-paths are where the tool calls actually ride, so
    an exact-string gate would leave the dangerous half open."""
    assert http_auth.is_loopback_only(path) is True
    assert authorize(path, "10.0.0.9", offered=TOKEN)[0] == http_auth.DENY_LOOPBACK_ONLY


def test_ws_token_is_loopback_only_even_with_a_valid_token():
    """It hands out the credential, so it cannot be protected by the credential."""
    decision, _, _ = authorize("/ws-token", "192.168.30.171", offered=TOKEN)
    assert decision == http_auth.DENY_LOOPBACK_ONLY


def test_a_local_peer_still_reaches_the_loopback_only_paths():
    assert authorize("/mcp", "127.0.0.1")[0] == http_auth.ALLOW
    assert authorize("/ws-token", "127.0.0.1")[0] == http_auth.ALLOW


@pytest.mark.parametrize("path", ["/mcpx", "/mcp-tools", "/health"])
def test_a_path_that_merely_starts_with_mcp_is_not_loopback_only(path):
    assert http_auth.is_loopback_only(path) is False


def test_a_trailing_slash_does_not_change_the_answer():
    assert http_auth.is_loopback_only("/ws-token/") is True
    assert authorize("/health/", "192.168.30.171", offered=TOKEN)[0] == http_auth.ALLOW


# --- header extraction and the bind ------------------------------------------------------

@pytest.mark.parametrize("ws_host,expected", [
    ("0.0.0.0", "0.0.0.0"),
    ("::", "::"),
    (" 0.0.0.0 ", "0.0.0.0"),
    ("localhost", "localhost"),
    ("127.0.0.1", "localhost"),
    (None, "localhost"),
])
def test_the_http_port_follows_the_websocket_bind(ws_host, expected):
    """One setting governs both ports, which is what the options page claims it does."""
    assert http_auth.mcp_bind_host(ws_host, "localhost") == expected


# --- the gate is actually WIRED IN --------------------------------------------------------

def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def http_get(app, path, client, headers=None):
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": "GET", "path": path,
             "headers": headers or [], "client": client}
    await app(scope, receive, send)
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, body


def _server():
    return BrowserPlugServer(host="localhost", port=_free_port(),
                             start_mcp=False, ws_token=TOKEN)


@pytest.mark.asyncio
async def test_remote_health_without_a_token_is_refused_through_the_real_wrapper():
    server = _server()
    app = server._wrap_client_tracking(lambda *a: None)
    status, body = await http_get(app, "/health", ("192.168.30.171", 44444))
    assert status == 403
    assert "token" in json.loads(body)["error"]


@pytest.mark.asyncio
async def test_remote_health_with_the_token_is_served_through_the_real_wrapper():
    server = _server()
    app = server._wrap_client_tracking(lambda *a: None)
    status, body = await http_get(app, "/health", ("192.168.30.171", 44444),
                                  headers=signed_headers(server, "GET", "/health"))
    assert status == 200
    assert json.loads(body)["service"] == "browserplug"


@pytest.mark.asyncio
async def test_the_raw_token_header_of_old_extensions_is_refused():
    """Before 1.9 the page sent the token itself in X-BrowserPlug-Token, in the clear on a
    LAN bind. Even the right value there no longer opens anything."""
    server = _server()
    app = server._wrap_client_tracking(lambda *a: None)
    status, _ = await http_get(app, "/health", ("192.168.30.171", 44444),
                               headers=[(b"x-browserplug-token", TOKEN.encode())])
    assert status == 403


@pytest.mark.asyncio
async def test_remote_mcp_is_refused_through_the_real_wrapper_even_with_the_token():
    """If this ever passes a request through, the LAN has full tool access."""
    server = _server()
    reached = []

    async def inner(scope, receive, send):
        reached.append(scope.get("path"))

    app = server._wrap_client_tracking(inner)
    status, _ = await http_get(app, "/mcp", ("192.168.30.171", 44444),
                               headers=signed_headers(server, "GET", "/mcp"))
    assert status == 403
    assert reached == [], "the MCP app must never see a remote request"


@pytest.mark.asyncio
async def test_local_health_still_needs_no_token():
    server = _server()
    app = server._wrap_client_tracking(lambda *a: None)
    status, _ = await http_get(app, "/health", ("127.0.0.1", 5555))
    assert status == 200


@pytest.mark.asyncio
async def test_local_doctor_is_served_through_the_real_wrapper():
    """GET /doctor's handler has its own unit tests, driven directly; only this proves the
    dispatch line in server.py actually routes the path to it."""
    server = _server()
    app = server._wrap_client_tracking(lambda *a: None)
    status, body = await http_get(app, "/doctor", ("127.0.0.1", 5555))
    assert status == 200
    payload = json.loads(body)
    assert payload["service"] == "browserplug"
    assert "extension" in {c["name"] for c in payload["checks"]}
    assert "BrowserPlug preflight:" in payload["summary"]


@pytest.mark.asyncio
async def test_remote_doctor_is_refused_even_with_the_token():
    """The payload names local paths and pids, so the handler keeps its own loopback check
    behind the gate: a remote peer with a valid token still gets nothing."""
    server = _server()
    app = server._wrap_client_tracking(lambda *a: None)
    status, body = await http_get(app, "/doctor", ("192.168.30.171", 44444),
                                  headers=signed_headers(server, "GET", "/doctor"))
    assert status == 403
    assert json.loads(body) == {"error": "localhost only"}
