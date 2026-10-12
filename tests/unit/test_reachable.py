"""
The WS port's one token-free HTTP path — GET /reachable — and the adapter-named
address list that backs the options page's LAN panel.

/reachable exists so the options page's WAN checker can hand check-host.net a URL an
external node may probe WITHOUT the connection token ever leaving the page. It is served
by websockets' `process_request` hook on the SAME socket as the extension WebSocket
(:8772 in production), so these tests run over real sockets: the thing being proven is
that a plain HTTP GET and a WebSocket handshake coexist on one listener — and that the
hook leaves the handshake alone.

The body is deliberately minimal ({"service","reachable"} and nothing else): an
internet-facing 200 must not leak a version, a pid, or — above all — the token.
"""

import asyncio
import json
import re
import socket
import urllib.request
from types import SimpleNamespace

import pytest
import websockets

from server import server as server_module
from server.reporting.net_addresses import ws_lan_addresses
from server.server import BrowserPlugServer
from server.ws.ws_auth import WS_CLOSE_UNAUTHORIZED, WS_TOKEN_ENDPOINT_PATH
from browserplug_shared.ws_token import WS_TOKEN_ENV


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _hello_frame(token=None, instance_id="ff-a"):
    """The hello background.js really sends (token key omitted entirely when unset)."""
    data = {"version": "1.4.0", "instance_id": instance_id, "tokenBootstrap": True}
    if token is not None:
        data["token"] = token
    return {"type": "extension_hello", "data": data}


async def _wait_until(predicate, what, timeout=5.0, interval=0.01):
    deadline = asyncio.get_event_loop().time() + timeout
    while not predicate():
        if asyncio.get_event_loop().time() >= deadline:
            raise AssertionError(f"timed out after {timeout}s waiting until {what}")
        await asyncio.sleep(interval)


def _http_get(url):
    """A plain blocking GET, like the check-host node would send (it speaks HTTP, not WS)."""
    with urllib.request.urlopen(url, timeout=5) as response:
        return response.status, dict(response.headers), response.read()


# --- GET /reachable over a real socket ------------------------------------------------

@pytest.mark.asyncio
async def test_reachable_answers_token_free_json_on_the_ws_port(monkeypatch):
    port = _free_port()
    server = BrowserPlugServer(host="localhost", port=port, start_mcp=False)
    ws_server = await websockets.serve(server.handle_extension_connection,
                                       "127.0.0.1", port,
                                       process_request=server._ws_process_request)
    try:
        status, headers, body = await asyncio.to_thread(
            _http_get, f"http://127.0.0.1:{port}/reachable")

        assert status == 200
        assert headers.get("Content-Type", "").startswith("application/json")
        payload = json.loads(body)
        assert payload == {"service": "browserplug", "reachable": True}, \
            "exactly these keys — an internet-facing 200 leaks nothing else"
        assert server.ws_token not in body.decode("utf-8", "replace")
    finally:
        ws_server.close()
        await ws_server.wait_closed()


@pytest.mark.asyncio
async def test_reachable_accepts_a_query_and_trailing_slash():
    """check-host nodes (and humans pasting URLs) append slashes and cache-busters."""
    port = _free_port()
    server = BrowserPlugServer(host="localhost", port=port, start_mcp=False)
    ws_server = await websockets.serve(server.handle_extension_connection,
                                       "127.0.0.1", port,
                                       process_request=server._ws_process_request)
    try:
        for path in ("/reachable/", "/reachable?x=1"):
            status, _, body = await asyncio.to_thread(
                _http_get, f"http://127.0.0.1:{port}{path}")
            assert status == 200, path
            assert json.loads(body)["reachable"] is True, path
    finally:
        ws_server.close()
        await ws_server.wait_closed()


