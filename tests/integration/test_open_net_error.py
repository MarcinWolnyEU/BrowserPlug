"""`tabs(action="open")` and `navigate` report a page that never loaded, on both engines.

Both browsers answer a failed lookup or connection by COMMITTING their own error page, at
the requested URL, so a naive "did it commit" check calls the open a success. The extension
has to see through that: the open must come back as a failure that names the net error,
and an unrouted `.onion` address must carry the hint that names the call that works
(`server/tools/tor_proxy.py`, .tmp/dev-docs/tor.md).

Only local and never-resolvable addresses are used: a refused loopback port, the `.invalid`
and `.onion` names, which no resolver outside Tor answers, and a loopback server that
breaks its responses on purpose (`misbehaving_server`).
"""

import re
import socket
import threading

import pytest
import pytest_asyncio

import test_imports  # noqa: F401  (path setup)
from mcp_client_harness import DirectMCPTestClient
from port_coordinator import dead_port

ONION = "http://2gzyxa5ihm7nsggfxnu52rck2vv4rvmdlkiu3zzui5du4xyclen53wid.onion/"
# Gecko: NS_ERROR_UNKNOWN_HOST. Blink: net::ERR_NAME_NOT_RESOLVED.
NAME_NOT_RESOLVED = re.compile(r"NS_ERROR_UNKNOWN_HOST|ERR_NAME_NOT_RESOLVED")
CONNECTION_REFUSED = re.compile(r"NS_ERROR_CONNECTION_REFUSED|ERR_CONNECTION_REFUSED")


PARTIAL_HTML = (b"<!doctype html><title>Partial page</title><h1>PARTIAL-MARKER</h1><p>"
                + b"x" * 3000 + b"</p>")
# What the headers promise; the server sends PARTIAL_HTML and stops well short of it.
PROMISED_LENGTH = 200000


def _misbehave(conn, dead_port):
    """Answer one request the way its path asks to fail."""
    try:
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = conn.recv(4096)
            if not chunk:
                return
            head += chunk
        path = head.split(b" ", 2)[1]
        if path == b"/redirect-refused":
            conn.sendall(b"HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1:%d/\r\n"
                         b"Content-Length: 0\r\nConnection: close\r\n\r\n" % dead_port)
            return
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: %d\r\n"
                     b"Cache-Control: no-store\r\nConnection: close\r\n\r\n" % PROMISED_LENGTH)
        conn.sendall(PARTIAL_HTML)
        conn.shutdown(socket.SHUT_WR)
    except OSError:
        pass
    finally:
        conn.close()


@pytest.fixture(scope="module")
def misbehaving_server():
    """A loopback HTTP server whose responses break: /partial-close sends headers and part
    of the body, then closes cleanly; /redirect-refused redirects to a port nothing
    listens on."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(16)

    with dead_port() as refused_port:
        def accept_loop():
            while True:
                try:
                    conn, _ = listener.accept()
                except OSError:
                    return
                threading.Thread(target=_misbehave, args=(conn, refused_port),
                                 daemon=True).start()

        threading.Thread(target=accept_loop, daemon=True).start()
        yield f"http://127.0.0.1:{listener.getsockname()[1]}"
        listener.close()


@pytest.mark.slow
class TestOpenNetError:
    @pytest_asyncio.fixture
    async def mcp(self, server_with_extension):
        client = DirectMCPTestClient(server_with_extension['server'].mcp_tools)
        client.engine = server_with_extension['engine']
        await client.connect()
        yield client
        await client.disconnect()

    async def _open(self, mcp, url):
        return await mcp.call_tool("tabs", {"action": "open", "url": url, "background": True})

    async def _request_log(self, mcp, res):
        """The extension's request log for the tab an open named: an open that should have
        failed and did not then says what the extension saw (one did, once, mid-sweep)."""
        found = re.search(r"Opened tab \[(\d+)\]", res.get("content") or "")
        if not found:
            return f"{res}"
        log = await mcp.call_tool("requests", {
            "action": "recent", "tab_id": int(found.group(1)), "limit": 10})
        return f"{res}\nrequest log: {log.get('content')}"

    @pytest.mark.asyncio
    async def test_a_page_that_loads_is_not_flagged(self, mcp, fixtures_server):
        res = await self._open(mcp, fixtures_server + "titled.html")
        assert not res.get("isError"), res
        assert "WARNING" not in res["content"], res["content"]

    @pytest.mark.asyncio
    async def test_open_of_an_unresolvable_name_is_a_failure_naming_the_error(self, mcp):
        res = await self._open(mcp, "http://no-such-host.invalid/")
        assert res.get("isError"), await self._request_log(mcp, res)
        assert NAME_NOT_RESOLVED.search(res["content"]), res["content"]

    @pytest.mark.asyncio
    async def test_open_of_a_refused_port_is_a_failure_naming_the_error(self, mcp):
        with dead_port() as port:
            res = await self._open(mcp, f"http://127.0.0.1:{port}/")
        assert res.get("isError"), await self._request_log(mcp, res)
        assert CONNECTION_REFUSED.search(res["content"]), res["content"]

    @pytest.mark.asyncio
    async def test_open_of_an_onion_without_a_proxy_says_what_to_call(self, mcp):
        res = await self._open(mcp, ONION)
        content = res["content"]
        assert res.get("isError"), await self._request_log(mcp, res)
        assert NAME_NOT_RESOLVED.search(content), content
        assert "HINT: .onion addresses resolve only inside Tor" in content, content
        assert 'proxy="tor"' in content, content
        if mcp.engine == "chromium":
            # Chromium has no per-tab proxy: the call is offered with its real scope.
            assert "switches the ENTIRE browser" in content, content

    @pytest.mark.asyncio
    async def test_navigate_to_an_onion_without_a_proxy_says_what_to_call(self, mcp, fixtures_server):
        opened = await self._open(mcp, fixtures_server + "titled.html")
        tab_id = int(re.search(r"Opened tab \[(\d+)\]", opened["content"]).group(1))
        res = await mcp.call_tool("navigate", {
            "tab_id": tab_id, "url": ONION, "wait": "settled", "timeout_ms": 15000})
        content = res["content"]
        assert NAME_NOT_RESOLVED.search(content), content
        assert "HINT: .onion addresses resolve only inside Tor" in content, content

    @pytest.mark.asyncio
    async def test_open_through_a_redirect_to_a_refused_port_is_a_failure(self, mcp, misbehaving_server):
        res = await self._open(mcp, misbehaving_server + "/redirect-refused")
        assert res.get("isError"), await self._request_log(mcp, res)
        assert CONNECTION_REFUSED.search(res["content"]), res["content"]

    @pytest.mark.asyncio
    async def test_a_body_cut_short_by_a_clean_close(self, mcp, misbehaving_server):
        # Firefox renders the part that arrived and reports the error through both the
        # request log and webNavigation; either way the open committed. Chromium replaces
        # the page with its error page, and whether the open sees the commit or the error
        # first is a race (Chrome 157 gave both), so either answer is right there.
        res = await self._open(mcp, misbehaving_server + "/partial-close")
        content = res["content"]
        if mcp.engine == "chromium":
            if res.get("isError"):
                assert "ERR_CONTENT_LENGTH_MISMATCH" in content, content
            else:
                assert "partial-close" in content, content
            return
        assert not res.get("isError"), res
        assert "WARNING" not in content, content
        tab_id = int(re.search(r"Opened tab \[(\d+)\]", content).group(1))
        page = await mcp.call_tool("page_read", {"tab_id": tab_id, "format": "text"})
        assert "PARTIAL-MARKER" in page["content"], page["content"]
