#!/usr/bin/env python3
"""
Regression tests for dynamic port allocation (port_coordinator) and for the
port-independence of the Firefox profile cache.

The bug these guard against: every port type was in fact FIXED (websocket=40000,
mcp=40200, test_individual=40400, test_mcp_individual=40600) and the availability
probe bound with SO_REUSEADDR — which on Windows SUCCEEDS on a port another process
is actively listening on when that listener also set SO_REUSEADDR, as uvicorn (our
MCP HTTP server) does. So two concurrent pytest sessions silently shared ports and a
test could talk to the OTHER session's server: a ws-token test fetched its secret
from the wrong server's /ws-token endpoint.

No Firefox required.
"""

import random
import socket

import pytest

import firefox_test_utils
import port_coordinator as pc
from port_coordinator import (
    PORT_POOL_END,
    PORT_POOL_START,
    PORT_TYPES,
    allocate_port,
    get_port_by_type,
    is_port_free,
    release_port,
)
from firefox_test_utils import FirefoxTestManager

# The ports the old scheme handed to EVERY session, forever.
LEGACY_FIXED_PORTS = (40000, 40200, 40400, 40600)


@pytest.fixture
def clean_allocator():
    """Run against a pristine allocator, and leave the real session's state untouched."""
    saved = (set(pc._ALLOCATED), dict(pc._TYPE_PORTS), dict(pc._RESERVED_FILES))
    pc._ALLOCATED.clear()
    pc._TYPE_PORTS.clear()
    pc._RESERVED_FILES.clear()
    try:
        yield
    finally:
        for port in list(pc._RESERVED_FILES):
            pc._unclaim(port)
        pc._ALLOCATED.clear()
        pc._ALLOCATED.update(saved[0])
        pc._TYPE_PORTS.clear()
        pc._TYPE_PORTS.update(saved[1])
        pc._RESERVED_FILES.clear()
        pc._RESERVED_FILES.update(saved[2])


class _ForcedRandom:
    """Proposes the given ports first, then behaves like a normal RNG."""

    def __init__(self, forced):
        self.forced = list(forced)
        self._real = random.Random(20260713)

    def randint(self, a, b):
        if self.forced:
            return self.forced.pop(0)
        return self._real.randint(a, b)


