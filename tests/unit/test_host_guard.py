"""DNS rebinding (review finding S1): the real ASGI wrapper around the real MCP server app.

A page whose name is rebound to 127.0.0.1 arrives as a loopback peer but keeps the attacker's
name in Host and Origin. Before the guard, initialize, tools/call and /ws-token all answered
it. The tests drive `BrowserPlugServer._wrap_client_tracking` exactly as production wires it.

Two checks stand in front of the MCP app: HostGuard (host_guard.py) in the wrapper, on
every path, and the origin layer (mcp_host_origin.py, the pre-SDK guard's rules) inside
the app. `[wrapper-only]` proves HostGuard holds on its own; `[with-origin-layer]` is
production. Where the two disagree, the tests below assert each variant's answer.
"""

import asyncio
import json
import socket

import pytest

from secure_ext import signed_headers
from server.tools.mcp_server import BrowserPlugMCPServer
from server.tools.tool_registry import ToolRegistry, ToolSpec
from server.web import host_guard, mcp_host_origin
from server.server import BrowserPlugServer

EVIL = "rebind.attacker.example:2830"
PEER = ("127.0.0.1", 50000)
MCP_HEADERS = {"content-type": "application/json",
               "accept": "application/json, text/event-stream"}


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _server():
    return BrowserPlugServer(host="localhost", port=_free_port(), start_mcp=False,
                             ws_token="a" * 43)


def secret_tool() -> str:
    return "tool ran"


@pytest.fixture(params=[False, True], ids=["wrapper-only", "with-origin-layer"])
def stack(request):
    """The wrapper around the app production serves (BrowserPlugMCPServer.http_app, the
    SDK's own DNS-rebinding check off), without and with the origin layer: HostGuard must
    hold on its own, and together with the layer production enables."""
    server = _server()
    app = BrowserPlugMCPServer(ToolRegistry([ToolSpec.from_function(secret_tool)])).http_app(
        host_origin_layer=request.param)
    return server._wrap_client_tracking(app), app


@pytest.fixture
def layer_on(request, stack):
    """True on the `[with-origin-layer]` variant."""
    return request.node.callspec.params["stack"]


async def call(wrapped, method, path, headers, body=b"", peer=PEER):
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": method, "scheme": "http", "path": path, "raw_path": path.encode(),
             "query_string": b"", "root_path": "", "client": peer,
             "server": ("127.0.0.1", 2830),
             "headers": [(k.encode(), v.encode()) for k, v in headers.items()]
                        + [(b"content-length", str(len(body)).encode())]}
    sent = [False]

    async def receive():
        if not sent[0]:
            sent[0] = True
            return {"type": "http.request", "body": body, "more_body": False}
        await asyncio.sleep(3600)

    out = {"status": None, "headers": {}, "body": b""}
    done = asyncio.Event()

    async def send(m):
        if m["type"] == "http.response.start":
            out["status"] = m["status"]
            out["headers"] = {k.decode().lower(): v.decode() for k, v in m.get("headers", [])}
        elif m["type"] == "http.response.body":
            out["body"] += m.get("body", b"")
            if not m.get("more_body") or b"tool ran" in out["body"]:
                done.set()

    task = asyncio.create_task(wrapped(scope, receive, send))
    await asyncio.wait_for(done.wait(), 10)
    task.cancel()
    return out


INIT = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                   "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                              "clientInfo": {"name": "t", "version": "1"}}}).encode()


@pytest.mark.asyncio
async def test_a_rebound_host_gets_nothing_from_any_route(stack):
    wrapped, app = stack
    async with app.router.lifespan_context(app):
        h = dict(MCP_HEADERS, host=EVIL, origin="http://" + EVIL)
        r = await call(wrapped, "POST", "/mcp", h, INIT)
        assert r["status"] == 403 and "mcp-session-id" not in r["headers"], r
        r = await call(wrapped, "GET", "/ws-token", {"host": EVIL, "origin": "http://" + EVIL})
        assert r["status"] == 403 and b"token" not in r["body"].lower(), r
        for path in ("/health", "/ready", "/doctor"):
            r = await call(wrapped, "GET", path, {"host": EVIL})
            assert r["status"] == 403, (path, r)


