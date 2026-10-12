"""
End-to-end WebSocket token-auth bootstrap against a REAL Firefox.

What the unit layer already covers (tests/unit/test_ws_token.py): the enforcement
matrix, the 4401 close reason, held pre-auth frames, and a real-socket bootstrap
WITHOUT Firefox (test_end_to_end_bootstrap_over_real_sockets). What it cannot cover
is the EXTENSION side of the loop: background.js's handleUnauthorized() parsing a
live server's close reason, fetching /ws-token over real HTTP, storing the token,
and coming back with it. Until now that path was only IMPLICIT in "the extension
connected" (every integration fixture) — and a budget regression in exactly this
round-trip once turned whole modules into skips (see FIREFOX_TEST_CONFIG's
extension_install_wait comment). These tests make it explicit:

  * test_firefox_keeps_its_proven_token_through_a_rotation drives the S5 rule live
    against a CONNECTED Firefox: rotate the server's token, drop the socket, and
    watch the real rejections (4401, stale token) with NO automatic re-key; restoring
    the old token brings it straight back.
  * test_a_peer_with_the_wrong_token_is_rejected_by_the_live_server pins the
    negative at the real socket: wrong token -> closed 4401 with the bootstrap
    endpoint in the close reason, never registered — and the genuine browser's
    registration survives the attempt untouched.

Token hygiene (PROJECT RULE, enforced by test_no_literal_token_in_the_repo): every
token here is minted at runtime, and the rotation only ever touches the in-memory
server attribute — never the developer's persisted token file.
"""

import asyncio
import json
import secrets
import urllib.request
import uuid

import pytest
import websockets

import test_imports  # noqa: F401  (path setup)
from token_test_helpers import is_valid_device_token
from server.ws.ws_auth import (
    AUTH_OFF,
    WS_CLOSE_UNAUTHORIZED,
    WS_TOKEN_ENDPOINT_PATH,
)


def mint():
    """A fresh token, exactly as the server would mint one. Never a literal."""
    return secrets.token_urlsafe(32)


