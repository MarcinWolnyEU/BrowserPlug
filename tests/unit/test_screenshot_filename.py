#!/usr/bin/env python3
"""
page_screenshot through the MCP REGISTRATION + DISPATCH path (DirectMCPTestClient).

test_page_tools.py already covers page_screenshot's formatting/save logic in depth
by calling the tool's .fn directly. What it does NOT cover is the harness path this
file owns: resolving the tool by NAME off the registered MCP app and classifying its
return value the way a real client does (a CallToolResult image vs. an "Error ..." string
flagged isError). So this file is deliberately narrow — one test per distinct outcome
of that dispatch, plus the two save branches nothing else pins:

  * CallToolResult([TextContent, ImageContent])  — the success shape
  * "Error capturing screenshot: ..."        — the error-string shape (isError)

(The two save_to branches this file used to pin went with save_to/return_image
themselves on 2026-08-03: both were passed 0 and 1 times respectively in 51 corpus
calls, and agents take screenshots to LOOK at them, not to file them.)

Do not re-add a test whose only distinction is "same assertion as test_page_tools,
but via call_tool" — the tests above already prove the dispatch path.
"""

import pytest
import base64
from unittest.mock import Mock, AsyncMock

from server.tools.mcp_tools import BrowserPlugTools

from mcp_types import CallToolResult, ImageContent, TextContent


# A minimal 1x1 PNG image in base64 (valid PNG header + body)
TEST_PNG_BASE64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="


class TestScreenshotFilename:
    """Test page_screenshot's CallToolResult behavior"""

    @pytest.fixture
    def mock_websocket_server(self):
        """Mock WebSocket server whose screenshot action returns a data URL.

        send_request_and_wait MUST accept the timeout kwarg — page_screenshot
        calls self._request(..., timeout=60.0).
        """
        mock_server = Mock()
        mock_server.send_request_and_wait = AsyncMock(return_value={
            "type": "response",
            "data": {
                "dataUrl": f"data:image/png;base64,{TEST_PNG_BASE64}",
                "format": "png",
                "method": "captureTab",
            }
        })
        return mock_server

    @pytest.fixture
    def mcp_tools(self, mock_websocket_server):
        """Create MCP tools instance with mock server"""
        return BrowserPlugTools(mock_websocket_server)

    @pytest.mark.asyncio
    async def test_page_screenshot_returns_toolresult_image(self, mcp_tools):
        """page_screenshot returns a viewable image: a CallToolResult with an image block."""
        from mcp_client_harness import DirectMCPTestClient

        client = DirectMCPTestClient(mcp_tools)
        await client.connect()
        result = await client.call_tool("page_screenshot", {})
        await client.disconnect()

        assert result["success"], f"page_screenshot should succeed: {result}"
        tool_result = result["content"]
        assert isinstance(tool_result, CallToolResult), \
            f"page_screenshot should return CallToolResult, got {type(tool_result)}"

        # Content carries a text summary and an inline image (not a base64 text blob)
        kinds = [type(c) for c in tool_result.content]
        assert TextContent in kinds, "the result should include a text summary"
        assert ImageContent in kinds, "the result should include an inline image"

        image = next(c for c in tool_result.content if isinstance(c, ImageContent))
        assert image.mime_type.startswith("image/"), "Image content should be an image MIME type"
        assert base64.b64decode(image.data), "Image data should be valid base64"

    @pytest.mark.asyncio
    async def test_page_screenshot_extension_handles_error(self, mock_websocket_server):
        """When the extension returns an error, page_screenshot reports it as a string."""
        from mcp_client_harness import DirectMCPTestClient

        mock_websocket_server.send_request_and_wait = AsyncMock(return_value={
            "type": "error",
            "data": {"message": "no active tab"}
        })
        mcp_tools = BrowserPlugTools(mock_websocket_server)

        client = DirectMCPTestClient(mcp_tools)
        await client.connect()
        result = await client.call_tool("page_screenshot", {})
        await client.disconnect()

        # The tool CAUGHT the extension error and returned it as a string (it did not
        # raise); the harness therefore flags it as an error result, not a success.
        assert result["isError"], f"extension error should be surfaced, not raised: {result}"
        content = result["content"]
        # Error path returns a plain string, not a CallToolResult
        assert isinstance(content, str), "Extension error should be returned as a string"
        assert "Error capturing screenshot" in content
        assert "no active tab" in content

    @staticmethod
    def _summary_text(tool_result):
        """Pull the TextContent summary string out of a CallToolResult."""
        for c in tool_result.content:
            if isinstance(c, TextContent):
                return c.text
        return ""


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
