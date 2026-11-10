"""Port overlap on the BSD socket stack (macOS).

Linux and Windows refuse a bind that overlaps a live listener's address. BSD does not
when the new socket sets SO_REUSEADDR, which asyncio and uvicorn do: a bind to 127.0.0.1
sits beside a listener on 0.0.0.0, and the reverse. Measured on macOS 27 (ports.md).
So three things must hold there, and each is pinned below:

  * the bind probe calls such a port busy (`bsd_bind.classify`), yet a port that only
    holds a TIME_WAIT leftover stays free, or a restarted server would relocate;
  * the WebSocket server refuses to start beside an overlapping listener and holds the
    loopback addresses itself when it binds a wildcard;
  * the test port allocator never hands out a port a wildcard or dual-stack listener holds.

The first group runs with a scripted `classify` so Windows exercises the routing; the
real-socket groups run wherever the kernel can show the behaviour (macOS for the hole).
"""
import asyncio
import errno
import os
import socket
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

import port_coordinator
from browserplug_shared import bsd_bind, mcp_port as mp
from server.runtime import lifecycle
from server.server import BrowserPlugServer

IS_WINDOWS = os.name == "nt"
ON_BSD = sys.platform == "darwin"


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _hold(host, port, reuse=False, family=socket.AF_INET, v6only=None):
    sock = socket.socket(family, socket.SOCK_STREAM)
    if reuse:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if v6only is not None:
        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, v6only)
    sock.bind((host, port))
    sock.listen(5)
    return sock


# ------------------------------------------------ routing, scripted (runs everywhere)

class _Scripted:
    """Stands in for bsd_bind._bind / _answers: strict and reuse binds answer from a script."""

    def __init__(self, strict, reuse, answers):
        self.strict, self.reuse, self.answers = strict, reuse, answers
        self.connects = 0

    def bind(self, family, addr, port, reuse):
        outcome = self.reuse if reuse else self.strict
        if outcome is not None:
            raise OSError(outcome, "scripted")

    def connect(self, family, addr, port):
        self.connects += 1
        return self.answers


def _script(monkeypatch, strict=None, reuse=None, answers=False):
    s = _Scripted(strict, reuse, answers)
    monkeypatch.setattr(bsd_bind, "_bind", s.bind)
    monkeypatch.setattr(bsd_bind, "_answers", s.connect)
    return s


def test_a_free_port_is_free_and_nothing_connects(monkeypatch):
    s = _script(monkeypatch)
    assert bsd_bind.classify(socket.AF_INET, "127.0.0.1", 1) == bsd_bind.FREE
    assert s.connects == 0


def test_an_overlapping_listener_is_told_from_a_time_wait_leftover(monkeypatch):
    """Strict bind fails, reuse bind succeeds: only a connect separates the two."""
    s = _script(monkeypatch, strict=errno.EADDRINUSE, answers=True)
    assert bsd_bind.classify(socket.AF_INET, "127.0.0.1", 1) == bsd_bind.OVERLAP
    s = _script(monkeypatch, strict=errno.EADDRINUSE, answers=False)
    assert bsd_bind.classify(socket.AF_INET, "127.0.0.1", 1) == bsd_bind.STALE
    assert s.connects == 1


def test_an_identical_address_is_the_real_binds_error(monkeypatch):
    _script(monkeypatch, strict=errno.EADDRINUSE, reuse=errno.EADDRINUSE)
    with pytest.raises(OSError) as e:
        bsd_bind.classify(socket.AF_INET, "127.0.0.1", 1)
    assert e.value.errno == errno.EADDRINUSE


def test_a_strict_bind_error_that_is_not_busy_propagates_without_a_reuse_try(monkeypatch):
    s = _script(monkeypatch, strict=errno.EACCES)
    with pytest.raises(OSError) as e:
        bsd_bind.classify(socket.AF_INET, "127.0.0.1", 1)
    assert e.value.errno == errno.EACCES and s.connects == 0


@pytest.mark.parametrize("verdict, bindable", [(bsd_bind.FREE, True), (bsd_bind.STALE, True),
                                               (bsd_bind.OVERLAP, False)])
def test_the_probe_follows_the_bsd_verdict_where_it_applies(monkeypatch, verdict, bindable):
    monkeypatch.setattr(bsd_bind, "applies", lambda: True)
    monkeypatch.setattr(bsd_bind, "classify", lambda family, addr, port: verdict)
    assert mp.port_is_bindable(1, "127.0.0.1") is bindable
    assert mp.overlapping_listener(1, "127.0.0.1") is (verdict == bsd_bind.OVERLAP)


