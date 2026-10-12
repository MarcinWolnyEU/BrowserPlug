"""
MCP Client Test Harness
A real MCP client that connects to the BrowserPlug server and makes tool calls.

Targets the consolidated BrowserPlug tool surface (9 tools: tabs, navigate, page_read,
page_snapshot, page_act, page_eval, page_screenshot, requests, server_info — plus
`browsers` while two or more are connected). Tools return formatted strings except
page_screenshot, which carries an inline image.

Every call goes through the MCP SDK's `Client`, so it takes the same road an agent's call
takes: JSON-RPC over the handshake era by default (`mode="legacy"`, initialize and all,
which is what the stdio relay speaks), or over 2026-07-28 with `mode=MODERN_MODE`,
pydantic argument validation (a JSON list of
dicts becomes `ActStep` models here, exactly as it does on the wire), the server's
tools/call dispatch (the outcome policy turns a recorded failure into `isError`), and
result serialization. By default the client is bound in-process to the tools' MCP server;
pass `mcp_url` to run the same calls over real Streamable HTTP against a started server's
`/mcp`, which adds the ASGI wrapper and the HTTP auth gate.
"""

import re
from contextlib import asynccontextmanager
from typing import Dict, Any, List, Optional

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import MCP_DEFAULT_SSE_READ_TIMEOUT, MCP_DEFAULT_TIMEOUT
from mcp_types import CallToolResult, TextContent

from server.tools import tool_outcome

# `mode=` for the 2026-07-28 era: no initialize, one self-contained request per call.
MODERN_MODE = "2026-07-28"


# The server reports a pydantic failure to bind the arguments as an isError result whose
# text starts "N validation error(s) for call[tool]". The old harness let the equivalent
# TypeError propagate, and that stays right: wrong arguments are a bug in the TEST, and
# must not read as a tool error the test then "expects".
_ARGUMENT_ERROR = re.compile(r"^\d+ validation errors? for call\[")


class ToolArgumentError(TypeError):
    """The server rejected the call's arguments before the tool body ran."""


class DirectMCPTestClient:
    """
    Test-side MCP client. The name is historical: it used to call the tool function
    directly, below the protocol. It now opens an MCP client per call (an
    `async with` held across pytest fixtures would enter and exit in different tasks).
    """

    def __init__(self, mcp_tools_instance, mcp_url: Optional[str] = None,
                 mode: str = "legacy"):
        self.mcp_tools = mcp_tools_instance
        self.mcp_url = mcp_url
        self.mode = mode
        # The protocol version each session negotiated, so a test can prove which era ran.
        self.protocol_versions: List[str] = []
        self.connected = False

    @asynccontextmanager
    async def _client(self):
        """One MCP session in `self.mode` (the handshake era unless a test asks otherwise)."""
        if not self.mcp_url:
            async with Client(self.mcp_tools.get_mcp_app(), mode=self.mode) as client:
                self.protocol_versions.append(client.protocol_version)
                yield client
            return
        # trust_env=False: a proxy set in the environment must not swallow loopback. The
        # timeouts are the SDK's own client defaults (30 s, 300 s read): httpx2's default
        # 5 s would cut off any tool call that takes longer, since streamable_http_client
        # sets no timeout on a client it is handed.
        async with httpx2.AsyncClient(
                trust_env=False,
                timeout=httpx2.Timeout(MCP_DEFAULT_TIMEOUT, read=MCP_DEFAULT_SSE_READ_TIMEOUT)) as http:
            async with Client(streamable_http_client(self.mcp_url, http_client=http),
                              mode=self.mode) as client:
                self.protocol_versions.append(client.protocol_version)
                yield client

    async def connect(self) -> bool:
        """Mark the client usable (each call opens its own MCP session)."""
        self.connected = True
        return True

    async def list_tools(self) -> List[str]:
        """List available tool names as an MCP client sees them (tools/list)."""
        async with self._client() as client:
            listed = await client.list_tools()
        return [t.name for t in listed.tools]

    async def call_tool(self, tool_name: str, arguments: Dict[str, Any] = None) -> Dict[str, Any]:
        """Call an MCP tool through the MCP protocol.

        Returns the tool's output under 'content': the text (a str) when the result
        holds only text blocks, or a `CallToolResult` carrying the blocks when it has
        anything else (page_screenshot's inline image).

        'success'/'isError' mirror the protocol's own isError flag, which the server sets
        from the tool's structured outcome. A degraded success (truncated, partial steps)
        stays a success and is reported under 'degraded', read from the result's `_meta`.
        An argument-validation rejection is a TEST bug and raises `ToolArgumentError` (a
        TypeError), never a soft failure.
        """
        if not self.connected:
            raise RuntimeError("Not connected")

        if arguments is None:
            arguments = {}

        async with self._client() as client:
            result = await client.call_tool(tool_name, arguments)

        blocks = list(result.content or [])
        text = "".join(b.text for b in blocks if isinstance(b, TextContent))
        text_only = all(isinstance(b, TextContent) for b in blocks)

        if result.is_error:
            if _ARGUMENT_ERROR.match(text):
                raise ToolArgumentError(text)
            if text.startswith("Unknown tool"):
                available = await self.list_tools()
                text = f"Tool '{tool_name}' not found. Available tools: {available}"
            return {
                'content': text,
                'isError': True,
                'success': False,
                'error': text,
            }

        out = {
            'content': text if text_only else CallToolResult(content=blocks),
            'isError': False,
            'success': True,
        }
        degraded = (result.meta or {}).get(tool_outcome.META_KEY)
        if degraded:
            out['degraded'] = degraded
        return out

    async def disconnect(self):
        """Mark the client closed (no session outlives a call)."""
        self.connected = False