def _listener(port, reuse_addr):
    """Occupy `port` exactly the way a real server does.

    reuse_addr=True is the uvicorn case (Config.bind_socket sets SO_REUSEADDR) — the one
    the old SO_REUSEADDR probe could bind straight over.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if reuse_addr:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(('127.0.0.1', port))
    sock.listen(5)
    return sock


def _free_pool_port():
    """A port in the pool that nothing holds (allocated, then given straight back)."""
    port = allocate_port()
    release_port(port)
    return port


# --- allocation ------------------------------------------------------------------

def test_two_allocations_never_collide(clean_allocator):
    ports = [allocate_port() for _ in range(25)]
    assert len(set(ports)) == len(ports), f"allocator handed out a duplicate: {ports}"
    assert all(PORT_POOL_START <= p <= PORT_POOL_END for p in ports)


def test_named_types_get_distinct_ports_and_stay_stable(clean_allocator):
    first = {t: get_port_by_type(t) for t in PORT_TYPES}
    assert len(set(first.values())) == len(PORT_TYPES), f"types share a port: {first}"

    # A role is a stable name for a port within one session — callers all over the suite
    # ask for the same role and must agree on the answer.
    again = {t: get_port_by_type(t) for t in PORT_TYPES}
    assert again == first


def test_ports_are_not_the_legacy_fixed_ones(clean_allocator):
    """'dynamic' must actually BE dynamic: nothing may be pinned to the old constants."""
    allocated = {get_port_by_type(t) for t in PORT_TYPES}
    assert not allocated & set(LEGACY_FIXED_PORTS), (
        f"port types are still fixed: {allocated}")


@pytest.mark.parametrize('reuse_addr', [True, False])
def test_occupied_port_is_never_handed_out(clean_allocator, monkeypatch, reuse_addr):
    """THE bug. With the old SO_REUSEADDR probe, the reuse_addr=True case hands out a
    port that has a LIVE listener on it (Windows lets a SO_REUSEADDR bind win over
    another SO_REUSEADDR listener), so a test would connect to somebody else's server.
    """
    occupied = _free_pool_port()
    sock = _listener(occupied, reuse_addr)
    try:
        assert is_port_free(occupied) is False, (
            f"probe says port {occupied} is free while a listener is bound to it")

        # Force the allocator to consider exactly that port first.
        monkeypatch.setattr(pc, '_RANDOM', _ForcedRandom([occupied] * 5))
        for _ in range(5):
            port = allocate_port()
            assert port != occupied, "allocator handed out an occupied port"
            assert is_port_free(port), f"allocator handed out unusable port {port}"
            release_port(port)
    finally:
        sock.close()


def test_allocation_works_when_the_legacy_fixed_ports_are_all_occupied(clean_allocator,
                                                                       monkeypatch):
    """The old scheme had exactly four ports; a second session was simply stuck."""
    sockets = []
    try:
        for port in LEGACY_FIXED_PORTS:
            try:
                sockets.append(_listener(port, reuse_addr=True))
            except OSError:
                pytest.skip(f"cannot occupy legacy port {port} (already in use)")

        monkeypatch.setattr(pc, '_RANDOM', _ForcedRandom(list(LEGACY_FIXED_PORTS) * 2))
        ports = {t: get_port_by_type(t) for t in PORT_TYPES}

        assert not set(ports.values()) & set(LEGACY_FIXED_PORTS)
        assert len(set(ports.values())) == len(PORT_TYPES)
    finally:
        for sock in sockets:
            sock.close()


def test_a_second_session_cannot_be_handed_this_session_s_port(clean_allocator,
                                                               monkeypatch):
    """The cross-process claim: the probe only sees ports already BOUND, so a port that
    another session has reserved but not yet bound must still be off limits.
    """
    mine = allocate_port()          # reserved by "session 1", nothing listening yet
    assert is_port_free(mine), "precondition: the port is not bound, only claimed"

    # A second session: same reservation dir, its own (empty) in-process registry.
    pc._ALLOCATED.clear()
    pc._TYPE_PORTS.clear()
    monkeypatch.setattr(pc, '_RANDOM', _ForcedRandom([mine] * 5))

    theirs = allocate_port()
    assert theirs != mine, "a second session was handed a port this session reserved"


def test_release_frees_the_port_for_reuse(clean_allocator, monkeypatch):
    port = allocate_port()
    release_port(port)
    assert port not in pc._ALLOCATED

    monkeypatch.setattr(pc, '_RANDOM', _ForcedRandom([port]))
    assert allocate_port() == port, "a released port must be allocatable again"


def test_unknown_port_type_is_rejected(clean_allocator):
    with pytest.raises(ValueError):
        get_port_by_type('not_a_port_type')


# --- profile cache key -----------------------------------------------------------

@pytest.fixture
def xpi(tmp_path, monkeypatch):
    """A stand-in extension XPI whose content the cache key must follow."""
    path = tmp_path / 'browserplug@marcinwolny.net.xpi'
    path.write_bytes(b'PK\x03\x04 extension v1')
    monkeypatch.setattr(firefox_test_utils, '_get_extension_xpi_path', lambda: str(path))
    FirefoxTestManager._fingerprints.clear()
    return path


def test_cache_key_ignores_the_port(xpi):
    """The whole point: a cached profile must be reusable by a session on any port,
    otherwise dynamic ports would rebuild the profile (~9-21s) on every single run.
    """
    a = FirefoxTestManager(test_port=40400).cache_key
    b = FirefoxTestManager(test_port=45123).cache_key
    assert a == b, "cache key still depends on the port"


def test_cache_key_changes_when_the_xpi_changes(xpi):
    before = FirefoxTestManager(test_port=40400).cache_key

    xpi.write_bytes(b'PK\x03\x04 extension v2 - different content')
    FirefoxTestManager._fingerprints.clear()
    after = FirefoxTestManager(test_port=40400).cache_key

    assert before != after, "a rebuilt extension must invalidate the cached profile"


def test_cache_key_changes_when_granted_permissions_change(xpi, monkeypatch):
    before = FirefoxTestManager(test_port=40400).cache_key

    monkeypatch.setattr(FirefoxTestManager, '_GRANTED_OPTIONAL_PERMISSIONS',
                        ['proxy', 'bookmarks'])
    FirefoxTestManager._fingerprints.clear()
    after = FirefoxTestManager(test_port=40400).cache_key

    assert before != after, (
        "adding an optional permission must invalidate cached profiles — they were "
        "built without the grant, and the matching tests would silently skip")


def test_tagged_profiles_keep_their_own_cache_slot(xpi):
    """The second Firefox of a multi-browser test needs its OWN profile (=> its own
    extension instance_id), even on the same port and the same XPI.
    """
    default = FirefoxTestManager(test_port=40400).cache_key
    tagged = FirefoxTestManager(test_port=40400, profile_tag='b').cache_key
    assert default != tagged
    assert tagged.endswith('-b')


def test_cache_key_is_a_filename(xpi):
    key = FirefoxTestManager(test_port=40400).cache_key
    assert key and all(c.isalnum() or c == '-' for c in key), key
