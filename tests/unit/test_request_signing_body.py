"""Review finding S7: the HTTP signature covers the body, and nonces survive a restart.

Two holes. A v1 signature covered method and path only, so a man in the middle could keep
the header and swap the body of a signed /ws-bind (the user clicks Local, the server gets
0.0.0.0). And the nonce memory lived in the process, so a captured request replayed once
per restart for the 300 s window.
"""

import importlib
import json
import os

import pytest

from secure_ext import device_token
from browserplug_shared import config_store, device_tokens, request_signing
from browserplug_shared import request_signing as rs
from test_ws_token import bind_app, http_post, make_bootstrap_server

NOW = 1_800_000_000
LAN_PEER = ("192.168.30.171", 44444)
LOCAL_BODY = json.dumps({"host": "localhost"}).encode()
OPEN_BODY = json.dumps({"host": "0.0.0.0"}).encode()


def device():
    import secrets
    master = secrets.token_urlsafe(32)
    label, secret = device_tokens.parse(device_tokens.issue(master))
    return master, label, secret, (lambda lab: device_tokens.secret_for(master, lab))


def _hdr(value):
    return [(rs.AUTH_HEADER.encode("latin-1"), value.encode("latin-1"))]


def _v1_headers(server, method, path):
    """A header in the pre-body form, built without the `body=` argument on purpose."""
    label, secret = device_tokens.parse(device_token(server))
    return _hdr(rs.sign(label, secret, method, path))


# --- (a) the body ------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_signed_ws_bind_body_cannot_be_swapped():
    """The attack: the header is the user's, the body is the attacker's."""
    server = make_bootstrap_server()
    app = bind_app(server)
    headers = _v1_headers(server, "POST", "/ws-bind")   # what the Local click signed
    status, out = await http_post(app, "/ws-bind", body=OPEN_BODY, headers=headers,
                                  client=LAN_PEER)
    assert status >= 400, out
    assert server.ws_host == "localhost", "the swapped body must never be applied"


@pytest.mark.asyncio
async def test_a_v2_signature_binds_the_body_it_was_made_for():
    server = make_bootstrap_server()
    app = bind_app(server)
    label, secret = device_tokens.parse(device_token(server))
    sig = rs.sign(label, secret, "POST", "/ws-bind", body=LOCAL_BODY)
    status, out = await http_post(app, "/ws-bind", body=OPEN_BODY, headers=_hdr(sig),
                                  client=LAN_PEER)
    assert status == 403 and b"body" in out, (status, out)
    assert server.ws_host == "localhost"


@pytest.mark.asyncio
async def test_a_v2_signature_with_its_own_body_is_accepted():
    server = make_bootstrap_server()
    app = bind_app(server)
    label, secret = device_tokens.parse(device_token(server))
    sig = rs.sign(label, secret, "POST", "/ws-bind", body=OPEN_BODY)
    status, out = await http_post(app, "/ws-bind", body=OPEN_BODY, headers=_hdr(sig),
                                  client=LAN_PEER)
    assert status == 200, out
    assert server.ws_host == "0.0.0.0"


@pytest.mark.asyncio
async def test_a_v1_signature_with_a_body_is_told_to_update():
    server = make_bootstrap_server()
    status, out = await http_post(bind_app(server), "/ws-bind", body=LOCAL_BODY,
                                  headers=_v1_headers(server, "POST", "/ws-bind"),
                                  client=LAN_PEER)
    assert status == 403
    assert b"update the BrowserPlug extension" in out, out


@pytest.mark.asyncio
async def test_a_v1_signature_without_a_body_still_works():
    server = make_bootstrap_server()
    status, out = await http_post(bind_app(server), "/reset-config", body=b"",
                                  headers=_v1_headers(server, "POST", "/reset-config"))
    assert status != 403, out


def test_the_body_hash_is_part_of_the_mac():
    _, label, secret, lookup = device()
    good = rs.sign(label, secret, "POST", "/ws-bind", now=NOW, body=LOCAL_BODY)
    parts = good.split(" ")
    parts[4] = rs.body_hash(OPEN_BODY)      # keep the mac, claim another body
    assert not rs.verify(" ".join(parts), lookup, "POST", "/ws-bind", now=NOW,
                         guard=rs.ReplayGuard())


def test_a_v2_header_does_not_verify_as_v1_or_the_other_way_round():
    _, label, secret, lookup = device()
    v2 = rs.sign(label, secret, "POST", "/x", now=NOW, body=b"")
    downgraded = " ".join(["v1"] + v2.split(" ")[1:4] + [v2.split(" ")[5]])
    assert not rs.verify(downgraded, lookup, "POST", "/x", now=NOW, guard=rs.ReplayGuard())


def test_body_problem_states_each_case():
    sig_v1 = rs.Signature(1, None)
    assert rs.body_problem(sig_v1, b"") is None
    assert rs.body_problem(sig_v1, b"x") == rs.UPDATE_MESSAGE
    sig_v2 = rs.Signature(2, rs.body_hash(b"abc"))
    assert rs.body_problem(sig_v2, b"abc") is None
    assert rs.body_problem(sig_v2, b"abd") == rs.BODY_MISMATCH_MESSAGE
    assert rs.body_problem(None, b"anything") is None


# --- (b) replay across a restart ---------------------------------------------------------

def test_a_replay_guard_with_a_file_remembers_nonces_across_instances(tmp_path):
    path = str(tmp_path / "nonces")
    nonce = "n" * 22
    assert rs.ReplayGuard(path=path).first_use(nonce, rs.time.time())
    assert not rs.ReplayGuard(path=path).first_use(nonce, rs.time.time()), \
        "a restarted server must still refuse a captured request"


def test_old_nonces_and_torn_lines_are_dropped_on_load(tmp_path):
    path = tmp_path / "nonces"
    now = rs.time.time()
    path.write_text(f"{now - 3 * rs.WINDOW_SECONDS:.3f} {'o' * 22}\n"
                    f"{now:.3f} {'f' * 22}\n"
                    f"{now:.3f} ", encoding="ascii")
    guard = rs.ReplayGuard(path=str(path))
    assert guard.first_use("o" * 22, now), "an expired nonce is forgotten"
    assert not guard.first_use("f" * 22, now)


def test_an_unwritable_nonce_file_degrades_to_memory(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    guard = rs.ReplayGuard(path=str(blocker / "nonces"))   # a directory cannot be made here
    assert guard.first_use("m" * 22, rs.time.time())
    assert not guard.first_use("m" * 22, rs.time.time())


def test_the_default_guard_survives_a_process_restart():
    """A captured request replayed after the server restarts. importlib.reload drops the
    module's in-memory guard exactly as a new process would."""
    _, label, secret, lookup = device()
    header = rs.sign(label, secret, "GET", "/health", now=rs.time.time())
    assert request_signing.verify(header, lookup, "GET", "/health")
    reloaded = importlib.reload(request_signing)
    assert not reloaded.verify(header, lookup, "GET", "/health"), \
        "replayed after a restart within the window"
    assert os.path.dirname(reloaded._default_guard()._path) == config_store.state_dir()
