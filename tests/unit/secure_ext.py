"""The extension's side of the encrypted handshake, for tests that need a logged-in browser.

From 1.9.1 the server refuses a plaintext hello that carries a token, so a test that wants
an authenticated extension has to do what ws-crypto.js does: send the request frame with
its device label, answer the server's first message, check its proof, and send the hello
as the first sealed record. This module does that against the server's own primitives,
following the schedule documented in server/ws/ws_crypto.py.

Two forms:
  * secure_fake_hello(ws, server, hello): for the unit tests' queue-backed FakeWS classes.
    It rewires that one FakeWS instance so the test keeps reading and feeding plain JSON:
    `ws.sent` still collects decoded messages and `ws.feed(dict)` seals what it feeds.
  * SecureClient over a real `websockets` client connection.
"""

import asyncio
import json

from cryptography.hazmat.primitives.asymmetric.mlkem import MLKEM768PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from browserplug_shared import device_tokens
from server.ws.ws_crypto import (DH_LEN, T_M1, T_M2, T_M3, VERSION, RecordLayer, _Symmetric,
                                 psk_from_token)
from token_test_helpers import request_frame


class Client:
    """The extension's half of the handshake for one device secret and label."""

    def __init__(self, secret, label):
        self.secret = secret
        self.label = label

    def respond(self, m1, tamper_tag=False, e_c=None):
        assert m1[:2] == bytes([VERSION, T_M1])
        e_s, ek = m1[2:2 + DH_LEN], m1[2 + DH_LEN:]
        sym = _Symmetric(self.label)
        sym.mix_hash(e_s)
        sym.mix_hash(ek)
        x = X25519PrivateKey.generate()
        e_c = e_c or x.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        sym.mix_hash(e_c)
        sym.mix_key(x.exchange(X25519PublicKey.from_public_bytes(e_s)))
        ss, ct = MLKEM768PublicKey.from_public_bytes(ek).encapsulate()
        sym.mix_hash(ct)
        sym.mix_key(ss)
        sym.mix_key_and_hash(psk_from_token(self.secret))
        tag = sym.encrypt_and_hash(b"")
        if tamper_tag:
            tag = bytes([tag[0] ^ 1]) + tag[1:]
        self.sym = sym
        return bytes([VERSION, T_M2]) + e_c + ct + tag

    def confirm(self, m3):
        assert m3[:2] == bytes([VERSION, T_M3])
        self.sym.decrypt_and_hash(m3[2:])
        to_ext, to_srv = self.sym.split()
        return RecordLayer(send_key=to_srv, recv_key=to_ext)


def device_token(server):
    """A device token of this server's, as /ws-token hands one out."""
    return device_tokens.issue(server.ws_token)


def client_for(token):
    label, secret = device_tokens.parse(token) or ("", token)
    return Client(secret, label)


async def secure_fake_hello(ws, server, hello, token=None, timeout=5.0):
    """Log `ws` (a FakeWS with a `_q` queue) in through the encrypted handshake and send
    `hello` sealed (none when it is None). Returns the client's RecordLayer. The handler
    task must be running."""
    token = token or device_token(server)
    client = client_for(token)
    plain_send = ws.send
    captured = asyncio.Queue()

    async def capture(raw):
        await captured.put(raw)
    ws.send = capture
    ws._q.put_nowait(request_frame(client.label))
    m1 = await asyncio.wait_for(captured.get(), timeout)
    ws._q.put_nowait(client.respond(m1))
    m3 = await asyncio.wait_for(captured.get(), timeout)
    records = client.confirm(m3)

    async def unseal(raw):
        await plain_send(records.open(raw).decode("utf-8"))
    ws.send = unseal
    ws.feed = lambda message: ws._q.put_nowait(records.seal(json.dumps(message).encode()))
    ws.feed_raw = lambda raw: ws._q.put_nowait(
        records.seal(raw.encode() if isinstance(raw, str) else raw))
    if hello is not None:
        ws.feed(hello)
    return records


async def failed_handshake(ws, token, timeout=5.0):
    """Run the extension's half with `token` up to its proof, for a test that expects the
    server to refuse it: the server closes instead of sending its own proof."""
    client = client_for(token)
    captured = asyncio.Queue()

    async def capture(raw):
        await captured.put(raw)
    ws.send = capture
    ws._q.put_nowait(request_frame(client.label))
    m1 = await asyncio.wait_for(captured.get(), timeout)
    ws._q.put_nowait(client.respond(m1))


def signed_headers(server, method, path, token=None, body=b""):
    """ASGI headers carrying a valid X-BrowserPlug-Auth signature for one request. A
    request that sends a body passes it here: the (v2) signature covers its bytes."""
    from browserplug_shared import request_signing
    label, secret = device_tokens.parse(token or device_token(server))
    value = request_signing.sign(label, secret, method, path, body=body)
    return [(request_signing.AUTH_HEADER.encode("latin-1"), value.encode("latin-1"))]


class SecureClient:
    """A real `websockets` client connection after the handshake: JSON in, JSON out."""

    def __init__(self, sock, records):
        self.sock = sock
        self.records = records

    @classmethod
    async def connect(cls, sock, token, hello, timeout=5.0):
        client = client_for(token)
        await sock.send(request_frame(client.label))
        m1 = await asyncio.wait_for(sock.recv(), timeout)
        await sock.send(client.respond(m1))
        m3 = await asyncio.wait_for(sock.recv(), timeout)
        self = cls(sock, client.confirm(m3))
        await self.send(hello)
        return self

    async def send(self, message):
        await self.sock.send(self.records.seal(json.dumps(message).encode()))

    async def recv(self):
        return json.loads(self.records.open(await self.sock.recv()))