@pytest.mark.asyncio
async def test_a_rebound_tools_call_never_runs(stack):
    wrapped, app = stack
    async with app.router.lifespan_context(app):
        good = dict(MCP_HEADERS, host="127.0.0.1:2830")
        r = await call(wrapped, "POST", "/mcp", good, INIT)
        sid = r["headers"]["mcp-session-id"]
        evil = dict(MCP_HEADERS, host=EVIL, **{"mcp-session-id": sid,
                                               "mcp-protocol-version": "2025-06-18"})
        body = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                           "params": {"name": "secret_tool", "arguments": {}}}).encode()
        r = await call(wrapped, "POST", "/mcp", evil, body)
        assert r["status"] == 403 and b"tool ran" not in r["body"]


@pytest.mark.asyncio
@pytest.mark.parametrize("host", ["127.0.0.1:2830", "localhost:2830", "LOCALHOST:2830",
                                  "localhost", "[::1]:2830", "[::1]"])
async def test_loopback_hosts_reach_mcp_and_the_token_endpoint(stack, host):
    wrapped, app = stack
    async with app.router.lifespan_context(app):
        r = await call(wrapped, "POST", "/mcp", dict(MCP_HEADERS, host=host), INIT)
        assert r["status"] == 200 and "mcp-session-id" in r["headers"], (host, r)
        r = await call(wrapped, "GET", "/ws-token", {"host": host})
        assert r["status"] == 200, (host, r)


@pytest.mark.asyncio
async def test_a_request_without_a_host_header_is_admitted(stack):
    """HTTP/1.0 clients omit it; a browser (the rebinding vector) never does."""
    wrapped, app = stack
    r = await call(wrapped, "GET", "/health", {})
    assert r["status"] == 200


@pytest.mark.asyncio
async def test_a_remote_options_page_reaches_health_by_lan_ip(stack, monkeypatch):
    """The remote options page dials http://<lan-ip>:2830/health with a signed request;
    the Host it sends is the LAN IP (IPv4, or a bracketed IPv6) and must be admitted."""
    _, app = stack
    monkeypatch.setattr(host_guard, "own_addresses", lambda: {"192.168.30.5", "fe80::1"})
    server = _server()
    wrapped = server._wrap_client_tracking(app)
    remote = ("192.168.30.171", 44444)

    async def health(host):
        h = dict(signed_headers(server, "GET", "/health"))
        h = {(k.decode() if isinstance(k, bytes) else k): (v.decode() if isinstance(v, bytes) else v)
             for k, v in (h.items() if isinstance(h, dict) else h)}
        return await call(wrapped, "GET", "/health", dict(h, host=host), peer=remote)

    for host in ("192.168.30.5:2830", "192.168.30.5", "[fe80::1]:2830"):
        r = await health(host)
        assert r["status"] == 200, (host, r)
    assert (await health("192.168.30.99:2830"))["status"] == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["http://rebind.attacker.example", "http://evil.example:2830",
                                    "null", "moz-extension://abc", "file://",
                                    "http://0.0.0.0:2830"])
async def test_mcp_refuses_a_foreign_origin_even_from_a_loopback_host(stack, origin):
    wrapped, app = stack
    async with app.router.lifespan_context(app):
        h = dict(MCP_HEADERS, host="127.0.0.1:2830", origin=origin)
        r = await call(wrapped, "POST", "/mcp", h, INIT)
        assert r["status"] == 403, (origin, r)


@pytest.mark.asyncio
async def test_mcp_admits_a_loopback_origin_and_no_origin(stack):
    wrapped, app = stack
    async with app.router.lifespan_context(app):
        for origin in (None, "http://localhost:3000", "http://127.0.0.1:2830",
                       "http://[::1]:5173"):
            h = dict(MCP_HEADERS, host="127.0.0.1:2830")
            if origin:
                h["origin"] = origin
            r = await call(wrapped, "POST", "/mcp", h, INIT)
            assert r["status"] == 200, (origin, r)


# --- the pure policy ------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("Localhost:80", "localhost"), ("[::1]:2830", "::1"), ("[::1]", "::1"),
    ("[::FFFF:127.0.0.1]:1", "127.0.0.1"), ("example.org.", "example.org"),
    ("host:notaport", None), ("[::1", None), ("", None), ("a:1:2", None),
])
def test_split_host(raw, expected):
    assert host_guard.split_host(raw) == expected


