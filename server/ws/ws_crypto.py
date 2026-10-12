"""The encrypted extension WebSocket: the server's half of the handshake and the records.

The extension half is `extension-gecko/ws-crypto.js`; the two must agree byte for byte, and
`tests/unit/test_ws_crypto_interop.py` drives one against the other to prove it. The design
and the reasons behind it are in `.tmp/dev-docs/ws-encryption.md`; this docstring is the wire format.

The handshake is shaped after Noise `NNhfs+psk2`, with the server as the Noise initiator:
fresh X25519 and ML-KEM-768 keys on both sides, and the browser's device secret
(shared-py/browserplug_shared/device_tokens.py) mixed in last as a pre-shared key. It is not a conforming Noise
implementation (no payloads, the KEM key is not encrypted), so do not expect a Noise
library to talk to it.

    ext -> srv  REQ  01 00 | len | label                         the device token's label
    srv -> ext  M1   01 01 | e_s[32] | ek[1184]                   no token involved
    ext -> srv  M2   01 02 | e_c[32] | ct[1088] | tag[16]          the extension's proof
    srv -> ext  M3   01 03 | tag[16]                               the server's proof

    h = ck = SHA-256(PROTOCOL_NAME); MixHash(PROLOGUE); MixHash(label)
    M1: MixHash(e_s); MixHash(ek)
    M2: MixHash(e_c); MixKey(X25519(e_s, e_c)); MixHash(ct); MixKey(ML-KEM shared secret)
        MixKeyAndHash(psk); tag = EncryptAndHash("")
    M3: tag = EncryptAndHash("")
    Split(): the first key seals server -> extension, the second extension -> server

The extension proves the token first, so a stranger who opens a socket learns nothing they
could test token guesses against. Records are one binary frame each: AES-256-GCM under the
direction's key, nonce = 4 zero bytes + the big-endian 64-bit record counter, no associated
data. A dropped, replayed or reordered frame fails authentication because the receiver's
counter no longer matches, and any failure closes the socket.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import threading

from cryptography.hazmat.primitives.asymmetric.mlkem import MLKEM768PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

PROTOCOL_NAME = b"BrowserPlug_NNhfs+psk2_25519+MLKEM768_AESGCM_SHA256"
PROLOGUE = b"browserplug ws v1"
PSK_LABEL = b"browserplug psk v1"

VERSION = 1
T_REQ, T_M1, T_M2, T_M3 = 0, 1, 2, 3
DH_LEN, EK_LEN, CT_LEN, TAG_LEN = 32, 1184, 1088, 16
MAX_LABEL = 32
M2_LEN = 2 + DH_LEN + CT_LEN + TAG_LEN

CLOSE_PROTOCOL = 4400
MAX_COUNTER = 2**64 - 1


class HandshakeError(Exception):
    """Carries the close code the server should send; `token_mismatch` marks the one
    failure that means "wrong token" rather than "malformed"."""

    def __init__(self, reason: str, token_mismatch: bool = False):
        super().__init__(reason)
        self.reason = reason
        self.token_mismatch = token_mismatch


class RecordError(Exception):
    pass


def request_label(frame):
    """The label out of the extension's request frame, or None if this is not one. An
    empty label is a request too (a token that is not a device token); it fails later,
    at the proof, like any other wrong token."""
    if not isinstance(frame, (bytes, bytearray, memoryview)):
        return None
    frame = bytes(frame)
    if len(frame) < 3 or frame[0] != VERSION or frame[1] != T_REQ:
        return None
    if frame[2] > MAX_LABEL or len(frame) != 3 + frame[2]:
        return None
    try:
        return frame[3:].decode("ascii")
    except UnicodeDecodeError:
        return None


def is_request(frame) -> bool:
    """Is this first frame the extension asking for the encrypted channel?"""
    return request_label(frame) is not None


def psk_from_token(secret: str) -> bytes:
    """A fixed-length key from the device secret (or any string)."""
    return hmac.new(PSK_LABEL, secret.encode("utf-8"), hashlib.sha256).digest()


def _hkdf(ck: bytes, ikm: bytes, n: int) -> list[bytes]:
    temp = hmac.new(ck, ikm, hashlib.sha256).digest()
    out, prev = [], b""
    for i in range(1, n + 1):
        prev = hmac.new(temp, prev + bytes([i]), hashlib.sha256).digest()
        out.append(prev)
    return out


def _nonce(counter: int) -> bytes:
    return bytes(4) + counter.to_bytes(8, "big")


class _Symmetric:
    """Noise's SymmetricState, reduced to the operations this handshake uses."""

    def __init__(self, label: str = ""):
        self.h = hashlib.sha256(PROTOCOL_NAME).digest()
        self.ck = self.h
        self.k = None
        self.n = 0
        self.mix_hash(PROLOGUE)
        self.mix_hash(label.encode("ascii"))

    def mix_hash(self, data: bytes) -> None:
        self.h = hashlib.sha256(self.h + data).digest()

    def mix_key(self, ikm: bytes) -> None:
        self.ck, self.k = _hkdf(self.ck, ikm, 2)
        self.n = 0

    def mix_key_and_hash(self, ikm: bytes) -> None:
        self.ck, temp_h, self.k = _hkdf(self.ck, ikm, 3)
        self.mix_hash(temp_h)
        self.n = 0

    def encrypt_and_hash(self, plaintext: bytes) -> bytes:
        ct = AESGCM(self.k).encrypt(_nonce(self.n), plaintext, self.h)
        self.n += 1
        self.mix_hash(ct)
        return ct

    def decrypt_and_hash(self, ct: bytes) -> bytes:
        pt = AESGCM(self.k).decrypt(_nonce(self.n), ct, self.h)
        self.n += 1
        self.mix_hash(ct)
        return pt

    def split(self) -> tuple[bytes, bytes]:
        k1, k2 = _hkdf(self.ck, b"", 2)
        return k1, k2