def test_the_probe_reports_an_identical_address_busy_on_bsd(monkeypatch):
    def classify(family, addr, port):
        raise OSError(errno.EADDRINUSE, "held")

    monkeypatch.setattr(bsd_bind, "applies", lambda: True)
    monkeypatch.setattr(bsd_bind, "classify", classify)
    assert mp.port_is_bindable(1, "127.0.0.1") is False
    assert mp.overlapping_listener(1, "127.0.0.1") is False  # the real bind reports it


def test_no_other_platform_touches_the_bsd_path(monkeypatch):
    """Linux and Windows keep their own probes byte for byte: classify is never reached."""
    def boom(*a, **k):
        raise AssertionError("bsd_bind reached on a platform it does not apply to")

    monkeypatch.setattr(bsd_bind, "applies", lambda: False)
    monkeypatch.setattr(bsd_bind, "classify", boom)
    port = _free_port()
    assert mp.port_is_bindable(port, "127.0.0.1") is True
    assert mp.overlapping_listener(port, "127.0.0.1") is False


def test_applies_only_on_macos(monkeypatch):
    for platform, expected in (("darwin", True), ("linux", False), ("win32", False)):
        monkeypatch.setattr(sys, "platform", platform)
        assert bsd_bind.applies() is expected


def test_a_wildcard_bind_also_holds_its_loopback_siblings():
    both = bsd_bind.with_loopback_siblings(["0.0.0.0", "::"])
    assert both == ["0.0.0.0", "::", "127.0.0.1", "::1"]
    assert bsd_bind.with_loopback_siblings(["0.0.0.0"]) == ["0.0.0.0", "127.0.0.1"]
    assert bsd_bind.with_loopback_siblings(["::", "0.0.0.0", "127.0.0.1"]) == [
        "::", "0.0.0.0", "127.0.0.1", "::1"]
    for untouched in ("localhost", "192.168.1.5", ["127.0.0.1"]):
        assert bsd_bind.with_loopback_siblings(untouched) is untouched


# ------------------------------------------------- the WebSocket bind (lifecycle glue)

def test_ws_bind_list_is_untouched_off_macos(monkeypatch):
    monkeypatch.setattr(bsd_bind, "applies", lambda: False)
    addresses = ["0.0.0.0", "::"]
    assert lifecycle.ws_addresses_for_bind(addresses, 1) is addresses


def test_ws_bind_refuses_to_start_beside_an_overlapping_listener(monkeypatch):
    monkeypatch.setattr(bsd_bind, "applies", lambda: True)
    monkeypatch.setattr(lifecycle, "overlapping_listener",
                        lambda port, host: host == "127.0.0.1")
    with pytest.raises(OSError) as e:
        lifecycle.ws_addresses_for_bind("127.0.0.1", 8772)
    assert e.value.errno == errno.EADDRINUSE and "8772" in str(e.value)


