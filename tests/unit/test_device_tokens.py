"""shared-py/browserplug_shared/device_tokens.py: one token per browser, derived from a master that stays put."""

import hashlib
import hmac
import logging
import secrets

import pytest

from browserplug_shared import device_tokens as dt
from token_test_helpers import is_valid_device_token


def mint():
    return secrets.token_urlsafe(32)


def test_a_device_token_has_the_documented_shape_and_verifies():
    master = mint()
    token = dt.issue(master)
    label, secret = dt.parse(token)
    assert token == f"bp1.{label}.{secret}"
    assert is_valid_device_token(master, token)
    assert master not in token


def test_every_issue_is_a_different_device():
    master = mint()
    a, b = dt.issue(master), dt.issue(master)
    assert a != b
    assert dt.parse(a)[0] != dt.parse(b)[0]


def test_one_devices_secret_says_nothing_about_anothers():
    """The point of the scheme: a phone holding its own token cannot produce the desktop's
    secret, so it cannot complete the desktop's handshake or pose as the server to it."""
    master = mint()
    phone_label, phone_secret = dt.parse(dt.issue(master))
    desktop_label, desktop_secret = dt.parse(dt.issue(master))
    assert phone_secret != desktop_secret
    assert not is_valid_device_token(master, f"bp1.{desktop_label}.{phone_secret}")


def test_a_token_from_another_master_is_not_valid():
    assert not is_valid_device_token(mint(), dt.issue(mint()))


def test_the_master_is_stretched_before_it_derives_anything():
    """A short master pinned by hand is guessed offline from a recorded handshake; scrypt
    makes each guess expensive. The derivation must not be a bare HMAC of the master."""
    master = "short"
    label = "abc"
    bare = hmac.new(master.encode(), dt.DEVICE_INFO + label.encode(), hashlib.sha256).digest()
    assert dt.secret_for(master, label) != dt._b64url(bare)
    assert dt.master_key(master) == hashlib.scrypt(
        master.encode(), salt=dt.SCRYPT_SALT, n=dt.SCRYPT_N, r=dt.SCRYPT_R, p=dt.SCRYPT_P,
        maxmem=64 * 1024 * 1024, dklen=32)


@pytest.mark.parametrize("label", ["", "a" * 33, "has space", "dot.dot", None, 7])
def test_an_invalid_label_has_no_secret(label):
    assert dt.secret_for(mint(), label) is None


@pytest.mark.parametrize("junk", ["", None, "bp1", "bp1.x", "bp2.abc." + "A" * 43,
                                  "bp1.abc." + "A" * 42, "bp1.abc." + "A" * 43 + "x",
                                  secrets.token_urlsafe(32)])
def test_anything_else_does_not_parse(junk):
    assert dt.parse(junk) is None


def test_a_short_explicit_master_draws_a_warning(caplog):
    from server.server import BrowserPlugServer
    with caplog.at_level(logging.WARNING):
        BrowserPlugServer(host="localhost", port=8772, start_mcp=False, ws_token="short")
    assert any("stretched with scrypt" in r.getMessage() for r in caplog.records)