class RecordLayer:
    """AES-256-GCM records with an implicit counter per direction. Any failure is fatal:
    the peer's counter has moved on, so the caller must close the connection."""

    def __init__(self, send_key: bytes, recv_key: bytes):
        self._send = AESGCM(send_key)
        self._recv = AESGCM(recv_key)
        self._send_n = 0
        self._recv_n = 0
        # A threading lock, not an asyncio one: whichever thread seals must take the next
        # counter alone, because two records under one nonce break AES-GCM outright. This
        # protects the counter only; SecureSocket still orders seal-then-send per loop.
        self._seal_lock = threading.Lock()

    def seal(self, plaintext: bytes) -> bytes:
        with self._seal_lock:
            if self._send_n > MAX_COUNTER:
                raise RecordError("record counter exhausted")
            n = self._send_n
            self._send_n = n + 1
            return self._send.encrypt(_nonce(n), plaintext, None)

    def open(self, record: bytes) -> bytes:
        if self._recv_n > MAX_COUNTER:
            raise RecordError("record counter exhausted")
        try:
            pt = self._recv.decrypt(_nonce(self._recv_n), bytes(record), None)
        except Exception as exc:  # InvalidTag, or a frame shorter than the tag
            raise RecordError("record failed authentication") from exc
        self._recv_n += 1
        return pt