def test_start_server_gives_websockets_the_vetted_list(monkeypatch, tmp_path):
    """The glue: a wildcard bind reaches websockets.serve with the loopback siblings added,
    and an overlapping listener stops the start before anything binds."""
    monkeypatch.setenv("BROWSERPLUG_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("BROWSERPLUG_WS_HOST", raising=False)
    seen = []

    async def fake_serve(handler, host, port, **kwargs):
        seen.append(host)

        class _Srv:
            async def wait_closed(self):
                return None

        return _Srv()

    monkeypatch.setattr(lifecycle.websockets, "serve", fake_serve)
    server = BrowserPlugServer(port=_free_port(), mcp_port=_free_port(), start_mcp=False,
                               ws_host="0.0.0.0", ws_token="t" * 32)
    monkeypatch.setattr(bsd_bind, "applies", lambda: True)
    monkeypatch.setattr(lifecycle, "overlapping_listener", lambda port, host: False)
    asyncio.run(server.start_server())
    assert seen == [["0.0.0.0", "::", "127.0.0.1", "::1"]]

    seen.clear()
    monkeypatch.setattr(lifecycle, "overlapping_listener", lambda port, host: True)
    with pytest.raises(OSError):
        asyncio.run(server.start_server())
    assert seen == [], "the server bound its port beside a live overlapping listener"


# ----------------------------------------- real sockets: what the kernel says (non-Windows)

# Windows lets a specific bind sit under a wildcard listener when SO_EXCLUSIVEADDRUSE is
# used (ports.md), so these premises belong to the POSIX kernels.
posix_only = pytest.mark.skipif(IS_WINDOWS, reason="POSIX bind semantics")


@posix_only
@pytest.mark.parametrize("holder_reuse", [False, True])
@pytest.mark.parametrize("holder, probe", [("0.0.0.0", "127.0.0.1"), ("0.0.0.0", "localhost"),
                                           ("127.0.0.1", "0.0.0.0"), ("127.0.0.1", "127.0.0.1"),
                                           ("0.0.0.0", "0.0.0.0")])
def test_a_live_listener_makes_an_overlapping_port_busy(holder, probe, holder_reuse):
    port = _free_port()
    sock = _hold(holder, port, reuse=holder_reuse)
    try:
        assert mp.port_is_bindable(port, probe) is False
        assert mp.port_is_occupied(port, "localhost") is True
    finally:
        sock.close()


@posix_only
@pytest.mark.skipif(not socket.has_ipv6, reason="no IPv6 stack")
def test_a_dual_stack_wildcard_listener_makes_the_ipv4_loopback_busy():
    port = _free_port()
    try:
        sock = _hold("::", port, reuse=True, family=socket.AF_INET6, v6only=0)
    except OSError:
        pytest.skip("cannot bind :: on this machine")
    try:
        assert mp.port_is_bindable(port, "127.0.0.1") is False
    finally:
        sock.close()


@posix_only
def test_a_time_wait_leftover_does_not_make_a_port_busy():
    """A restarted server finds its own closed connections in TIME_WAIT. Calling that busy
    would relocate it for nothing, and a reuse bind (what uvicorn does) takes it fine."""
    port = _free_port()
    sock = _hold("127.0.0.1", port, reuse=True)
    client = socket.create_connection(("127.0.0.1", port))
    served, _ = sock.accept()
    served.close()  # the listener's side closes first, so the leftover lives on its port
    time.sleep(0.2)
    client.recv(1)
    client.close()
    sock.close()
    assert mp.port_is_bindable(port, "127.0.0.1") is True
    assert mp.port_is_occupied(port, "localhost") is False
    again = _hold("127.0.0.1", port, reuse=True)  # the real bind goes through
    again.close()


@posix_only
@pytest.mark.parametrize("holder, family, v6only", [("0.0.0.0", socket.AF_INET, None),
                                                    ("127.0.0.1", socket.AF_INET, None),
                                                    ("::1", socket.AF_INET6, 1),
                                                    ("::", socket.AF_INET6, 0)])
@pytest.mark.parametrize("holder_reuse", [False, True])
def test_the_test_port_allocator_never_offers_a_held_port(holder, family, v6only, holder_reuse):
    if family == socket.AF_INET6 and not socket.has_ipv6:
        pytest.skip("no IPv6 stack")
    port = _free_port()
    try:
        sock = _hold(holder, port, reuse=holder_reuse, family=family, v6only=v6only)
    except OSError:
        pytest.skip(f"cannot bind {holder} on this machine")
    try:
        assert port_coordinator.is_port_free(port) is False
    finally:
        sock.close()
    assert port_coordinator.is_port_free(port) is True


# ------------------------------------------- real sockets: the BSD hole itself (macOS only)

on_bsd = pytest.mark.skipif(not ON_BSD, reason="the SO_REUSEADDR overlap is BSD behaviour")


@on_bsd
def test_the_hole_is_real_here():
    """The premise: a reuse bind sits beside a different-spec listener, and a strict one
    does not. If this fails, the platform changed and the BSD branch needs a second look."""
    port = _free_port()
    sock = _hold("0.0.0.0", port)
    try:
        beside = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        beside.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        beside.bind(("127.0.0.1", port))
        beside.close()
        assert bsd_bind.classify(socket.AF_INET, "127.0.0.1", port) == bsd_bind.OVERLAP
        assert mp.overlapping_listener(port, "127.0.0.1") is True
    finally:
        sock.close()


@on_bsd
def test_ws_bind_refuses_a_real_overlapping_listener():
    port = _free_port()
    sock = _hold("0.0.0.0", port)
    try:
        for address in ("127.0.0.1", "localhost", ["127.0.0.1"]):
            with pytest.raises(OSError) as e:
                lifecycle.ws_addresses_for_bind(address, port)
            assert e.value.errno == errno.EADDRINUSE
    finally:
        sock.close()


@on_bsd
def test_holding_the_loopback_siblings_leaves_a_squatter_nothing_to_bind():
    """Our wildcard listener alone can be shadowed by a later reuse bind on 127.0.0.1 that
    then takes every local connection. With the siblings held, the squatter's bind fails."""
    port = _free_port()

    async def scenario():
        async def handler(reader, writer):
            writer.close()

        addresses = lifecycle.ws_addresses_for_bind(["0.0.0.0", "::"] if socket.has_ipv6
                                                    else ["0.0.0.0"], port)
        server = await asyncio.start_server(handler, addresses, port)
        try:
            for family, host in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
                if family == socket.AF_INET6 and not socket.has_ipv6:
                    continue
                squatter = socket.socket(family, socket.SOCK_STREAM)
                squatter.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    with pytest.raises(OSError) as e:
                        squatter.bind((host, port))
                    assert e.value.errno == errno.EADDRINUSE
                finally:
                    squatter.close()
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())