UNCOMMON_LOOPBACK = ("[0:0:0:0:0:0:0:1]:2830", "localhost.:2830", "[::ffff:127.0.0.1]:2830")


def test_uncommon_spellings_of_loopback_are_admitted():
    """The origin layer behind HostGuard refuses these on the MCP app (421, as the pre-SDK
    guard did); no real client sends them, but HostGuard must not be the one that
    refuses a valid spelling. None is a rebinding vector: two are IP literals, which no
    DNS answer can produce, and `localhost.` is the loopback name itself."""
    g = host_guard.HostGuard(addresses=lambda: set())
    for h in UNCOMMON_LOOPBACK:
        assert g.host_allowed(h), h


def _misdirected(r):
    return (r["status"], r["body"], "content-type" in r["headers"]) == (
        421, b"Misdirected Request", False)


def _ran(r):
    return r["status"] == 200 and "mcp-session-id" in r["headers"]


@pytest.mark.asyncio
@pytest.mark.parametrize("host", UNCOMMON_LOOPBACK)
async def test_uncommon_spellings_of_loopback_get_421_from_the_origin_layer(stack, layer_on, host):
    wrapped, app = stack
    async with app.router.lifespan_context(app):
        r = await call(wrapped, "POST", "/mcp", dict(MCP_HEADERS, host=host), INIT)
    assert (_misdirected(r) if layer_on else _ran(r)), (host, r)


@pytest.mark.asyncio
async def test_this_machines_own_names_reach_health_but_get_421_from_the_mcp_app(
        stack, layer_on, monkeypatch):
    """HostGuard admits this machine's hostname and interface addresses and the hosts
    BROWSERPLUG_ALLOWED_HOSTS lists (a remote options page dials them). On a loopback
    socket the origin layer allows only the loopback names, so on the MCP app they are
    421, while /health still answers. A request with no Host is the same."""
    _, app = stack
    monkeypatch.setattr(host_guard, "own_addresses", lambda: {"192.168.30.5"})
    monkeypatch.setenv(host_guard.ALLOWED_HOSTS_ENV, "home.example.net")
    wrapped = _server()._wrap_client_tracking(app)
    hosts = [f"{socket.gethostname()}:2830", "192.168.30.5:2830", "home.example.net:2830", None]
    async with app.router.lifespan_context(app):
        for host in hosts:
            extra = {} if host is None else {"host": host}
            r = await call(wrapped, "POST", "/mcp", dict(MCP_HEADERS, **extra), INIT)
            assert (_misdirected(r) if layer_on else _ran(r)), (host, r)
            r = await call(wrapped, "GET", "/health", extra)
            assert r["status"] == 200, (host, r)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/mcp/", "/not-a-route"])
async def test_the_origin_layer_covers_every_path_the_mcp_app_receives(stack, layer_on, path):
    """The origin check covers the whole MCP app, so it sees every request without a route
    row in HttpEndpoints, not only /mcp."""
    wrapped, app = stack
    async with app.router.lifespan_context(app):
        r = await call(wrapped, "POST", path, dict(MCP_HEADERS, host="localhost.:2830"), INIT)
    assert (_misdirected(r) if layer_on else r["status"] in (307, 404)), (path, r)


@pytest.mark.asyncio
async def test_an_origin_naming_this_machine_off_loopback_is_403_from_the_origin_layer(
        stack, layer_on, monkeypatch):
    """HostGuard admits an Origin that names this machine on any port. The origin layer
    admits one only if it is loopback like the Host, or the very origin the request was
    sent to, so a page another local server publishes on the LAN address is refused."""
    _, app = stack
    monkeypatch.setattr(host_guard, "own_addresses", lambda: {"192.168.30.5"})
    wrapped = _server()._wrap_client_tracking(app)
    async with app.router.lifespan_context(app):
        for origin in ("http://192.168.30.5:8080", f"http://{socket.gethostname()}:2830"):
            h = dict(MCP_HEADERS, host="127.0.0.1:2830", origin=origin)
            r = await call(wrapped, "POST", "/mcp", h, INIT)
            if layer_on:
                assert (r["status"], r["body"]) == (403, b"Forbidden Origin"), (origin, r)
            else:
                assert _ran(r), (origin, r)