class ServerHandshake:
    """One connection's handshake: `first_message()`, then `finish(m2)`."""

    def __init__(self, secret, label: str = ""):
        # No secret for this label: run the handshake with a random one anyway, so the
        # failure comes at the proof and looks like any other wrong token.
        self._psk = psk_from_token(secret if secret else os.urandom(32).hex())
        self._sym = _Symmetric(label)
        self._x = X25519PrivateKey.generate()
        self._kem = MLKEM768PrivateKey.generate()
        self._done = False

    def first_message(self) -> bytes:
        e = self._x.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        ek = self._kem.public_key().public_bytes_raw()
        self._sym.mix_hash(e)
        self._sym.mix_hash(ek)
        return bytes([VERSION, T_M1]) + e + ek

    def finish(self, m2) -> tuple[RecordLayer, bytes]:
        """Verify the extension's proof. Returns the record layer and M3 to send."""
        if self._done:
            raise HandshakeError("handshake already finished")
        self._done = True
        if not isinstance(m2, (bytes, bytearray)) or len(m2) != M2_LEN \
                or m2[0] != VERSION or m2[1] != T_M2:
            raise HandshakeError("malformed handshake message")
        m2 = bytes(m2)
        e_c = m2[2:2 + DH_LEN]
        ct = m2[2 + DH_LEN:2 + DH_LEN + CT_LEN]
        tag = m2[2 + DH_LEN + CT_LEN:]
        sym = self._sym
        sym.mix_hash(e_c)
        try:
            # cryptography raises on an all-zero result (a low-order peer point).
            dh = self._x.exchange(X25519PublicKey.from_public_bytes(e_c))
        except ValueError as exc:
            raise HandshakeError("degenerate X25519 share") from exc
        sym.mix_key(dh)
        sym.mix_hash(ct)
        # Implicit rejection: a bad ciphertext yields a random secret and fails the tag.
        sym.mix_key(self._kem.decapsulate(ct))
        self._x = self._kem = None
        sym.mix_key_and_hash(self._psk)
        try:
            sym.decrypt_and_hash(tag)
        except Exception as exc:
            raise HandshakeError("handshake proof mismatch", token_mismatch=True) from exc
        m3 = bytes([VERSION, T_M3]) + sym.encrypt_and_hash(b"")
        to_ext, to_srv = sym.split()
        return RecordLayer(send_key=to_ext, recv_key=to_srv), m3


class SecureSocket:
    """A websockets connection that carries only sealed records.

    Stands in for the raw connection everywhere the server keeps one (the browser registry
    included), so `send(json_text)` and `async for message in ws` keep working unchanged.
    Every other attribute (remote_address, close, close_code, ...) is the raw socket's.
    `send` holds a lock across seal and send, so two tasks sending at once cannot put
    counter n+1 on the wire before counter n. That asyncio.Lock binds to the first loop that
    contends it, so every send must run on the loop that owns the socket (the WebSocket
    loop); ExtensionRpc.send_to_extension hops there for callers on other loops.
    """

    encrypted = True
    device_label = ""

    def __init__(self, ws, records: RecordLayer):
        self._ws = ws
        self._records = records
        self._send_lock = asyncio.Lock()

    def __getattr__(self, name):
        return getattr(self._ws, name)

    @property
    def raw(self):
        return self._ws

    async def send(self, data) -> None:
        if isinstance(data, str):
            data = data.encode("utf-8")
        async with self._send_lock:
            await self._ws.send(self._records.seal(bytes(data)))

    async def recv(self) -> str:
        frame = await self._ws.recv()
        return await self._open(frame)

    async def _open(self, frame) -> str:
        try:
            if isinstance(frame, str):
                raise RecordError("plaintext frame on an encrypted channel")
            return self._records.open(frame).decode("utf-8")
        except (RecordError, UnicodeDecodeError) as exc:
            try:
                await self._ws.close(CLOSE_PROTOCOL, "bad record")
            except Exception:
                pass
            raise RecordError(str(exc)) from exc

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        from websockets.exceptions import ConnectionClosedOK
        try:
            frame = await self._ws.recv()
        except ConnectionClosedOK:
            raise StopAsyncIteration from None
        return await self._open(frame)


async def accept(ws, request, secret_for, timeout: float) -> SecureSocket:
    """Run the server side after the extension's request frame. `secret_for(label)` is the
    device secret for a label, or None. Raises HandshakeError (the caller closes the
    socket with the right code) or asyncio.TimeoutError."""
    label = request_label(request)
    if label is None:
        raise HandshakeError("malformed handshake request")
    hs = ServerHandshake(secret_for(label) if label else None, label)
    await ws.send(hs.first_message())
    m2 = await asyncio.wait_for(ws.recv(), timeout)
    if isinstance(m2, str):
        raise HandshakeError("malformed handshake message")
    records, m3 = hs.finish(m2)
    await ws.send(m3)
    secure = SecureSocket(ws, records)
    secure.device_label = label
    return secure