@pytest.mark.asyncio
async def test_process_request_passes_websocket_handshakes_through(monkeypatch, tmp_path):
    """The hook must be invisible to the extension: the full bootstrap dance — rejected
    with 4401, fetch the token from /ws-token, reconnect, register — still works with
    process_request installed. (This is the regression a broken hook would cause: every
    handshake answered as HTTP, no browser ever connecting again.)"""
    monkeypatch.delenv(WS_TOKEN_ENV, raising=False)
    monkeypatch.setenv(server_module.WS_TOKEN_FILE_ENV, str(tmp_path / "ws-token"))

    ws_port, mcp_port = _free_port(), _free_port()
    server = BrowserPlugServer(host="localhost", port=ws_port, mcp_port=mcp_port,
                          start_mcp=True)
    await server.start_mcp_server()
    ws_server = await websockets.serve(server.handle_extension_connection,
                                       server.ws_host, server.port,
                                       process_request=server._ws_process_request)
    url = f"ws://localhost:{server.port}"
    try:
        # 1. No token -> 4401 with directions (a WS close, not an HTTP response).
        async with websockets.connect(url) as sock:
            await sock.send(json.dumps(_hello_frame()))
            with pytest.raises(websockets.exceptions.ConnectionClosed) as closed:
                await asyncio.wait_for(sock.recv(), timeout=5.0)
        assert closed.value.rcvd.code == WS_CLOSE_UNAUTHORIZED
        endpoint = re.search(r"https?://\S+", closed.value.rcvd.reason).group(0)
        assert endpoint.endswith(WS_TOKEN_ENDPOINT_PATH)

        # 2. Fetch the token over HTTP (the MCP port, not the WS port).
        with urllib.request.urlopen(endpoint, timeout=5) as response:
            token = json.loads(response.read())["token"]

        # 3. Reconnect with it — the handshake the hook must not have touched.
        from secure_ext import SecureClient
        async with websockets.connect(url) as sock:
            await SecureClient.connect(sock, token, _hello_frame())
            await _wait_until(lambda: "ff-a" in server.browsers,
                              "the bootstrapped browser registered")
    finally:
        ws_server.close()
        await ws_server.wait_closed()
        server.lifecycle.stop_mcp_server()


# --- /reachable stays GET-only across the websockets 16 -> 17 change ------------------

def _raw_request(port, raw, timeout=5):
    """Send bytes the stdlib HTTP clients refuse to send, and return the status line.

    urllib will not emit HTTP/1.0 or a body-less POST on demand, and both are exactly
    what has to be tested here, so this speaks the protocol directly. An empty string
    means the peer closed without answering.
    """
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
        sock.sendall(raw)
        sock.settimeout(timeout)
        chunks = []
        try:
            while len(b"".join(chunks)) < 2048:
                chunk = sock.recv(1024)
                if not chunk:
                    break
                chunks.append(chunk)
                if b"\r\n" in b"".join(chunks):
                    break
        except (TimeoutError, socket.timeout, ConnectionResetError, OSError):
            pass
    return b"".join(chunks).decode("utf-8", "replace").split("\r\n")[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["POST", "HEAD", "DELETE", "PUT"])
async def test_reachable_never_answers_a_non_get_method(method):
    """Only GET may get the reachable body — on either websockets major.

    websockets 16 raised during request parsing for any method other than GET, so this
    hook was never called and the connection was dropped. 17 parses them and calls the
    hook, which would have handed every verb the same token-free 200 on the one endpoint
    that is deliberately unauthenticated and may face the internet. The guard in
    _ws_process_request keeps the answer GET-only; this pins the contract rather than
    whichever library version happens to be installed, so it is meaningful on both.
    """
    port = _free_port()
    server = BrowserPlugServer(host="localhost", port=port, start_mcp=False)
    ws_server = await websockets.serve(server.handle_extension_connection,
                                       "127.0.0.1", port,
                                       process_request=server._ws_process_request)
    try:
        raw = (f"{method} /reachable HTTP/1.1\r\nHost: x\r\n"
               f"Content-Length: 0\r\nConnection: close\r\n\r\n").encode()
        status_line = await asyncio.to_thread(_raw_request, port, raw)
        assert "200" not in status_line, (
            f"{method} /reachable answered {status_line!r} — the token-free endpoint "
            f"must not serve anything but GET")
    finally:
        ws_server.close()
        await ws_server.wait_closed()


