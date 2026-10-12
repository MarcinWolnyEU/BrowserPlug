"""Port binding, the ascending MCP-port search, and the client-side seek.

Three properties are pinned here, each of which was broken (or absent) before:

1. **The bind probe never lies.** It must NOT use SO_REUSEADDR: on Windows a
   SO_REUSEADDR bind SUCCEEDS on a port a SO_REUSEADDR listener is actively serving,
   so the probe reported occupied ports as free — which handed out live ports and let a
   squatter bind OVER the extension WebSocket (and harvest the token from its hello).

2. **The MCP HTTP port search is deterministic**: 2830, then 2831, 2832 … 2942 —
   ascending, one at a time, no gaps, no randomness, no wrap. Clients rediscover a
   relocated server by walking the same sequence, so any deviation breaks discovery.

3. **The launcher seeks**: it finds a server that moved, refuses to mistake a stranger's
   HTTP server for BrowserPlug, and pins (no seek) when BROWSERPLUG_MCP_PORT is set.
"""
import json
import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from launcher import launcher
from browserplug_shared import mcp_port as mp
from browserplug_shared.mcp_port import (HEALTH_SERVICE, MCP_PORT_DEFAULT, MCP_PORT_ENV, MCP_PORT_LAST,
                                         find_mcp_port, browserplug_health, mcp_port_sequence, pinned_mcp_port,
                                         port_is_bindable, seek_browserplug)
from server.server import BrowserPlugServer

IS_WINDOWS = os.name == "nt"


# --------------------------------------------------------------------------- helpers

