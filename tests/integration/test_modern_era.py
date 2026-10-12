"""The 2026-07-28 era against a real browser, so the integration sweep covers the default.

The server's default era is `all`: a client that asks for 2026-07-28 is served it over
HTTP, --stdio, the proxy front and the relay. Every other integration module speaks the
handshake era (the harness default), and the modern path is otherwise exercised only with
fake extensions. This module drives a few real tools through the harness's modern mode:

  * over real Streamable HTTP, where the connection has no Mcp-Session-Id and the server
    keys it by a synthetic per-connection identity (the routing a browser call needs);
  * in-process, the SDK's per-request modern path without a transport.

Tool bodies are era-independent, so this is a smoke set, not a second sweep: server_info,
a tab opened, listed and closed, and a navigate that reads the page in the same call.
"""

import re

import pytest
import pytest_asyncio

import test_imports  # noqa: F401  (path setup)
from mcp_client_harness import MODERN_MODE, DirectMCPTestClient

EXAMPLE = "https://example.org/"
TRANSPORTS = ["http", "in-process"]


def _tab_id(opened):
    found = re.search(r"Opened tab \[(\d+)\]", opened["content"])
    assert found, f"tabs open should report the new tab id: {opened['content']}"
    return int(found.group(1))


def _listed_ids(content):
    return [int(n) for n in re.findall(r"^id: (\d+)", content, re.MULTILINE)]


@pytest.mark.slow
class TestModernEra:

    @pytest_asyncio.fixture(params=TRANSPORTS, ids=lambda t: t)
    async def mcp(self, request, server_with_extension):
        server = server_with_extension["server"]
        url = (f"http://localhost:{server_with_extension['mcp_port']}/mcp"
               if request.param == "http" else None)
        client = DirectMCPTestClient(server.mcp_tools, mcp_url=url, mode=MODERN_MODE)
        await client.connect()
        try:
            yield client
        finally:
            await client.disconnect()

    @staticmethod
    def _assert_modern(mcp):
        """Every session the harness opened negotiated 2026-07-28: the calls above were
        made in the era this module is about, not on a silent fallback."""
        assert mcp.protocol_versions, "the harness opened no session"
        assert set(mcp.protocol_versions) == {MODERN_MODE}, mcp.protocol_versions

    @pytest.mark.asyncio
    async def test_server_info_and_the_tab_lifecycle(self, mcp):
        info = await mcp.call_tool("server_info", {})
        assert info["success"] is True and isinstance(info["content"], str), info
        assert info["content"].strip(), "server_info returned no text"

        opened = await mcp.call_tool("tabs", {"action": "open", "url": EXAMPLE,
                                              "background": True})
        assert opened["success"], opened
        tab_id = _tab_id(opened)
        try:
            listed = await mcp.call_tool("tabs", {})
            assert listed["success"], listed
            assert tab_id in _listed_ids(listed["content"]), listed["content"]
        finally:
            closed = await mcp.call_tool("tabs", {"action": "close", "tab_id": tab_id})
        assert closed["success"], closed
        after = await mcp.call_tool("tabs", {})
        assert tab_id not in _listed_ids(after["content"]), after["content"]
        self._assert_modern(mcp)

    @pytest.mark.asyncio
    async def test_navigate_reads_the_page_in_the_same_call(self, mcp):
        opened = await mcp.call_tool("tabs", {"action": "open", "url": EXAMPLE,
                                              "background": True})
        assert opened["success"], opened
        tab_id = _tab_id(opened)
        try:
            nav = await mcp.call_tool("navigate", {
                "tab_id": tab_id, "url": EXAMPLE, "wait": "settled", "read": "markdown"})
            assert nav["success"], nav
            assert "Example Domain" in nav["content"], nav["content"]
        finally:
            await mcp.call_tool("tabs", {"action": "close", "tab_id": tab_id})
        self._assert_modern(mcp)
