"""Property-based fuzzing of request signing and device-token parsing.

`verify` takes a header straight off the network, so it must reject anything malformed
without raising, accept exactly what `sign` produced, bind a signature to its method,
path and body, and spend each nonce once. Keys are minted at run time with the project's
own functions; no secret is written in this file.
"""

import string

from hypothesis import assume, given, strategies as st

from browserplug_shared import device_tokens, request_signing as rs
from browserplug_shared.ws_token import generate_token
from fuzz_support import fuzz

NOW = 1_800_000_000
# One master for the module: master_key() runs scrypt (about a third of a second), cached.
MASTER = generate_token()
STRANGER = generate_token()

labels = st.from_regex(device_tokens.LABEL_RE, fullmatch=True)
methods = st.sampled_from(["GET", "POST", "put", "Delete"])
paths = st.text(min_size=1, max_size=60).map(lambda p: "/" + p)
bodies = st.binary(max_size=200)
nonces = st.text(alphabet=string.ascii_letters + string.digits + "_-", min_size=16, max_size=64)


def lookup(label):
    return device_tokens.secret_for(MASTER, label)


def signed(label, method, path, body=None, nonce=None, now=NOW):
    return rs.sign(label, lookup(label), method, path, now=now, nonce=nonce, body=body)


@fuzz()
@given(st.one_of(st.none(), st.text(max_size=300), st.binary(max_size=100), st.integers(),
                 st.lists(st.text(max_size=10))))
def test_verify_never_raises_and_rejects_noise(header):
    guard = rs.ReplayGuard()
    assert rs.verify_detail(header, lookup, "GET", "/health", now=NOW, guard=guard) is None
    assert not rs.verify(header, lookup, "GET", "/health", now=NOW, guard=guard)


_word = st.text(alphabet=string.ascii_letters + string.digits + "_-", min_size=0, max_size=70)


@fuzz()
@given(st.sampled_from(["v1", "v2", "v3", ""]), st.lists(_word, max_size=7), st.text(max_size=5))
def test_header_shaped_noise_is_rejected(version, words, pad):
    """The right word count with arbitrary words: still never a valid signature."""
    header = pad + " ".join([version] + words)
    assert rs.verify(header, lookup, "GET", "/", now=NOW, guard=rs.ReplayGuard()) is False


@fuzz()
@given(labels, methods, paths, st.none() | bodies, nonces)
def test_sign_then_verify_round_trips_once(label, method, path, body, nonce):
    guard = rs.ReplayGuard()
    header = signed(label, method, path, body, nonce)
    sig = rs.verify_detail(header, lookup, method, path, now=NOW, guard=guard)
    assert sig is not None
    assert sig.version == (1 if body is None else 2)
    assert (sig.body_hash is None) == (body is None)
    assert rs.body_problem(sig, body or b"") is None
    # Replay: the same header spends its nonce once.
    assert rs.verify_detail(header, lookup, method, path, now=NOW, guard=guard) is None


@fuzz()
@given(labels, methods, paths, paths, nonces)
def test_a_signature_is_bound_to_method_and_path(label, method, path, other_path, nonce):
    header = signed(label, method, path, b"", nonce)
    assume(other_path != path)
    assert rs.verify(header, lookup, method, other_path, now=NOW, guard=rs.ReplayGuard()) is False
    other_method = "PATCH" if method.upper() != "PATCH" else "GET"
    assert rs.verify(header, lookup, other_method, path, now=NOW, guard=rs.ReplayGuard()) is False


@fuzz()
@given(labels, paths, bodies, bodies)
def test_the_body_that_arrives_must_match_the_body_that_was_signed(label, path, signed_body, arrived):
    sig = rs.verify_detail(signed(label, "POST", path, signed_body), lookup, "POST", path,
                           now=NOW, guard=rs.ReplayGuard())
    assert sig is not None
    problem = rs.body_problem(sig, arrived)
    assert (problem is None) == (arrived == signed_body)
    if problem:
        assert problem == rs.BODY_MISMATCH_MESSAGE


@fuzz()
@given(labels, paths, st.binary(min_size=1, max_size=100))
def test_a_legacy_signature_is_refused_for_a_request_with_a_body(label, path, body):
    sig = rs.verify_detail(signed(label, "POST", path), lookup, "POST", path, now=NOW,
                           guard=rs.ReplayGuard())
    assert sig is not None and sig.body_hash is None
    assert rs.body_problem(sig, body) == rs.UPDATE_MESSAGE
    assert rs.body_problem(sig, b"") is None


@fuzz()
@given(labels, paths, st.none() | bodies, st.data())
def test_one_changed_character_never_verifies(label, path, body, data):
    header = signed(label, "GET", path, body)
    index = data.draw(st.integers(0, len(header) - 1))
    replacement = data.draw(st.characters(blacklist_categories=("Cs",)))
    assume(replacement != header[index])
    mutated = header[:index] + replacement + header[index + 1:]
    # Surrounding whitespace is stripped before parsing, so it is the one edit that may
    # leave the header equal to the original.
    assume(mutated.strip() != header)
    assert rs.verify(mutated, lookup, "GET", path, now=NOW, guard=rs.ReplayGuard()) is False


@fuzz()
@given(labels, paths, st.integers(rs.WINDOW_SECONDS + 1, 10**7), st.booleans())
def test_a_timestamp_outside_the_window_is_refused(label, path, skew, future):
    header = signed(label, "GET", path)
    now = NOW - skew if future else NOW + skew
    assert rs.verify(header, lookup, "GET", path, now=now, guard=rs.ReplayGuard()) is False


@fuzz()
@given(labels, paths)
def test_a_secret_from_another_master_never_verifies(label, path):
    header = rs.sign(label, device_tokens.secret_for(STRANGER, label), "GET", path, now=NOW)
    assert rs.verify(header, lookup, "GET", path, now=NOW, guard=rs.ReplayGuard()) is False
    # And a lookup that knows no such label rejects instead of raising.
    assert rs.verify(header, lambda _label: None, "GET", path, now=NOW, guard=rs.ReplayGuard()) is False


@fuzz()
@given(labels)
def test_device_token_parse_round_trips(label):
    token = device_tokens.issue(MASTER, label)
    assert device_tokens.parse(token) == (label, device_tokens.secret_for(MASTER, label))
    assert device_tokens.parse(f"  {token}\n") == device_tokens.parse(token)


@fuzz()
@given(st.one_of(st.none(), st.integers(), st.binary(max_size=50), st.text(max_size=120)))
def test_device_token_parse_accepts_only_its_own_shape(value):
    parsed = device_tokens.parse(value)
    if parsed is None:
        return
    label, secret = parsed
    assert device_tokens.LABEL_RE.match(label) and len(secret) == 43
    assert f"{device_tokens.PREFIX}.{label}.{secret}" == value.strip()


@fuzz()
@given(st.one_of(st.none(), st.integers(), st.binary(max_size=20), st.text(max_size=60)))
def test_secret_for_rejects_labels_outside_the_grammar(label):
    secret = device_tokens.secret_for(MASTER, label)
    if isinstance(label, str) and device_tokens.LABEL_RE.match(label):
        assert secret is not None and len(secret) == 43
    else:
        assert secret is None
