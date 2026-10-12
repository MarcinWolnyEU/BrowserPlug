"""server/ws/ws_crypto.py: the encrypted extension WebSocket's handshake and records.

The extension's side is JavaScript; test_ws_crypto_interop.py runs the real thing against
the server. Here a Python client built from the same published schedule drives the server
half, so every failure mode can be provoked byte by byte.
"""

import asyncio
import hashlib
import secrets
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.mlkem import MLKEM768PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from server.ws import ws_crypto
from server.ws.ws_crypto import (DH_LEN, M2_LEN, T_M1, T_M2, T_M3, VERSION,
                                 HandshakeError, RecordError, RecordLayer, ServerHandshake,
                                 _Symmetric, psk_from_token, request_label)
from token_test_helpers import request_frame

ROOT = Path(__file__).resolve().parents[2]

# vendor/mlkem.js is a reproducible esbuild bundle (vendor-src/mlkem/README.md). Move this
# pin with the lock file whenever the bundle is rebuilt.
MLKEM_BUNDLE_SHA256 = "a3aaa45c715ea25b162cffe0a8ad61ee423f952d8bd09a52dc9dc260a898f36b"


LABEL = "dev1"


def secret_lookup(secret):
    return lambda label: secret if label == LABEL else None


def mint():
    return secrets.token_urlsafe(32)


class Client:
    """The extension's half, in Python, following the ws-crypto.js schedule."""

    def __init__(self, token, label=LABEL):
        self.token = token
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
        sym.mix_key_and_hash(psk_from_token(self.token))
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


def handshake(token_server, token_client):
    srv = ServerHandshake(token_server, LABEL)
    cli = Client(token_client)
    records, m3 = srv.finish(cli.respond(srv.first_message()))
    return records, cli.confirm(m3)


def test_matching_tokens_agree_on_keys_in_both_directions():
    token = mint()
    server, client = handshake(token, token)
    assert client.open(server.seal(b"to the extension")) == b"to the extension"
    assert server.open(client.seal(b"to the server")) == b"to the server"


def test_a_wrong_token_fails_as_a_token_mismatch():
    srv = ServerHandshake(mint(), LABEL)
    with pytest.raises(HandshakeError) as err:
        srv.finish(Client(mint()).respond(srv.first_message()))
    assert err.value.token_mismatch


def test_the_label_is_bound_into_the_handshake():
    """The same secret under a different label is a different device: it fails."""
    token = mint()
    srv = ServerHandshake(token, "other")
    with pytest.raises(HandshakeError) as err:
        srv.finish(Client(token, LABEL).respond(srv.first_message()))
    assert err.value.token_mismatch


def test_one_device_token_cannot_pass_as_another():
    """What device tokens are for: a phone holding its own token cannot complete a
    handshake as the desktop browser, so it cannot pose as the server to it either."""
    from browserplug_shared import device_tokens
    master = mint()
    phone = device_tokens.parse(device_tokens.issue(master))
    desktop = device_tokens.parse(device_tokens.issue(master))
    assert phone[1] != desktop[1]
    srv = ServerHandshake(device_tokens.secret_for(master, desktop[0]), desktop[0])
    with pytest.raises(HandshakeError):
        srv.finish(Client(phone[1], desktop[0]).respond(srv.first_message()))
    srv = ServerHandshake(device_tokens.secret_for(master, desktop[0]), desktop[0])
    records, _ = srv.finish(Client(desktop[1], desktop[0]).respond(srv.first_message()))
    assert records is not None


@pytest.mark.asyncio
async def test_an_unknown_label_fails_at_the_proof_not_before_it():
    """No early answer that would tell a prober which labels exist."""
    ws = PipeWS()
    token = mint()
    task = asyncio.create_task(ws_crypto.accept(ws, request_frame("nobody"),
                                                lambda label: None, timeout=5))
    m1 = await ws.outbox.get()
    await ws.inbox.put(Client(token, "nobody").respond(m1))
    with pytest.raises(HandshakeError) as err:
        await task
    assert err.value.token_mismatch


def test_the_first_message_carries_no_token_proof():
    """The server speaks first with nothing derived from the token, so a stranger who opens
    a socket gets no value to test token guesses against offline."""
    token = mint()
    a, b = ServerHandshake(token, LABEL).first_message(), ServerHandshake(token, LABEL).first_message()
    assert len(a) == 2 + DH_LEN + 1184
    assert psk_from_token(token) not in a
    assert a != b, "fresh ephemeral keys on every connection"


def test_a_tampered_proof_is_a_token_mismatch():
    token = mint()
    srv = ServerHandshake(token, LABEL)
    with pytest.raises(HandshakeError) as err:
        srv.finish(Client(token).respond(srv.first_message(), tamper_tag=True))
    assert err.value.token_mismatch


@pytest.mark.parametrize("m2", [b"", bytes(M2_LEN - 1), bytes([VERSION, T_M1]) + bytes(M2_LEN - 2)])
def test_a_malformed_second_message_is_not_a_token_mismatch(m2):
    srv = ServerHandshake(mint(), LABEL)
    srv.first_message()
    with pytest.raises(HandshakeError) as err:
        srv.finish(m2)
    assert not err.value.token_mismatch


