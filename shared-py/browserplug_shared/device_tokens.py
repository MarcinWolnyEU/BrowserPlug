"""Per-browser device tokens, derived from the server's master token. Stdlib only.

The master token (`ws_token.resolve_token`) never leaves the server. Every browser holds a
device token of its own:

    bp1.<label>.<secret>      secret = base64url(HMAC-SHA-256(master key, DEVICE_INFO + label))

The label travels in the clear in the handshake request, so the server recomputes the
secret from the master and needs no list of issued tokens. What this buys: the encrypted
handshake proves a device secret, and one browser's secret says nothing about another's.
A phone that holds its own token can no longer pose as the server to the desktop
browsers, which it could while every browser shared one token.

The master key is the master token stretched with scrypt, once per process. A minted
master has 256 bits and needs no stretching; a short value pinned with
BROWSERPLUG_WS_TOKEN does, because every device secret is a deterministic function of it
and a recorded handshake lets an attacker test guesses offline.

Loaded both as `browserplug_shared.device_tokens` and as a bare module by gen_ws_token.py, like
ws_token.py, so it imports nothing from the project.
"""

from __future__ import annotations

import base64
import functools
import hashlib
import hmac
import re
import secrets

PREFIX = "bp1"
DEVICE_INFO = b"browserplug device v1\x00"
SCRYPT_SALT = b"browserplug master v1"
# 32 MiB and about a third of a second, once per process: every guess at a short master costs the same.
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 15, 8, 1
LABEL_BYTES = 8
LABEL_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_TOKEN_RE = re.compile(r"^bp1\.([A-Za-z0-9_-]{1,32})\.([A-Za-z0-9_-]{43})$")


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


@functools.lru_cache(maxsize=4)
def master_key(master: str) -> bytes:
    return hashlib.scrypt(master.encode("utf-8"), salt=SCRYPT_SALT, n=SCRYPT_N,
                          r=SCRYPT_R, p=SCRYPT_P, maxmem=64 * 1024 * 1024, dklen=32)


def new_label() -> str:
    return secrets.token_urlsafe(LABEL_BYTES)


def secret_for(master: str, label: str):
    """The device secret for `label`, or None when the label is not a valid one."""
    if not isinstance(label, str) or not LABEL_RE.match(label):
        return None
    return _b64url(hmac.new(master_key(master), DEVICE_INFO + label.encode("ascii"),
                            hashlib.sha256).digest())


def issue(master: str, label: str | None = None) -> str:
    """A device token. A fresh random label unless one is given."""
    label = label or new_label()
    secret = secret_for(master, label)
    if secret is None:
        raise ValueError("invalid device label")
    return f"{PREFIX}.{label}.{secret}"


def parse(token):
    """(label, secret) of a device token, or None for anything else."""
    m = _TOKEN_RE.match((token or "").strip()) if isinstance(token, str) else None
    return (m.group(1), m.group(2)) if m else None
