"""server/web/modern_identity.py: the key a 2026-07-28 request is filed under.

Pure: headers in, a key out. What the key does once HttpEndpoints binds it is in
test_modern_http.py.
"""

import hashlib
import re
import uuid

import pytest

import test_imports  # noqa: F401  (path setup)
from browserplug_shared import mcp_era
from server.web import modern_identity as ident

PEER = ("127.0.0.1", 54371)
CONNECTION_ID = uuid.uuid4().hex
ID_HEADER = mcp_era.CONNECTION_ID_HEADER.lower()
SHAPE = re.compile(r"[0-9a-f]{32}")


# --- where the key comes from -----------------------------------------------------------

def test_a_well_formed_connection_id_is_the_key():
    assert ident.connection_key({ID_HEADER: CONNECTION_ID}, PEER) == CONNECTION_ID


def test_surrounding_space_on_the_connection_id_is_tolerated():
    assert ident.connection_key({ID_HEADER: f"  {CONNECTION_ID} "}, PEER) == CONNECTION_ID


@pytest.mark.parametrize("bad", ["", "abc", CONNECTION_ID.upper(), CONNECTION_ID + "0",
                                 CONNECTION_ID[:-1] + "g", "not-an-id"])
def test_a_malformed_connection_id_falls_back_to_the_peer(bad):
    assert ident.connection_key({ID_HEADER: bad}, PEER) == ident.peer_key(PEER)


def test_with_no_connection_id_the_key_is_a_hash_of_the_peer():
    expected = hashlib.sha1(b"127.0.0.1:54371").hexdigest()[:32]
    assert ident.connection_key({}, PEER) == expected
    assert ident.peer_key(PEER) == expected


def test_a_session_header_means_no_synthetic_key():
    """A request that names its session has an identity already, and the connection id
    sent beside it does not replace it."""
    assert ident.connection_key({"mcp-session-id": "abc", ID_HEADER: CONNECTION_ID}, PEER) is None
    assert ident.connection_key({"mcp-session-id": "abc"}, PEER) is None


def test_an_empty_session_header_names_no_session():
    assert ident.connection_key({"mcp-session-id": ""}, PEER) == ident.peer_key(PEER)


@pytest.mark.parametrize("peer", [None, (), ("127.0.0.1",), (None, 5), ("127.0.0.1", None)])
def test_nothing_to_derive_from_gives_no_key(peer):
    assert ident.peer_key(peer) is None
    assert ident.connection_key({}, peer) is None


def test_a_connection_id_needs_no_peer():
    assert ident.connection_key({ID_HEADER: CONNECTION_ID}, None) == CONNECTION_ID


def test_the_connection_id_header_is_looked_up_in_lowercase():
    """HttpEndpoints hands over request_context's lowercased map."""
    assert ident.connection_key({"x-browserplug-connection-id": CONNECTION_ID}, None) \
        == CONNECTION_ID


# --- the shape of the key ---------------------------------------------------------------

def test_a_peer_key_has_the_shape_of_a_session_id():
    assert SHAPE.fullmatch(ident.peer_key(PEER))
    assert SHAPE.fullmatch(ident.peer_key(("::1", 80)))
    assert SHAPE.fullmatch(ident.peer_key(["10.0.0.7", 49152]))


def test_one_peer_always_gets_the_same_key_and_another_gets_another():
    assert ident.peer_key(PEER) == ident.peer_key(("127.0.0.1", 54371))
    assert ident.peer_key(PEER) != ident.peer_key(("127.0.0.1", 54372))
    assert ident.peer_key(PEER) != ident.peer_key(("127.0.0.2", 54371))


def test_the_first_eight_characters_tell_connections_apart():
    """CallTelemetry files a call under (session[:8], request id): two live connections
    must not share their first eight characters. A fixed prefix would give every client the
    same eight."""
    keys = [ident.peer_key(("127.0.0.1", port)) for port in range(40000, 42000)]
    assert len({key[:8] for key in keys}) == len(keys)
    # Fixed ids, so the check cannot flake on a chance collision of random ones.
    ids = [hashlib.sha1(str(n).encode()).hexdigest()[:32] for n in range(500)]
    assert len({ident.connection_key({ID_HEADER: i}, None)[:8] for i in ids}) == len(ids)


def test_the_key_is_bound_under_the_name_the_tool_layer_reads():
    from server.web import request_context
    assert ident.SESSION_HEADER == "mcp-session-id"
    with request_context.bound({ident.SESSION_HEADER: "k"}):
        assert request_context.header("mcp-session-id") == "k"