# The verdicts of the pre-SDK guard (fastmcp 4.0.10's HostOriginGuardMiddleware, mode
# "auto", no allowlists), recorded by running it: (socket address, Host, Origin, status).
# They pin the rules mcp_host_origin.py must keep.
FASTMCP_VERDICTS = [
    ("127.0.0.1", "127.0.0.1:2830", None, 200),
    ("127.0.0.1", "localhost", "http://localhost:3000", 200),
    ("127.0.0.1", "[::1]:2830", "http://127.0.0.1:5173", 200),
    ("::1", "[::1]:2830", None, 200),
    ("127.0.0.1", None, None, 421),
    ("127.0.0.1", "localhost.:2830", None, 421),
    ("127.0.0.1", "[0:0:0:0:0:0:0:1]:2830", None, 421),
    ("127.0.0.1", "[::ffff:127.0.0.1]:2830", None, 421),
    ("127.0.0.1", "192.168.30.5:2830", None, 421),
    ("127.0.0.1", "box:2830", None, 421),
    ("127.0.0.2", "127.0.0.2:2830", None, 200),
    ("127.0.0.1", "127.0.0.1:2830", "http://192.168.30.5:8080", 403),
    ("127.0.0.1", "localhost:2830", "http://box:2830", 403),
    ("127.0.0.1", "localhost:2830", "null", 403),
    ("192.168.30.5", "192.168.30.5:2830", None, 200),
    ("192.168.30.5", "192.168.30.5:2830", "http://192.168.30.5:2830", 200),
    ("192.168.30.5", "192.168.30.5:2830", "http://evil.example", 200),
    ("192.168.30.5", "localhost:2830", "http://192.168.30.5:2830", 403),
    ("192.168.30.5", "localhost:2830", "http://localhost:2830", 200),
    ("192.168.30.5", "box:2830", "http://evil.example", 200),
]


@pytest.mark.parametrize("sock,host,origin,status", FASTMCP_VERDICTS)
def test_the_origin_layer_answers_as_fastmcps_did(sock, host, origin, status):
    headers = [(b"host", host.encode())] if host else []
    if origin:
        headers.append((b"origin", origin.encode()))
    refused = mcp_host_origin.refusal({"type": "http", "scheme": "http",
                                       "server": (sock, 2830), "headers": headers})
    assert (refused or (200, None))[0] == status


def test_unspecified_addresses_are_never_hosts():
    """A browser can dial 0.0.0.0 and reach loopback; the Host then names 0.0.0.0."""
    g = host_guard.HostGuard(extra_hosts=lambda: ("0.0.0.0", "::"),
                             addresses=lambda: {"0.0.0.0"})
    assert not g.host_allowed("0.0.0.0:2830")
    assert not g.host_allowed("[::]:2830")


def test_env_and_bind_hosts_extend_the_list(monkeypatch):
    monkeypatch.setenv(host_guard.ALLOWED_HOSTS_ENV, "home.example.net; 203.0.113.7")
    g = host_guard.HostGuard(extra_hosts=lambda: ("192.168.1.9",), addresses=lambda: set(),
                             hostname=lambda: "BOX")
    for h in ("home.example.net:2830", "203.0.113.7:2830", "192.168.1.9", "box:2830"):
        assert g.host_allowed(h), h
    assert not g.host_allowed("evil.example")


def test_a_new_interface_address_is_picked_up_without_restart(monkeypatch):
    addrs = {"10.0.0.2"}
    g = host_guard.HostGuard(addresses=lambda: set(addrs))
    assert g.host_allowed("10.0.0.2")
    addrs.add("10.0.0.3")
    monkeypatch.setattr(host_guard, "_REFRESH_SECONDS", 0.0)
    assert g.host_allowed("10.0.0.3")


def test_split_host_rejects_non_ascii_digit_ports():
    from server.web.host_guard import split_host
    assert split_host("localhost:²") is None
    assert split_host("[::1]:١٢") is None
    assert split_host("localhost:80") == "localhost"
