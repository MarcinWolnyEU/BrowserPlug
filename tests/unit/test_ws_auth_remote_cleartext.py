"""A remote peer can never authenticate in cleartext (S10).

There is no require-encryption switch because there is nothing for it to refuse: since
1.9.1 WsAuth.authenticate turns away a plaintext hello, with or without a token, for every
peer. These tests pin that for a REMOTE peer, the case the review worried about.
"""
import asyncio
import json

import pytest

from server.ws import ws_auth
from server.ws.ws_auth import WsAuth


class _WS:
    remote_address = ("192.168.0.50", 51000)

    def __init__(self, frames):
        self._frames = list(frames)
        self.close_code = None
        self.close_reason = None

    async def recv(self):
        if self._frames:
            return self._frames.pop(0)
        await asyncio.sleep(30)

    async def close(self, code=1000, reason=""):
        self.close_code, self.close_reason = code, reason


def _auth():
    return WsAuth(token=lambda: "x" * 43, token_explicit=lambda: True,
                  auth_timeout=lambda: 2.0, http_port=lambda: 2830)


@pytest.mark.asyncio
@pytest.mark.parametrize("hello_data", [{"token": "x" * 43, "instanceId": "a"},
                                        {"instanceId": "a", "tokenBootstrap": True}])
async def test_a_remote_plaintext_hello_is_refused_with_or_without_the_token(hello_data):
    ws = _WS([json.dumps({"type": "extension_hello", "data": hello_data})])
    ok, replay, _ = await _auth().authenticate(ws, ws_auth.AUTH_STRICT, peer_local=False)
    assert ok is False and replay == []
    assert ws.close_code == ws_auth.WS_CLOSE_UNAUTHORIZED
    assert "127.0.0.1" not in ws.close_reason  # no loopback endpoint offered to a remote peer