def test_a_low_order_x25519_share_is_refused():
    token = mint()
    srv = ServerHandshake(token, LABEL)
    m1 = srv.first_message()
    good = Client(token).respond(m1)
    low_order = bytes(DH_LEN)   # the all-zero point
    with pytest.raises(HandshakeError) as err:
        srv.finish(good[:2] + low_order + good[2 + DH_LEN:])
    assert "X25519" in err.value.reason


def test_a_handshake_cannot_be_finished_twice():
    token = mint()
    srv = ServerHandshake(token, LABEL)
    m2 = Client(token).respond(srv.first_message())
    srv.finish(m2)
    with pytest.raises(HandshakeError):
        srv.finish(m2)


def test_a_tampered_record_fails():
    token = mint()
    server, client = handshake(token, token)
    sealed = bytearray(client.seal(b"hello"))
    sealed[0] ^= 1
    with pytest.raises(RecordError):
        server.open(bytes(sealed))


def test_a_replayed_or_reordered_record_fails():
    token = mint()
    server, client = handshake(token, token)
    first = client.seal(b"one")
    assert server.open(first) == b"one"
    with pytest.raises(RecordError):
        server.open(first)          # replay
    server2, client2 = handshake(token, token)
    client2.seal(b"a")
    b = client2.seal(b"b")
    with pytest.raises(RecordError):
        server2.open(b)             # b before a


def test_the_psk_has_a_fixed_length_whatever_the_token():
    assert len(psk_from_token("x")) == len(psk_from_token("y" * 500)) == 32


def test_is_request_recognises_only_a_well_formed_frame():
    req = request_frame(LABEL)
    assert request_label(req) == LABEL
    assert request_label(bytearray(req)) == LABEL
    assert request_label(request_frame("")) == ""
    assert not ws_crypto.is_request(req.decode("latin-1"))
    assert not ws_crypto.is_request(req + b"\x00")
    assert not ws_crypto.is_request(bytes([VERSION, 0, 40]) + b"x" * 40)
    assert not ws_crypto.is_request('{"type":"extension_hello"}')


class PipeWS:
    """Two queues standing in for a websockets connection."""

    def __init__(self):
        self.inbox = asyncio.Queue()
        self.outbox = asyncio.Queue()
        self.closed = None

    async def send(self, data):
        await self.outbox.put(data)

    async def recv(self):
        return await self.inbox.get()

    async def close(self, code=1000, reason=""):
        self.closed = (code, reason)


@pytest.mark.asyncio
async def test_accept_and_the_secure_socket_round_trip_text():
    token = mint()
    ws = PipeWS()
    client = Client(token)
    task = asyncio.create_task(ws_crypto.accept(ws, request_frame(LABEL), secret_lookup(token), timeout=5))
    m1 = await ws.outbox.get()
    await ws.inbox.put(client.respond(m1))
    records = client.confirm(await ws.outbox.get())
    secure = await task

    await secure.send('{"type":"request"}')
    assert records.open(await ws.outbox.get()) == b'{"type":"request"}'
    await ws.inbox.put(records.seal('{"type":"response"}'.encode()))
    assert await secure.recv() == '{"type":"response"}'
    assert secure.encrypted is True


@pytest.mark.asyncio
async def test_a_plaintext_frame_on_an_encrypted_socket_closes_it():
    token = mint()
    ws = PipeWS()
    client = Client(token)
    task = asyncio.create_task(ws_crypto.accept(ws, request_frame(LABEL), secret_lookup(token), timeout=5))
    await ws.inbox.put(client.respond(await ws.outbox.get()))
    client.confirm(await ws.outbox.get())
    secure = await task
    await ws.inbox.put('{"type":"extension_hello"}')
    with pytest.raises(RecordError):
        await secure.recv()
    assert ws.closed == (ws_crypto.CLOSE_PROTOCOL, "bad record")


@pytest.mark.asyncio
async def test_concurrent_sends_leave_in_counter_order():
    token = mint()
    ws = PipeWS()
    client = Client(token)
    task = asyncio.create_task(ws_crypto.accept(ws, request_frame(LABEL), secret_lookup(token), timeout=5))
    await ws.inbox.put(client.respond(await ws.outbox.get()))
    records = client.confirm(await ws.outbox.get())
    secure = await task
    await asyncio.gather(*(secure.send(str(i)) for i in range(50)))
    got = [records.open(await ws.outbox.get()).decode() for _ in range(50)]
    assert sorted(got, key=int) == [str(i) for i in range(50)]


@pytest.mark.asyncio
async def test_accept_times_out_when_the_extension_stalls():
    ws = PipeWS()
    with pytest.raises(asyncio.TimeoutError):
        await ws_crypto.accept(ws, request_frame(LABEL), secret_lookup(mint()),
                               timeout=0.05)


def test_the_vendored_mlkem_bundle_is_the_pinned_build():
    for tree in ("extension-gecko", "extension-chromium"):
        data = (ROOT / tree / "vendor" / "mlkem.js").read_bytes()
        assert hashlib.sha256(data).hexdigest() == MLKEM_BUNDLE_SHA256, (
            f"{tree}/vendor/mlkem.js is not the pinned build; rebuild it as "
            f"vendor-src/mlkem/README.md says and move MLKEM_BUNDLE_SHA256 with it")
