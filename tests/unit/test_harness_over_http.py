"""tests/mcp_client_harness.py over real Streamable HTTP (`mcp_url=`), the mode the
integration tests use against a started server.

The harness builds its own httpx2 client there, and streamable_http_client sets no timeout
on a client it is handed, so the harness's choice is the only one. httpx2's default is 5 s,
which cut off any tool call that took longer (a slow navigate, a page_act wait).
"""

import asyncio

import httpx2
import pytest
import uvicorn
from mcp.shared._httpx_utils import MCP_DEFAULT_SSE_READ_TIMEOUT, MCP_DEFAULT_TIMEOUT

import mcp_client_harness
import test_imports  # noqa: F401  (path setup)
from mcp_client_harness import DirectMCPTestClient
from server.tools.mcp_server import BrowserPlugMCPServer
from server.tools.tool_registry import ToolRegistry, ToolSpec


async def ping() -> str:
    return "pong"


@pytest.fixture
async def mcp_url():
    """A loopback uvicorn on an ephemeral port serving the production app."""
    app = BrowserPlugMCPServer(ToolRegistry([ToolSpec.from_function(ping)])).http_app()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning",
                                           lifespan="on"))
    task = asyncio.create_task(server.serve())
    for _ in range(500):
        if server.started or task.done():
            break
        await asyncio.sleep(0.01)
    assert server.started, "uvicorn did not start"
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 10)


async def test_the_harness_http_client_waits_as_long_as_the_sdks_own(mcp_url, monkeypatch):
    built = []

    class Recording(httpx2.AsyncClient):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            built.append(self)

    monkeypatch.setattr(mcp_client_harness.httpx2, "AsyncClient", Recording)
    client = DirectMCPTestClient(None, mcp_url=mcp_url)
    await client.connect()
    assert await client.list_tools() == ["ping"]
    result = await client.call_tool("ping")
    assert result["success"] is True and result["content"] == "pong"

    assert len(built) == 2, "one client per harness session, and these two carried the calls"
    for http in built:
        assert http.timeout == httpx2.Timeout(MCP_DEFAULT_TIMEOUT, read=MCP_DEFAULT_SSE_READ_TIMEOUT)
        assert http.trust_env is False, "an environment proxy must not swallow loopback"
