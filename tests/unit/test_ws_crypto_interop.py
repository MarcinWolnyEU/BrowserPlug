"""The real ws-crypto.js against the real server, over a real socket.

Node runs the extension's shared file unchanged (both trees' copies, both ML-KEM paths:
the vendored library and Node's native WebCrypto ML-KEM) against
BrowserPlugServer.handle_extension_connection. This is the test that proves the two
halves of the wire format agree; test_ws_crypto.py covers the failure modes in Python.
"""

import asyncio
import json
import secrets
import shutil
import socket
from pathlib import Path
from unittest.mock import patch

import pytest

from browserplug_shared import device_tokens, request_signing
from server.server import BrowserPlugServer
from server.ws.ws_auth import WS_CLOSE_UNAUTHORIZED

ROOT = Path(__file__).resolve().parents[2]
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

CLIENT = r"""
const fs = require('fs');
const [tree, port, token, kem] = process.argv.slice(2);
if (kem === 'library' && globalThis.crypto.subtle) globalThis.crypto.subtle.encapsulateBits = undefined;
eval(fs.readFileSync(tree + '/vendor/mlkem.js', 'utf8'));
eval(fs.readFileSync(tree + '/ws-crypto.js', 'utf8'));
const out = (o) => console.log(JSON.stringify(o));
const ws = new WebSocket('ws://127.0.0.1:' + port);
ws.binaryType = 'arraybuffer';
const wire = BrowserplugWsCrypto.createWire(ws, {
  onMessage: (text) => {
    const m = JSON.parse(text);
    if (m.id === 'probe-1') { out({ event: 'response', type: m.type }); ws.close(); }
  },
});
ws.onmessage = (ev) => wire.receive(ev.data);
ws.onclose = (ev) => { wire.socketClosed(ev.code, ev.reason); out({ event: 'close', code: ev.code, reason: ev.reason }); };
ws.onopen = () => {
  // Queued behind the hello: nothing may overtake the handshake.
  wire.send(JSON.stringify({ id: 'probe-1', type: 'request', action: 'server.health', data: {} }));
  wire.start(token, (encrypted) => JSON.stringify({ type: 'extension_hello', data: {
    version: '1.9.0', instance_id: 'ff-node', tokenBootstrap: true, encrypted_hello: encrypted,
    pad: 'x'.repeat(1500000) } }))
    .then((r) => out({ event: 'started', encrypted: r.encrypted, kem: r.kem }),
          (e) => out({ event: 'start_failed', message: e.message }));
};
BrowserplugWsCrypto.signRequest(token, 'post', 'http://192.168.0.5:2830/ws-bind', '{"n":"\u00e9"}')
  .then((h) => out({ event: 'signed', header: h }));
BrowserplugWsCrypto.signRequest(token, 'post', 'http://192.168.0.5:2830/restart')
  .then((h) => out({ event: 'signed_bodyless', header: h }));
"""


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def run_client(tmp_path, tree, port, token, kem):
    script = tmp_path / "client.cjs"
    script.write_text(CLIENT, encoding="utf-8")
    proc = await asyncio.create_subprocess_exec(
        NODE, "--no-warnings", str(script), str(ROOT / tree), str(port), token, kem,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await asyncio.wait_for(proc.communicate(), timeout=30)
    events = [json.loads(line) for line in out.decode().splitlines() if line.startswith("{")]
    return events, err.decode()


async def serving(token):
    import websockets
    server = BrowserPlugServer(host="localhost", port=_free_port(), start_mcp=False,
                               ws_token=token)
    ws_server = await websockets.serve(server.handle_extension_connection, "127.0.0.1",
                                       server.port, compression=None, max_size=None)
    return server, ws_server


@pytest.mark.asyncio
@pytest.mark.parametrize("tree", ["extension-gecko", "extension-chromium"])
@pytest.mark.parametrize("kem", ["library", "native"])
async def test_the_extension_wire_talks_to_the_server(tmp_path, tree, kem):
    token = secrets.token_urlsafe(32)
    server, ws_server = await serving(token)
    try:
        # The record leaves the registry when the client disconnects; keep a handle.
        records = []
        register = server.registry.register

        def spy_register(ws, **kw):
            records.append(register(ws, **kw))
            return records[-1]
        with patch.object(server, "_probe_browser_identity", return_value={}),                 patch.object(server.registry, "register", spy_register):
            events, err = await run_client(tmp_path, tree, server.port,
                                           device_tokens.issue(token), kem)
        by = {e["event"]: e for e in events}
        assert by.get("started") == {"event": "started", "encrypted": True, "kem": kem}, (events, err)
        # The request queued before the handshake was answered through the channel.
        assert by.get("response", {}).get("type") == "response", (events, err)
        # The server registered the browser from the sealed hello, with no token in it.
        assert [(r.instance_id, r.encrypted) for r in records] == [("ff-node", True)]
        # And the HTTP signature verifies on the server's side of the format.
        sig = request_signing.verify_detail(by["signed"]["header"], server.ws_auth.device_secret,
                                            "POST", "/ws-bind", guard=request_signing.ReplayGuard())
        assert sig is not None and sig.version == 2
        # The JS body hash is over the UTF-8 bytes fetch() would send (S7).
        assert request_signing.body_problem(sig, '{"n":"\u00e9"}'.encode("utf-8")) is None
        assert request_signing.body_problem(sig, b'{"n":"e"}') is not None
        # Without a body it signs v1, as the server's own signer does, so a server that
        # predates v2 still accepts Restart and the bind panels.
        bare = by["signed_bodyless"]["header"]
        assert bare.startswith("v1 "), bare
        sig = request_signing.verify_detail(bare, server.ws_auth.device_secret,
                                            "POST", "/restart", guard=request_signing.ReplayGuard())
        assert sig is not None and sig.version == 1
        assert request_signing.body_problem(sig, b"") is None
        assert request_signing.body_problem(sig, b"{}") is not None
    finally:
        ws_server.close()
        await ws_server.wait_closed()


@pytest.mark.asyncio
async def test_a_wrong_token_is_closed_with_the_token_reason(tmp_path):
    token = secrets.token_urlsafe(32)
    server, ws_server = await serving(token)
    try:
        # A well-formed device token, issued by a different server.
        events, err = await run_client(tmp_path, "extension-gecko", server.port,
                                       device_tokens.issue(secrets.token_urlsafe(32)),
                                       "library")
        close = next(e for e in events if e["event"] == "close")
        assert close["code"] == WS_CLOSE_UNAUTHORIZED, (events, err)
        # The prefix the extension's handleUnauthorized acts on.
        assert close["reason"].startswith("token required:"), close
        assert server.browsers == {}
        assert not any(e["event"] == "started" for e in events)
    finally:
        ws_server.close()
        await ws_server.wait_closed()


@pytest.mark.asyncio
async def test_a_server_in_auth_off_mode_upgrades_a_registered_socket(tmp_path, monkeypatch):
    """AUTH_OFF (no HTTP app, auto-minted token) registers a socket before reading it, so
    an extension that still holds the token asks for the channel after registration. The
    record must end up on the SecureSocket, and the queued request still gets its answer."""
    import websockets
    from browserplug_shared.ws_token import WS_TOKEN_ENV
    monkeypatch.delenv(WS_TOKEN_ENV, raising=False)
    server = BrowserPlugServer(host="localhost", port=_free_port(), start_mcp=False)
    assert server.ws_auth.auth_mode(peer_local=True) == "off"
    ws_server = await websockets.serve(server.handle_extension_connection, "127.0.0.1",
                                       server.port, compression=None, max_size=None)
    try:
        seen = []
        handle = server.handle_extension_message

        async def spy(message, websocket=None):
            seen.append(bool(getattr(websocket, "encrypted", False)))
            return await handle(message, websocket)
        with patch.object(server, "_probe_browser_identity", return_value={}), \
                patch.object(server, "handle_extension_message", spy):
            events, err = await run_client(tmp_path, "extension-gecko", server.port,
                                           server.ws_auth.issue_device_token(), "library")
        by = {e["event"]: e for e in events}
        assert by.get("started", {}).get("encrypted") is True, (events, err)
        assert by.get("response", {}).get("type") == "response", (events, err)
        assert seen and all(seen), "every message was read through the SecureSocket"
    finally:
        ws_server.close()
        await ws_server.wait_closed()