def _listener(port, reuse_address=False, host="127.0.0.1"):
    """A real LISTENING socket, optionally with SO_REUSEADDR (what uvicorn/websockets
    set on POSIX — and what server.py used to set on Windows too)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if reuse_address:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.listen(5)
    return sock


# Where the stub servers live. It must NOT overlap the live 2830..2942 range: a stub that
# answers /health with the real service marker inside that range can be adopted by a live
# launcher that is seeking while the real server restarts. The seek functions take
# explicit start/last ports, so the same walk is exercised over a private band.
_TEST_BAND_FIRST = 38830
_TEST_BAND_LAST = 38942
assert not (MCP_PORT_DEFAULT <= _TEST_BAND_LAST and _TEST_BAND_FIRST <= MCP_PORT_LAST)


def _free_band(n, live=False):
    """The first run of `n` consecutive FREE ports inside the private test band.

    The tests below exercise the real search/seek code over a real band, passing it as
    the explicit range. The band sits outside the live range (see _TEST_BAND_FIRST).

    `live=True` picks from the real range instead, for the one test whose subject is the
    server's in-range walk-up (lifecycle only walks the sequence for in-range requests).
    Its listeners never answer /health, so no launcher can mistake them for a server.
    """
    first, last = ((MCP_PORT_DEFAULT, MCP_PORT_LAST) if live
                   else (_TEST_BAND_FIRST, _TEST_BAND_LAST))
    # _seek_skippable, not port_is_bindable: "nothing is listening here" is the property
    # these tests need, and the two differ for a WILDCARD listener. port_is_bindable
    # answers the narrower question it is named for (could I bind this address?) and on
    # Windows that is True for 127.0.0.1 while a server serves 0.0.0.0:port.
    for base in mcp_port_sequence(first, last - n + 1):
        if all(mp._seek_skippable(base + i, "localhost") for i in range(n)):
            return base
    pytest.fail(f"no {n} consecutive free ports in {first}-{last}")


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _StubHTTP(threading.Thread):
    """A tiny HTTP server on a fixed port that serves a canned /health body."""

    def __init__(self, port, payload, host="127.0.0.1"):
        super().__init__(daemon=True)
        body = json.dumps(payload).encode()

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.httpd = HTTPServer((host, port), H)
        self.port = port

    def run(self):
        self.httpd.serve_forever(poll_interval=0.05)

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.join(timeout=5)


@pytest.fixture
def stub_servers():
    """Start canned /health servers; they are all stopped at teardown."""
    started = []

    def start(port, payload, host="127.0.0.1"):
        s = _StubHTTP(port, payload, host=host)
        s.start()
        started.append(s)
        return s

    yield start
    for s in started:
        s.stop()


def _browserplug_payload(port):
    return {"status": "ok", "service": HEALTH_SERVICE, "server_pid": 4242,
            "mcp_port": port, "ws_port": 8772}


# --------------------------------------------------------- 1. the probe never lies

def test_probe_reports_busy_when_a_plain_listener_holds_the_port():
    port = _free_port()
    sock = _listener(port)
    try:
        assert port_is_bindable(port, host="127.0.0.1") is False
    finally:
        sock.close()


def test_probe_reports_busy_when_a_reuseaddr_listener_holds_the_port():
    """THE regression. A SO_REUSEADDR probe (what the constructor used to do) can bind
    straight over a SO_REUSEADDR listener on Windows and answer 'free' — that is how the
    port coordinator handed out occupied ports, and how a squatter could take :8772 from
    under the extension. Revert port_is_bindable to SO_REUSEADDR and this test fails."""
    port = _free_port()
    sock = _listener(port, reuse_address=True)
    try:
        assert port_is_bindable(port, host="127.0.0.1") is False
    finally:
        sock.close()


@pytest.mark.skipif(not IS_WINDOWS, reason="SO_EXCLUSIVEADDRUSE is Windows-only")
def test_windows_probe_is_exclusive_not_reuseaddr():
    """Explicitly assert the Windows semantics the fix depends on:
      * a SO_REUSEADDR bind DOES steal a SO_REUSEADDR listener's port (the hole), and
      * our probe refuses it, because it asks for SO_EXCLUSIVEADDRUSE instead.
    """
    port = _free_port()
    sock = _listener(port, reuse_address=True)
    try:
        thief = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        thief.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            thief.bind(("127.0.0.1", port))  # the hole: this SUCCEEDS on Windows
            stolen = True
        except OSError:
            stolen = False
        finally:
            thief.close()
        assert stolen, "Windows should let a REUSEADDR socket bind over a REUSEADDR listener"
        assert port_is_bindable(port, host="127.0.0.1") is False, \
            "the probe must use SO_EXCLUSIVEADDRUSE, which refuses that steal"
    finally:
        sock.close()


def test_probe_reports_free_for_an_unused_port():
    port = _free_port()
    assert port_is_bindable(port, host="127.0.0.1") is True


@pytest.mark.skipif(not socket.has_ipv6, reason="no IPv6 stack")
def test_probe_sees_an_ipv6_only_listener_on_a_localhost_port():
    """`localhost` resolves to BOTH 127.0.0.1 and ::1, and that is what uvicorn binds. An
    IPv4-only probe would call such a port free and the real bind would then fail — fatally,
    for the standalone server. So the probe must ask both families."""
    port = _free_port()
    sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    try:
        sock.bind(("::1", port))
        sock.listen(5)
    except OSError:
        sock.close()
        pytest.skip("cannot bind ::1 on this machine")
    try:
        assert port_is_bindable(port, host="127.0.0.1") is True, "IPv4 half really is free"
        assert port_is_bindable(port) is False, "but localhost:port as a whole is NOT"
    finally:
        sock.close()


def test_the_seek_never_skips_a_port_held_by_a_wildcard_listener():
    """A listener on 0.0.0.0 serves 127.0.0.1, but does not block a bind to it.

    So the loopback bind probe says "free" for a port that is actively answering, and the
    seek's skip filter walked straight past a live server. Reachable from the options
    page: mcp_bind_host() puts the MCP port wherever the WebSocket is, so LAN or WAN mode
    moves the shared server onto 0.0.0.0. Every launcher then reported "no BrowserPlug
    server found" and spawned a duplicate that died on the busy :8772, while /health on
    127.0.0.1 answered "ok" throughout.

    The skip verdict is asserted on both platforms; the bind quirk underneath it is
    asserted only on Windows, because POSIX refuses the specific-address bind and so never
    had the bug.
    """
    port = _free_port()
    sock = _listener(port, host="0.0.0.0")
    try:
        if IS_WINDOWS:
            assert port_is_bindable(port, host="127.0.0.1") is True, (
                "the premise of this test: Windows lets a specific-address bind succeed "
                "under a wildcard listener, which is why the loopback probe alone lies")
        assert mp._seek_skippable(port, "localhost") is False, (
            "a port with a live wildcard listener must be PROBED, never skipped")
    finally:
        sock.close()


def test_seek_finds_a_browserplug_bound_on_the_wildcard_address(stub_servers, monkeypatch):
    """End to end: the seek must return the server LAN/WAN mode put on 0.0.0.0."""
    monkeypatch.delenv(MCP_PORT_ENV, raising=False)
    base = _free_band(3)
    stub_servers(base + 1, _browserplug_payload(base + 1), host="0.0.0.0")

    port, health = seek_browserplug(base, base + 2, timeout=1.0)
    assert port == base + 1, "the seek skipped a server bound on the wildcard address"
    assert health["mcp_port"] == base + 1


def test_websocket_listener_never_asks_for_reuse_address(monkeypatch):
    """The extension WebSocket is full control of a logged-in browser: SO_REUSEADDR on it
    is what lets another local process bind OVER it and impersonate the server (harvesting
    the token from the extension's hello). start_server must not pass reuse_address."""
    from server.runtime import lifecycle as srv

    seen = {}

    async def fake_serve(handler, host, port, **kwargs):
        seen["host"], seen["port"], seen["kwargs"] = host, port, kwargs

        class _Srv:
            async def wait_closed(self):
                return None

        return _Srv()

    monkeypatch.setattr(srv.websockets, "serve", fake_serve)
    server = BrowserPlugServer(port=_free_port(), mcp_port=_free_port(), start_mcp=False)
    import asyncio
    asyncio.run(server.start_server())

    assert "reuse_address" not in seen["kwargs"], \
        f"websockets.serve must not set SO_REUSEADDR; got {seen['kwargs']}"


# ------------------------------------------- 2. the deterministic ascending search

def test_default_mcp_port_is_2830_and_range_ends_at_2942():
    assert MCP_PORT_DEFAULT == 2830
    assert MCP_PORT_LAST == 2942
    assert list(mcp_port_sequence())[:3] == [2830, 2831, 2832]
    assert list(mcp_port_sequence())[-1] == 2942
    assert len(list(mcp_port_sequence())) == 113


def test_server_and_launcher_agree_on_the_default_port():
    """One module owns the policy — the 3000-vs-3456 split is exactly what this prevents."""
    from server.runtime import lifecycle as srv
    assert srv.MCP_PORT_DEFAULT == launcher.MCP_PORT_DEFAULT == MCP_PORT_DEFAULT == 2830
    assert srv.MCP_PORT_LAST == launcher.MCP_PORT_LAST == MCP_PORT_LAST == 2942


def test_search_is_strictly_ascending_with_no_gaps(monkeypatch):
    """2830..2833 busy -> 2834. Probed in order, every port, none skipped."""
    probed = []
    busy = {2830, 2831, 2832, 2833}

    def fake_bindable(port, host="localhost"):
        probed.append(port)
        return port not in busy

    monkeypatch.setattr(mp, "port_is_bindable", fake_bindable)
    assert mp.find_mcp_port() == 2834
    assert probed == [2830, 2831, 2832, 2833, 2834]


def test_search_walks_past_four_real_listeners_and_takes_the_fifth_port():
    """The ascending search with REAL listeners on REAL in-range ports (no mocking)."""
    base = _free_band(5)
    socks = []
    try:
        for i in range(4):
            socks.append(_listener(base + i, host="localhost"))
        assert find_mcp_port(start_port=base, last_port=base + 4) == base + 4
    finally:
        for s in socks:
            s.close()


def test_search_gives_up_at_2942_with_a_clear_error(monkeypatch):
    """Exhaustion must raise — never wrap around and never hand back a used port."""
    probed = []

    def fake_bindable(port, host="localhost"):
        probed.append(port)
        return False

    monkeypatch.setattr(mp, "port_is_bindable", fake_bindable)
    with pytest.raises(RuntimeError) as exc:
        mp.find_mcp_port()
    msg = str(exc.value)
    assert "2830" in msg and "2942" in msg and MCP_PORT_ENV in msg
    assert probed == list(range(2830, 2943)), "every port once, ascending, then stop"


def test_server_constructor_defaults_to_the_search(monkeypatch):
    """BrowserPlugServer(mcp_port=None) takes the first free port of the standard sequence."""
    from server.runtime import lifecycle as srv
    calls = []

    def fake_find(start=MCP_PORT_DEFAULT, last=MCP_PORT_LAST, host="localhost"):
        calls.append((start, last))
        return 2837

    monkeypatch.setattr(srv, "find_mcp_port", fake_find)
    server = BrowserPlugServer(port=_free_port(), mcp_port=None, start_mcp=False)
    assert server.mcp_port == 2837
    assert calls == [(MCP_PORT_DEFAULT, MCP_PORT_LAST)]


@pytest.mark.parametrize("ws_host, expected", [(None, "localhost"), ("0.0.0.0", "0.0.0.0")])
def test_server_probes_the_address_it_will_actually_bind(monkeypatch, tmp_path,
                                                         ws_host, expected):
    """The port search must ask about the bind address, not always about localhost.

    mcp_bind_host() moves this port onto 0.0.0.0 with the WebSocket in LAN/WAN mode, and
    a loopback probe answers a different question there: on Windows it succeeds while a
    wildcard listener holds the port, so the server could pick a port it then fails to
    bind. Same mismatch the launcher's seek had, pointing the other way.

    BROWSERPLUG_STATE_DIR is redirected because _resolve_ws_host falls back to the
    persisted config.json, and on a developer box that file may already say 0.0.0.0 —
    which would let the localhost case pass for the wrong reason.
    """
    from server.runtime import lifecycle as srv
    monkeypatch.setenv("BROWSERPLUG_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("BROWSERPLUG_WS_HOST", raising=False)
    hosts = []

    def fake_find(start=MCP_PORT_DEFAULT, last=MCP_PORT_LAST, host="localhost"):
        hosts.append(host)
        return 2837

    monkeypatch.setattr(srv, "find_mcp_port", fake_find)
    server = BrowserPlugServer(port=_free_port(), mcp_port=None, start_mcp=False,
                               ws_host=ws_host, ws_token="t" * 32)
    assert server.mcp_port == 2837
    assert hosts == [expected], (
        f"the search probed {hosts} while the server will bind {expected!r}")


def test_server_probe_of_a_requested_port_uses_the_bind_address_too(monkeypatch, tmp_path):
    """The `elif port_is_bindable(...)` branch, i.e. the port launcher.py asks for."""
    from server.runtime import lifecycle as srv
    monkeypatch.setenv("BROWSERPLUG_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("BROWSERPLUG_WS_HOST", raising=False)
    seen = []

    def fake_bindable(port, host="localhost"):
        seen.append(host)
        return True

    monkeypatch.setattr(srv, "port_is_bindable", fake_bindable)
    server = BrowserPlugServer(port=_free_port(), mcp_port=2833, start_mcp=False,
                               ws_host="0.0.0.0", ws_token="t" * 32)
    assert server.mcp_port == 2833
    assert seen == ["0.0.0.0"]


def test_server_walks_up_when_the_requested_in_range_port_is_taken():
    """The launcher asks for a specific in-range port; a stranger on it must push the
    server to the NEXT port in the sequence, not make it fail or jump somewhere
    unpredictable — the client rediscovers it by walking the same order."""
    base = _free_band(2)
    sock = _listener(base, host="localhost")
    try:
        server = BrowserPlugServer(port=_free_port(), mcp_port=base, start_mcp=False)
        assert server.mcp_port == base + 1
    finally:
        sock.close()


# ------------------------------------------------------------- 3. the launcher seek

def test_launcher_finds_a_server_that_relocated_one_port_up(stub_servers, monkeypatch):
    monkeypatch.setattr(launcher, "server_pid_is_ours", lambda *a, **k: True)  # stubs live in this process
    monkeypatch.delenv(MCP_PORT_ENV, raising=False)
    base = _free_band(2)
    stub_servers(base + 1, _browserplug_payload(base + 1))
    assert launcher.find_server_port(timeout=1.0, start_port=base,
                                     last_port=base + 1) == base + 1


def test_launcher_walks_past_a_non_browserplug_server_squatting_on_the_first_port(stub_servers,
                                                                             monkeypatch):
    """A stranger's HTTP server answering 200 on the first port is NOT our server: proxying
    MCP into it would be a silent, confusing failure. Identity is a /health field, not a
    pulse."""
    monkeypatch.delenv(MCP_PORT_ENV, raising=False)
    monkeypatch.setattr(launcher, "server_pid_is_ours", lambda *a, **k: True)  # stubs live in this process
    base = _free_band(2)
    stub_servers(base, {"status": "ok", "service": "grafana", "version": "11"})
    stub_servers(base + 1, _browserplug_payload(base + 1))

    assert browserplug_health(base, timeout=1.0) is None
    assert launcher.find_server_port(timeout=1.0, start_port=base,
                                     last_port=base + 1) == base + 1
    port, health = seek_browserplug(base, base + 1, timeout=1.0)
    assert port == base + 1 and health["mcp_port"] == base + 1


def test_a_degraded_health_is_still_our_server(stub_servers, monkeypatch):
    """A server whose /health could not build its full report answers "degraded". It is
    still ours and still serving MCP; walking past it would spawn a duplicate that dies on
    the busy WebSocket port. Identity, not completeness, is what the seek checks."""
    monkeypatch.delenv(MCP_PORT_ENV, raising=False)
    base = _free_band(1)
    stub_servers(base, {"status": "degraded", "service": HEALTH_SERVICE, "server_pid": 4242,
                        "health_error": "RuntimeError: boom"})
    assert browserplug_health(base, timeout=1.0)["status"] == "degraded"
    port, _health = seek_browserplug(base, base, timeout=1.0)
    assert port == base


def test_a_status_the_server_never_sends_is_not_identification(stub_servers):
    base = _free_band(1)
    stub_servers(base, {"status": "starting", "service": HEALTH_SERVICE})
    assert browserplug_health(base, timeout=1.0) is None


def test_health_probe_ignores_a_proxy_set_in_the_environment(stub_servers, monkeypatch):
    """A corporate HTTP_PROXY must not be applied to our own loopback probe.

    urllib's default opener reads HTTP_PROXY/http_proxy from the environment. With one
    set and no NO_PROXY covering localhost, every probe in the range went to the proxy
    -- which cannot reach 127.0.0.1 -- so the seek found NOTHING while the server was up
    and healthy: each launcher then spawned a replacement that died as a duplicate
    (:8772 taken) and every client timed out at 30s. Same signature .tmp/dev-docs/ports.md records
    for the --mcp-port pin, with no pin involved.

    The proxy address here is a black hole: if the probe honours it, the request cannot
    succeed, so this test cannot pass by accident.
    """
    monkeypatch.delenv(MCP_PORT_ENV, raising=False)
    base = _free_band(2)
    stub_servers(base, _browserplug_payload(base))

    dead_proxy = "http://127.0.0.1:1"  # nothing listens on port 1
    for var in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(var, dead_proxy)
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)

    health = browserplug_health(base, timeout=2.0)
    assert health is not None, (
        "the /health probe was routed through HTTP_PROXY — a healthy local server "
        "reads as DOWN on every corporate-proxy machine")
    assert health["mcp_port"] == base

    port, _ = seek_browserplug(base, base + 1, timeout=2.0)
    assert port == base, "the seek walked past a live server because of HTTP_PROXY"


def test_seek_finds_nothing_when_the_range_holds_no_browserplug(stub_servers, monkeypatch):
    monkeypatch.delenv(MCP_PORT_ENV, raising=False)
    monkeypatch.setattr(mp, "browserplug_health", lambda *a, **k: None)
    monkeypatch.setattr(launcher, "browserplug_health", lambda *a, **k: None)
    monkeypatch.setattr(launcher, "seek_browserplug", lambda *a, **k: (None, None))
    assert launcher.find_server_port(timeout=0.2) is None


def test_seek_does_not_network_probe_ports_with_no_listener(stub_servers, monkeypatch):
    """An empty port must cost a bind syscall, NOT a network timeout.

    A closed port is not guaranteed to refuse instantly. With Windows Firewall enabled
    (the default: DefaultInboundAction=NotConfigured => drop, not reject) the SYN is
    dropped even on loopback, so every empty port costs the FULL SEEK_TIMEOUT_SECONDS —
    and pays it twice, once per family "localhost" resolves to. Live-measured on Win11:
    0.82 s per empty port x 113 ports = 93 s for one failed seek, against a 30 s client
    connect budget.

    The damage was to the RECOVERY path, which is why it hid for months: with a server up
    on 2830 the seek stops at the first probe and is instant, so everything looked fine.
    With no server running, every launcher was killed mid-walk at 30 s — before reaching
    the spawn that would have brought the server back. Healthy: fine. Down: permanently.
    """
    monkeypatch.delenv(MCP_PORT_ENV, raising=False)
    base = _free_band(3)
    stub_servers(base + 2, _browserplug_payload(base + 2))

    probed = []
    real_health = mp.browserplug_health
    monkeypatch.setattr(mp, "browserplug_health",
                        lambda port, **kw: (probed.append(port), real_health(port, **kw))[1])

    port, health = seek_browserplug(base, base + 2, timeout=1.0)

    assert port == base + 2 and health["mcp_port"] == base + 2, (
        "the skip must remove COST, never a candidate — a live server was walked past")
    assert probed == [base + 2], (
        f"the seek network-probed empty ports {sorted(set(probed) - {base + 2})}: on a "
        f"host that drops rather than refuses, each one costs the full timeout and the "
        f"launcher is killed mid-seek before it can spawn a replacement server")


def test_failed_seek_over_an_empty_band_is_fast(monkeypatch):
    """The wall-clock symptom itself, bounded.

    The call-count test above pins the mechanism; this pins what the user actually felt —
    'MCP server "browserplug" connection timed out after 30000ms'. The margin is ~3 orders of
    magnitude (measured: 93 s broken, 0.015 s fixed), so this is a real bound, not a
    flaky benchmark.
    """
    monkeypatch.delenv(MCP_PORT_ENV, raising=False)
    span = 20
    base = _free_band(span)

    started = time.monotonic()
    port, _ = seek_browserplug(base, base + span - 1)
    elapsed = time.monotonic() - started

    assert port is None
    budget = span * mp.SEEK_TIMEOUT_SECONDS / 4
    assert elapsed < budget, (
        f"a failed seek over {span} empty ports took {elapsed:.1f}s (budget {budget:.1f}s) "
        f"— it is paying a per-port network timeout again; over the real "
        f"{MCP_PORT_DEFAULT}-{MCP_PORT_LAST} range that is a client-killing 90s+")


def test_env_pin_is_honoured_and_skips_the_seek(stub_servers, monkeypatch):
    """BROWSERPLUG_MCP_PORT means 'this port, no discovery'. A BrowserPlug server elsewhere in the
    range must NOT be picked up — the user pinned a port on purpose."""
    pinned = _free_port()
    monkeypatch.setattr(launcher, "server_pid_is_ours", lambda *a, **k: True)  # stubs live in this process
    monkeypatch.setenv(MCP_PORT_ENV, str(pinned))
    assert pinned_mcp_port() == pinned

    seeks = []
    monkeypatch.setattr(launcher, "seek_browserplug", lambda *a, **k: seeks.append(k) or (2831, {}))
    stub_servers(pinned, _browserplug_payload(pinned))

    assert launcher.find_server_port(timeout=1.0) == pinned
    assert seeks == [], "a pinned port must never trigger a range seek"


def test_env_pin_that_is_dead_reports_no_server(monkeypatch):
    """Pinned but nothing there: report None (the caller spawns) — never silently seek
    somewhere else, which would hide the user's misconfiguration."""
    monkeypatch.setenv(MCP_PORT_ENV, str(_free_port()))
    monkeypatch.setattr(launcher, "seek_browserplug", lambda *a, **k: (2831, {}))
    assert launcher.find_server_port(timeout=0.5) is None


def test_spawn_uses_the_default_port_and_the_pin(monkeypatch):
    """The spawned server is told 2830 (it walks up itself if that is taken), or the pin."""
    seen = {}
    monkeypatch.setattr(launcher.subprocess, "Popen",
                        lambda args, **kw: seen.update(args=args) or None)

    monkeypatch.delenv(MCP_PORT_ENV, raising=False)
    launcher.spawn_detached_server()
    # No explicit port: an --mcp-port here would switch the server's own search off.
    assert "--mcp-port" not in seen["args"]

    monkeypatch.setenv(MCP_PORT_ENV, "2900")
    launcher.spawn_detached_server()
    assert seen["args"][-3:] == ["--mcp-port", "2900", "--mcp-port-pinned"]


def test_health_reports_the_service_identity(monkeypatch):
    """The seek keys on this field; it is part of the contract, not a decoration."""
    server = BrowserPlugServer(port=_free_port(), mcp_port=_free_port(), start_mcp=False)
    health = server.get_health_info()
    assert health["service"] == "browserplug"
    assert health["status"] == "ok"
    assert health["mcp_port"] == server.mcp_port


# ------------------------------------------------------------- 4. the pin is a pin (L8)

def test_pinned_busy_port_fails_clearly_instead_of_relocating(monkeypatch):
    """A pin means nobody searches: a busy pinned port is an error, never port+1."""
    from server.runtime import lifecycle
    base = _free_band(2)
    sock = _listener(base, host="localhost")
    try:
        with pytest.raises(RuntimeError, match=str(base)):
            lifecycle.choose_mcp_port(base, "localhost", pinned=True)
    finally:
        sock.close()


def test_pinned_free_port_is_taken_as_is():
    from server.runtime import lifecycle
    base = _free_band(1)
    assert lifecycle.choose_mcp_port(base, "localhost", pinned=True) == base


def test_server_with_a_busy_pinned_port_refuses_to_construct():
    base = _free_band(2)
    sock = _listener(base, host="localhost")
    try:
        with pytest.raises(RuntimeError, match=str(base)):
            BrowserPlugServer(port=_free_port(), mcp_port=base, start_mcp=False,
                              mcp_port_pinned=True)
    finally:
        sock.close()


def test_unpinned_launcher_spawn_passes_no_explicit_port(monkeypatch):
    """Without a pin the launcher must leave the port to the server's own search."""
    seen = {}
    monkeypatch.setattr(launcher.subprocess, "Popen",
                        lambda args, **kw: seen.update(args=args) or None)
    monkeypatch.delenv(MCP_PORT_ENV, raising=False)
    launcher.spawn_detached_server()
    assert "--mcp-port" not in seen["args"]
    assert "--mcp-port-pinned" not in seen["args"]


def test_main_treats_the_env_pin_as_a_pin(monkeypatch, tmp_path):
    """main() must hand the factory pinned=True when BROWSERPLUG_MCP_PORT is set, and the
    lock loser must log the range (not a phantom pin) when nothing is pinned."""
    import asyncio
    from server.runtime import lifecycle
    monkeypatch.setenv("BROWSERPLUG_STATE_DIR", str(tmp_path))
    monkeypatch.setenv(MCP_PORT_ENV, "38900")
    seen = {}

    class Stop(Exception):
        pass

    def factory(**kw):
        seen.update(kw)
        raise Stop

    monkeypatch.setattr(lifecycle, "acquire_instance_lock", lambda port: True)
    monkeypatch.setattr(lifecycle, "is_port_in_use", lambda port: False)
    with pytest.raises(Stop):
        asyncio.run(lifecycle.main(factory, ["--port", str(_free_port())]))
    assert seen["mcp_port"] == 38900
    assert seen["mcp_port_pinned"] is True