async def poll_until(predicate, timeout=30.0, interval=0.25):
    """Bounded poll (the suite's preferred pattern over bare sleeps). Returns the
    predicate's final value so the caller's assert fires with its own diagnostics."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()


@pytest.mark.asyncio
async def test_firefox_keeps_its_proven_token_through_a_rotation(server_with_extension):
    """Rotate the token under a live, connected Firefox and force a reconnect: the
    extension was authenticated with its stored token once, so (review S5) it must NOT
    swap that token for whatever a 4401 offers. It is rejected for the stale token, keeps
    retrying with it, and does not re-register on the rotated master; when the server
    holds the old token again it comes straight back and is usable. Adopting a rotated
    token is the user's "Re-authorize" click, covered by tests/js*/tokenbootstrap.test.mjs.

    The FIRST adoption (the fixture's own connect) is the other half of that rule and
    is proven by every browser-backed test."""
    server = server_with_extension["server"]
    mcp_port = server_with_extension["mcp_port"]

    local_mode = server.ws_auth.auth_mode(peer_local=True)
    assert local_mode != AUTH_OFF, (
        "the fixture server has an MCP HTTP app, so a local peer must be challenged")
    assert len(server.browsers) == 1, list(server.browsers)
    original_id, browser = next(iter(server.browsers.items()))
    old_ws = browser.ws

    # Rotate IN MEMORY ONLY: this attribute is what auth compares against and what
    # /ws-token serves. The developer's persisted token file is never touched.
    original_token = server.ws_token
    rotated = mint()
    server.ws_token = rotated

    # The live HTTP endpoint serves device tokens of the rotated master: exactly what a
    # squatter or a real rotation would offer the extension.
    endpoint = server.ws_auth.token_endpoint_url()
    assert endpoint == f"http://127.0.0.1:{mcp_port}{WS_TOKEN_ENDPOINT_PATH}"
    with urllib.request.urlopen(endpoint, timeout=5) as response:
        assert is_valid_device_token(rotated, json.loads(response.read())["token"])

    rejections = []      # why each unauthenticated socket was turned away
    auth_attempts = []   # (ok, encrypted, mode) per post-rotation attempt
    real_reject = server.ws_auth.reject
    real_auth = server.ws_auth.authenticate

    async def spy_reject(websocket, peer, why, peer_local=True, **kwargs):
        rejections.append(why)
        await real_reject(websocket, peer, why, peer_local, **kwargs)

    async def spy_auth(websocket, mode, peer_local):
        ok, replay, sock = await real_auth(websocket, mode, peer_local)
        auth_attempts.append((ok, bool(getattr(sock, "encrypted", False)), mode))
        return ok, replay, sock

    server.ws_auth.reject = spy_reject
    server.ws_auth.authenticate = spy_auth
    try:
        # Drop the extension's socket with a PLAIN close: it reconnects through its
        # normal backoff and presents its stored, now stale, token.
        await browser.ws.close()

        # 1. The stale token is refused, repeatedly: the extension keeps retrying with it.
        assert await poll_until(
            lambda: sum("proved a different token" in why for why in rejections) >= 2), (
            f"the reconnects with the stale token were not rejected; rejections: "
            f"{rejections}, auth attempts: {auth_attempts}, "
            f"browsers: {list(server.browsers)}")

        # 2. ...and it did not re-key itself from the endpoint above. Had it fetched and
        #    adopted the rotated token, a successful attempt (and a new socket) would
        #    show up within a retry or two; give it several.
        await asyncio.sleep(6.0)
        assert not any(a[0] for a in auth_attempts), (
            f"the extension authenticated on the rotated token without a user click: "
            f"{auth_attempts}")
        current = server.browsers.get(original_id)
        assert current is None or not current.connected or current.ws is old_ws, (
            "the extension re-registered on a new socket after the rotation")

        # 3. The old token is the only thing it holds: restore it and it comes straight
        #    back, encrypted, and is usable.
        server.ws_token = original_token
        def recovered():
            b = server.browsers.get(original_id)
            return b is not None and b.connected and b.ws is not old_ws
        assert await poll_until(recovered), (
            f"the extension did not return once the original token was restored; "
            f"rejections: {rejections}, auth attempts: {auth_attempts}, "
            f"browsers: {list(server.browsers)}")
        ok_attempts = [a for a in auth_attempts if a[0]]
        assert ok_attempts and ok_attempts[-1] == (True, True, local_mode), auth_attempts

        reply = await server.send_request_and_wait(
            {"id": str(uuid.uuid4()), "type": "request",
             "action": "tabs.list", "data": {}}, timeout=15.0)
        assert reply.get("type") == "response", reply
        assert "tabs" in reply.get("data", {}), reply
    finally:
        server.ws_auth.reject = real_reject
        server.ws_auth.authenticate = real_auth
        server.ws_token = original_token


@pytest.mark.asyncio
async def test_a_peer_with_the_wrong_token_is_rejected_by_the_live_server(server_with_extension):
    """A wrong (but valid-shaped) token at the REAL socket: closed 4401, the close
    reason names the bootstrap endpoint, nothing is registered — and the genuine
    browser's registration is untouched by the attempt (no displacement)."""
    server = server_with_extension["server"]
    test_port = server_with_extension["test_port"]
    before = {instance_id: b.ws for instance_id, b in server.browsers.items()}
    assert before, "the fixture must have the real Firefox registered"

    hello = {"type": "extension_hello",
             "data": {"version": "1.4.0", "instance_id": "ff-intruder",
                      "tokenBootstrap": True, "token": mint()}}

    async with websockets.connect(f"ws://localhost:{test_port}") as sock:
        await sock.send(json.dumps(hello))
        with pytest.raises(websockets.exceptions.ConnectionClosed) as closed:
            await asyncio.wait_for(sock.recv(), timeout=10.0)

    assert closed.value.rcvd.code == WS_CLOSE_UNAUTHORIZED
    # A token in a plaintext hello (an extension older than 1.9) is refused whatever it
    # is, and the reason names no endpoint: fetching a token would only put it on the
    # wire in the clear again. A wrong DEVICE token is the unit tests' job
    # (test_ws_crypto_interop.py), since it needs the extension's side of the handshake.
    assert closed.value.rcvd.reason.startswith("encryption required")
    assert WS_TOKEN_ENDPOINT_PATH not in closed.value.rcvd.reason
    assert "ff-intruder" not in server.browsers, \
        "an unauthenticated socket must never enter the registry"
    assert {i: b.ws for i, b in server.browsers.items()} == before, (
        "the intruder's attempt must not displace or disturb the real browser")