@pytest.mark.asyncio
async def test_reachable_still_answers_a_plain_get_after_the_method_guard():
    """The guard must not cost the one request the endpoint exists to serve."""
    port = _free_port()
    server = BrowserPlugServer(host="localhost", port=port, start_mcp=False)
    ws_server = await websockets.serve(server.handle_extension_connection,
                                       "127.0.0.1", port,
                                       process_request=server._ws_process_request)
    try:
        raw = b"GET /reachable HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"
        status_line = await asyncio.to_thread(_raw_request, port, raw)
        assert "200" in status_line, status_line
    finally:
        ws_server.close()
        await ws_server.wait_closed()


@pytest.mark.asyncio
async def test_extension_connection_close_is_a_coroutine():
    """lifecycle.close_extension_connection branches on this, against a REAL library object.

    server.py's sync stop path calls conn.close() and schedules the result only when
    asyncio.iscoroutine() says so; if a future websockets made close() synchronous again
    the branch would silently stop closing sockets. Every other test of that path drives
    an AsyncMock, which answers the question by construction, so this asks a real
    ServerConnection instead.
    """
    port = _free_port()
    server = BrowserPlugServer(host="localhost", port=port, start_mcp=False)
    ws_server = await websockets.serve(server.handle_extension_connection,
                                       "127.0.0.1", port,
                                       process_request=server._ws_process_request)
    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}") as client:
            await client.send(json.dumps(_hello_frame()))
            await _wait_until(lambda: "ff-a" in server.browsers, "the browser registered")
            conn = server.browsers["ff-a"].ws
            closing = conn.close()
            assert asyncio.iscoroutine(closing), (
                "websockets' Connection.close() is no longer async — the iscoroutine "
                "branch in lifecycle.close_extension_connection now silently closes nothing")
            await closing
    finally:
        ws_server.close()
        await ws_server.wait_closed()


# --- ws_lan_addresses: the adapter-named list behind the LAN panel --------------------

def _snic(family, address):
    return SimpleNamespace(family=family, address=address)


def test_health_reports_adapter_named_lan_addresses(monkeypatch):
    import psutil
    fake_table = {
        "Loopback": [_snic(socket.AF_INET, "127.0.0.1")],
        "Ethernet": [_snic(socket.AF_INET, "192.168.1.42"),
                     _snic(socket.AF_INET6, "fe80::1")],
        "vEthernet (WSL)": [_snic(socket.AF_INET, "172.23.0.5")],
        "Corp VPN": [_snic(socket.AF_INET, "10.1.2.3"),
                     _snic(socket.AF_INET, "169.254.10.9")],
    }
    monkeypatch.setattr(psutil, "net_if_addrs", lambda: fake_table)

    rows = ws_lan_addresses()
    assert rows[0] == {"ip": "192.168.1.42", "interface": "Ethernet",
                      "recommended": True}, "the home-router range leads the list"
    ips = [r["ip"] for r in rows]
    assert "127.0.0.1" not in ips and "169.254.10.9" not in ips and "fe80::1" not in ips
    assert set(ips) == {"192.168.1.42", "10.1.2.3", "172.23.0.5"}
    assert all(r["recommended"] is False for r in rows[1:])

    health = BrowserPlugServer(host="localhost", port=8772, start_mcp=False).get_health_info()
    assert health["ws_lan_addresses"] == rows
    assert isinstance(health["ws_lan_urls"], list), "the un-named list stays for old pages"


def test_ws_lan_addresses_survives_psutil_failure(monkeypatch):
    """net_if_addrs CAN throw (odd adapters, permissions) — /health must not."""
    import psutil

    def boom():
        raise OSError("no adapters for you")

    monkeypatch.setattr(psutil, "net_if_addrs", boom)
    assert ws_lan_addresses() == []
    health = BrowserPlugServer(host="localhost", port=8772, start_mcp=False).get_health_info()
    assert health["ws_lan_addresses"] == []
