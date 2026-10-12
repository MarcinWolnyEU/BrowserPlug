"""shared-py/browserplug_shared/request_signing.py: the signed header that replaced the raw token on HTTP."""

import secrets

from browserplug_shared import device_tokens
from browserplug_shared import request_signing as rs

NOW = 1_800_000_000


def device():
    """(master, label, secret, lookup): one browser's device token and the server's view."""
    master = secrets.token_urlsafe(32)
    label, secret = device_tokens.parse(device_tokens.issue(master))
    return master, label, secret, (lambda lab: device_tokens.secret_for(master, lab))


def test_a_fresh_signature_verifies_once():
    _, label, secret, lookup = device()
    guard = rs.ReplayGuard()
    header = rs.sign(label, secret, "GET", "/health", now=NOW)
    assert rs.verify(header, lookup, "GET", "/health", now=NOW, guard=guard)
    assert not rs.verify(header, lookup, "GET", "/health", now=NOW, guard=guard), \
        "a captured header is good for one request"


def test_neither_the_secret_nor_the_master_is_in_the_header():
    master, label, secret, _ = device()
    header = rs.sign(label, secret, "POST", "/ws-bind", now=NOW)
    assert secret not in header and master not in header


def test_a_signature_is_bound_to_the_method_and_the_path():
    _, label, secret, lookup = device()
    header = rs.sign(label, secret, "GET", "/health", now=NOW)
    assert not rs.verify(header, lookup, "POST", "/health", now=NOW, guard=rs.ReplayGuard())
    assert not rs.verify(header, lookup, "GET", "/ws-bind", now=NOW, guard=rs.ReplayGuard())


def test_another_servers_device_token_does_not_verify():
    _, label, secret, _ = device()
    _, _, _, other_server = device()
    header = rs.sign(label, secret, "GET", "/health", now=NOW)
    assert not rs.verify(header, other_server, "GET", "/health", now=NOW, guard=rs.ReplayGuard())


def test_a_device_cannot_sign_under_another_devices_label():
    master, label, secret, lookup = device()
    other_label, _ = device_tokens.parse(device_tokens.issue(master))
    header = rs.sign(other_label, secret, "GET", "/health", now=NOW)
    assert not rs.verify(header, lookup, "GET", "/health", now=NOW, guard=rs.ReplayGuard())


def test_a_stale_or_future_timestamp_is_refused():
    _, label, secret, lookup = device()
    for skew in (rs.WINDOW_SECONDS + 1, -(rs.WINDOW_SECONDS + 1)):
        header = rs.sign(label, secret, "GET", "/health", now=NOW + skew)
        assert not rs.verify(header, lookup, "GET", "/health", now=NOW, guard=rs.ReplayGuard())
    header = rs.sign(label, secret, "GET", "/health", now=NOW - rs.WINDOW_SECONDS + 5)
    assert rs.verify(header, lookup, "GET", "/health", now=NOW, guard=rs.ReplayGuard())


def test_an_invalid_signature_does_not_spend_the_nonce():
    """Otherwise a stranger could burn the page's nonces by replaying them with junk."""
    _, label, secret, lookup = device()
    guard = rs.ReplayGuard()
    good = rs.sign(label, secret, "GET", "/health", now=NOW)
    forged = " ".join(good.split(" ")[:4] + ["A" * 43])
    assert not rs.verify(forged, lookup, "GET", "/health", now=NOW, guard=guard)
    assert rs.verify(good, lookup, "GET", "/health", now=NOW, guard=guard)


def test_junk_never_raises():
    _, _, _, lookup = device()
    for junk in ("", "v1", "v2 x 1 abcdefghijklmnop " + "A" * 43, "v1 x y z w", None,
                 "v1  " * 5, 42):
        assert not rs.verify(junk, lookup, "GET", "/health", now=NOW, guard=rs.ReplayGuard())


def test_the_replay_guard_stays_bounded():
    guard = rs.ReplayGuard(limit=10)
    for i in range(100):
        assert guard.first_use(f"nonce-{i:012d}", NOW)
    assert len(guard._seen) <= 10


def test_the_asgi_form_reads_method_path_and_header():
    _, label, secret, lookup = device()
    header = rs.sign(label, secret, "POST", "/reset-config")
    scope = {"method": "POST", "path": "/reset-config",
             "headers": [(b"x-browserplug-auth", header.encode("latin-1"))]}
    assert rs.verify_scope(scope, lookup)
    assert not rs.verify_scope({"method": "POST", "path": "/reset-config", "headers": []}, lookup)


def test_non_ascii_digits_in_the_timestamp_are_rejected_not_raised():
    # str.isdigit() is True for superscripts and Arabic-Indic digits, and int() then
    # raises inside the HTTP wrapper: a remote peer got a 500 and a traceback.
    _, label, _, lookup = device()
    nonce, mac = "a" * 16, "b" * 43
    for ts in ("²³", "١٢٣", "1800000000²"):
        header = f"v1 {label} {ts} {nonce} {mac}"
        assert rs.verify(header, lookup, "GET", "/health", now=NOW) is False
