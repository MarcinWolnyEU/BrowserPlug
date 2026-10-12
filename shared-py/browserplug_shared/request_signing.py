"""Proof of a device token on an HTTP request, without the token on the wire.

Before 1.9 the options page sent the raw token in `X-BrowserPlug-Token`. On a LAN or WAN
bind that header crossed the network in cleartext. The page now sends
`X-BrowserPlug-Auth`, keyed by its device token (shared-py/browserplug_shared/device_tokens.py):

    v2 <label> <unix seconds> <nonce> <body hash> <mac>
    key = HMAC-SHA-256(key="browserplug http v2", msg=device secret)
    mac = base64url(HMAC-SHA-256(key, "browserplug http v2" LF METHOD LF PATH LF ts LF nonce LF body hash))
    body hash = base64url(SHA-256(request body)); the hash of the empty string for no body

    v1 <label> <unix seconds> <nonce> <mac>        (no body hash; what older extensions send)

`signRequest` in `extension-gecko/ws-crypto.js` is the other half. A timestamp outside
`WINDOW_SECONDS` or a nonce already seen inside it is refused, so a captured header is good
for one request to one method and path. The scheme version is the header's first word.

The header is verified before the body is read, so `verify` only proves the CLAIMED body
hash; `body_problem` then checks the bytes that arrived against it. v1 signs no body, so a
man in the middle could swap one: the server accepts v1 only for a request without a body,
and `body_problem` refuses the rest with a message telling the sender to update.

Nonces are kept in the state directory as well as in memory, so a restart does not forget
them. Otherwise a captured request would replay once per restart for `WINDOW_SECONDS`.
Stdlib only.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import time
from typing import NamedTuple, Optional

AUTH_HEADER = "x-browserplug-auth"
LABEL = b"browserplug http v1"
LABEL_V2 = b"browserplug http v2"
NONCE_FILE_NAME = "http-nonces"
# Wide enough for a phone whose clock drifts, narrow enough that the nonce memory stays small.
WINDOW_SECONDS = 300
_NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_MAC_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_HASH_RE = _MAC_RE   # a base64url SHA-256 is the same 43 characters as a MAC

UPDATE_MESSAGE = ("this request has a body, but its X-BrowserPlug-Auth signature (v1) does "
                  "not cover the body; update the BrowserPlug extension to a build that "
                  "signs it (v2)")
BODY_MISMATCH_MESSAGE = "the request body does not match the body hash that was signed"


class Signature(NamedTuple):
    version: int
    body_hash: Optional[str]   # None for v1, which signs no body


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _key(secret: str, label: bytes = LABEL) -> bytes:
    return hmac.new(label, secret.encode("utf-8"), hashlib.sha256).digest()


def body_hash(body: bytes) -> str:
    return _b64url(hashlib.sha256(body or b"").digest())


def mac_for(secret: str, method: str, path: str, ts: int, nonce: str) -> str:
    msg = b"\n".join([LABEL, method.upper().encode(), path.encode("utf-8"),
                      str(ts).encode(), nonce.encode()])
    return _b64url(hmac.new(_key(secret), msg, hashlib.sha256).digest())


def mac_v2(secret: str, method: str, path: str, ts: int, nonce: str, bhash: str) -> str:
    msg = b"\n".join([LABEL_V2, method.upper().encode(), path.encode("utf-8"),
                      str(ts).encode(), nonce.encode(), bhash.encode()])
    return _b64url(hmac.new(_key(secret, LABEL_V2), msg, hashlib.sha256).digest())


def sign(label: str, secret: str, method: str, path: str, now: float | None = None,
         nonce: str | None = None, body: bytes | None = None) -> str:
    """The header value for a request. A `body` (even b"") makes it v2, which covers the
    body; without one it is the legacy v1 form, good only for a request that has none."""
    import secrets
    ts = int(time.time() if now is None else now)
    nonce = nonce or _b64url(secrets.token_bytes(16))
    if body is None:
        return f"v1 {label} {ts} {nonce} {mac_for(secret, method, path, ts, nonce)}"
    bh = body_hash(body)
    return f"v2 {label} {ts} {nonce} {bh} {mac_v2(secret, method, path, ts, nonce, bh)}"


class ReplayGuard:
    """Nonces seen inside the window. Bounded: a flood evicts the oldest first, and an
    evicted nonce's timestamp is by then close to leaving the window anyway.

    With a `path`, each first use is also appended there as `<seen at> <nonce>` and the
    file is read back on construction, so a restarted server still refuses a captured
    request. Nonces are not secrets. A file that cannot be read or written degrades to
    the in-memory guard."""

    def __init__(self, limit: int = 4096, path: str | None = None):
        self._seen: dict[str, float] = {}
        self._limit = limit
        self._path = path
        self._appended = 0
        if path:
            self._load()

    def _load(self):
        cutoff = time.time() - 2 * WINDOW_SECONDS
        try:
            with open(self._path, "r", encoding="ascii", errors="ignore") as f:
                for line in f:
                    try:
                        stamp, nonce = line.split()
                        stamp = float(stamp)
                    except ValueError:
                        continue   # a torn last line after a crash
                    if stamp >= cutoff and _NONCE_RE.match(nonce):
                        self._seen[nonce] = stamp
        except OSError:
            return
        while len(self._seen) > self._limit:
            del self._seen[next(iter(self._seen))]
        self._rewrite()

    def _rewrite(self):
        try:
            os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
            tmp = self._path + ".tmp"
            with open(tmp, "w", encoding="ascii") as f:
                f.writelines(f"{t:.3f} {n}\n" for n, t in self._seen.items())
            os.replace(tmp, self._path)
            self._appended = 0
        except OSError:
            pass

    def first_use(self, nonce: str, now: float) -> bool:
        cutoff = now - 2 * WINDOW_SECONDS
        if len(self._seen) >= self._limit or (self._seen and next(iter(self._seen.values())) < cutoff):
            for key in [k for k, t in self._seen.items() if t < cutoff]:
                del self._seen[key]
            while len(self._seen) >= self._limit:
                del self._seen[next(iter(self._seen))]
        if nonce in self._seen:
            return False
        self._seen[nonce] = now
        if self._path:
            self._persist(nonce, now)
        return True

    def _persist(self, nonce: str, now: float):
        # Append-only between compactions, so a health poll costs one short write.
        if self._appended >= self._limit:
            self._rewrite()
            return
        try:
            with open(self._path, "a", encoding="ascii") as f:
                f.write(f"{now:.3f} {nonce}\n")
            self._appended += 1
        except FileNotFoundError:
            self._rewrite()
        except OSError:
            pass


_GUARD: ReplayGuard | None = None


def _default_guard() -> ReplayGuard:
    """The process-wide guard, backed by a file in the state directory. Built on first
    use, not at import, and rebuilt if the directory moved (tests relocate it per test)."""
    global _GUARD
    try:
        from browserplug_shared.config_store import state_dir
        path = os.path.join(state_dir(), NONCE_FILE_NAME)
    except Exception:
        path = None
    if _GUARD is None or _GUARD._path != path:
        _GUARD = ReplayGuard(path=path)
    return _GUARD


def verify_detail(header_value, secret_for, method: str, path: str,
                  now: float | None = None, guard: ReplayGuard | None = None) -> Signature | None:
    """The Signature this header proves for this method and path, once, or None.
    `secret_for(label)` returns the device secret for a label, or None."""
    if not header_value or not isinstance(header_value, str):
        return None
    parts = header_value.strip().split(" ")
    if parts[0] == "v1" and len(parts) == 5:
        _, label, ts_text, nonce, mac = parts
        bhash = None
    elif parts[0] == "v2" and len(parts) == 6:
        _, label, ts_text, nonce, bhash, mac = parts
        if not _HASH_RE.match(bhash):
            return None
    else:
        return None
    # ASCII only: isdigit() also accepts superscripts, and int() then raises.
    if not (ts_text.isascii() and ts_text.isdigit()) or not _NONCE_RE.match(nonce) or not _MAC_RE.match(mac):
        return None
    now = time.time() if now is None else now
    if abs(now - int(ts_text)) > WINDOW_SECONDS:
        return None
    secret = secret_for(label)
    if not secret:
        return None
    expected = (mac_for(secret, method, path, int(ts_text), nonce) if bhash is None
                else mac_v2(secret, method, path, int(ts_text), nonce, bhash))
    if not hmac.compare_digest(expected, mac):
        return None
    # Only a valid MAC may spend a nonce, or a stranger could burn the page's nonces.
    if not (guard or _default_guard()).first_use(nonce, now):
        return None
    return Signature(2 if bhash is not None else 1, bhash)


def verify(header_value, secret_for, method: str, path: str,
           now: float | None = None, guard: ReplayGuard | None = None) -> bool:
    """Does this header prove a device secret for this method and path, once?"""
    return verify_detail(header_value, secret_for, method, path, now, guard) is not None


def body_problem(signature: Signature | None, body: bytes) -> str | None:
    """Why the body that arrived does not fit the verified signature, or None. Call it
    once the body has been read; `verify` never saw it."""
    if signature is None:
        return None
    if signature.body_hash is None:
        return UPDATE_MESSAGE if body else None
    if not hmac.compare_digest(body_hash(body), signature.body_hash):
        return BODY_MISMATCH_MESSAGE
    return None


def header_from_scope(scope) -> str:
    try:
        for name, value in scope.get("headers") or []:
            if name == AUTH_HEADER.encode("latin-1"):
                return value.decode("latin-1").strip()
    except Exception:
        pass
    return ""


SCOPE_KEY = "browserplug.signature"


def verify_scope(scope, secret_for) -> bool:
    """The ASGI form: the method and path come from the request itself. The verified
    Signature is left in the scope for `body_problem`."""
    sig = verify_detail(header_from_scope(scope), secret_for,
                        scope.get("method") or "GET", scope.get("path") or "/")
    scope[SCOPE_KEY] = sig
    return sig is not None
