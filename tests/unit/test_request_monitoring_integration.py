"""
Integration tests for web request monitoring
Tests the complete flow from MCP client to extension communication
through the consolidated requests(action=...) tool.
"""

import pytest
import json
import asyncio
from datetime import datetime

from facade_double import FacadeDouble
from server.tools.mcp_tools import BrowserPlugTools


class TestRequestMonitoringIntegration:
    """Integration tests for the consolidated requests tool"""

    @pytest.fixture
    def mock_extension_server(self):
        """Create mock extension server that simulates Firefox extension responses"""
        class MockExtensionServer(FacadeDouble):
            def __init__(self):
                super().__init__()
                self.pending_requests = {}
                self.sent_messages = []
                self.mock_responses = {
                    "requests.recent": {
                        "type": "response",
                        "data": {
                            "tabId": 123,
                            "total": 2,
                            "returned": 2,
                            "requests": [
                                {
                                    "method": "GET",
                                    "url": "https://api.example.com/posts",
                                    "type": "xmlhttprequest",
                                    "status": 200,
                                    "durMs": 180,
                                    "ageMs": 2000
                                },
                                {
                                    "method": "POST",
                                    "url": "https://api.example.com/users",
                                    "type": "fetch",
                                    "status": 201,
                                    "durMs": 245,
                                    "ageMs": 800
                                }
                            ]
                        }
                    },
                    "requests.start_monitoring": {
                        "type": "response",
                        "data": {
                            "monitor_id": "mon_integration_test",
                            "status": "active",
                            "started_at": datetime.now().isoformat(),
                            "url_patterns": ["*"],
                            "options": {
                                "capture_request_bodies": True,
                                "capture_response_bodies": True,
                                "max_body_size": 50000
                            }
                        }
                    },
                    "requests.stop_monitoring": {
                        "type": "response",
                        "data": {
                            "monitor_id": "mon_integration_test",
                            "status": "stopped",
                            "stopped_at": datetime.now().isoformat(),
                            "total_requests_captured": 25,
                            "statistics": {
                                "duration_seconds": 30,
                                "requests_per_second": 0.83,
                                "total_data_size": 1024000
                            }
                        }
                    },
                    "requests.list_captured": {
                        "type": "response",
                        "data": {
                            "monitor_id": "mon_integration_test",
                            "total_requests": 3,
                            "requests": [
                                {
                                    "request_id": "req_int_001",
                                    "timestamp": "2025-01-15T10:30:15.123Z",
                                    "url": "https://api.example.com/users",
                                    "method": "POST",
                                    "status_code": 201,
                                    "duration_ms": 245,
                                    "request_size": 89,
                                    "response_size": 156,
                                    "content_type": "application/json",
                                    "tab_id": 123
                                },
                                {
                                    "request_id": "req_int_002",
                                    "timestamp": "2025-01-15T10:30:18.456Z",
                                    "url": "https://api.example.com/posts",
                                    "method": "GET",
                                    "status_code": 200,
                                    "duration_ms": 180,
                                    "request_size": 0,
                                    "response_size": 2048,
                                    "content_type": "application/json",
                                    "tab_id": 123
                                },
                                {
                                    "request_id": "req_int_003",
                                    "timestamp": "2025-01-15T10:30:22.789Z",
                                    "url": "https://example.org/image.png",
                                    "method": "GET",
                                    "status_code": 200,
                                    "duration_ms": 95,
                                    "request_size": 0,
                                    "response_size": 15360,
                                    "content_type": "image/png",
                                    "tab_id": 123
                                }
                            ]
                        }
                    },
                    "requests.get_content": {
                        "type": "response",
                        "data": {
                            "request_id": "req_int_001",
                            "request_headers": {
                                "Content-Type": "application/json",
                                "Authorization": "Bearer ***",
                                "User-Agent": "Mozilla/5.0 Firefox"
                            },
                            "response_headers": {
                                "Content-Type": "application/json",
                                "Content-Length": "156",
                                "Cache-Control": "no-cache"
                            },
                            "request_body": {
                                "included": True,
                                "content": '{"name": "John Doe", "email": "john@example.org"}',
                                "content_type": "application/json",
                                "encoding": "utf8",
                                "size_bytes": 89,
                                "truncated": False
                            },
                            "response_body": {
                                "included": True,
                                "content": '{"id": 123, "name": "John Doe", "email": "john@example.org", "created_at": "2025-01-15T10:30:15Z"}',
                                "content_type": "application/json",
                                "encoding": "utf8",
                                "size_bytes": 156,
                                "truncated": False
                            }
                        }
                    }
                }

            async def send_request_and_wait(self, request, timeout=10.0, browser=None):
                """Mock request/response simulation (accepts the timeout kwarg)"""
                self.sent_messages.append(request)
                action = request.get("action", "")
                request_id = request.get("id")

                # Simulate network delay
                await asyncio.sleep(0.01)

                if action in self.mock_responses:
                    response = self.mock_responses[action].copy()
                    response["id"] = request_id
                    return response

                # Default response for unknown actions
                return {
                    "id": request_id,
                    "type": "response",
                    "data": {"mock": True, "action": action}
                }

            def set_mock_response(self, action, response):
                """Override mock response for specific action"""
                self.mock_responses[action] = response

        return MockExtensionServer()

    @pytest.fixture
    def mcp_tools(self, mock_extension_server):
        """Create BrowserPlugTools with mock extension server"""
        return BrowserPlugTools(mock_extension_server)

    @staticmethod
    async def _requests_fn(mcp_tools):
        """Look up the consolidated requests tool by name"""
        return ({t.name: t for t in await mcp_tools.mcp.list_tools()})["requests"].fn

    @pytest.mark.asyncio
    async def test_complete_monitoring_workflow(self, mcp_tools, mock_extension_server):
        """Test complete workflow: start -> list -> get -> stop via one tool"""

        requests_tool = await self._requests_fn(mcp_tools)

        # Step 1: Start monitoring
        start_result = await requests_tool(
            action="start",
            url_patterns=["https://api.example.com/*", "https://example.org/*"],
            options={
                "capture_request_bodies": True,
                "capture_response_bodies": True,
                "max_body_size": 100000
            }
        )

        start_data = json.loads(start_result)
        assert "monitor_id" in start_data
        assert start_data["status"] == "active"
        monitor_id = start_data["monitor_id"]

        # Verify start request was sent correctly
        start_request = mock_extension_server.sent_messages[0]
        assert start_request["action"] == "requests.start_monitoring"
        assert "https://api.example.com/*" in start_request["data"]["url_patterns"]
        assert start_request["data"]["options"]["max_body_size"] == 100000
        # Defaults are merged under the caller's options. Set-Cookie is in the default
        # list because get_content returns the RESPONSE headers — that is the header
        # that would otherwise hand a session cookie to the agent.
        assert start_request["data"]["options"]["sensitive_headers"] == [
            "Authorization", "Cookie", "Set-Cookie"]

        # Step 2: List captured requests
        list_result = await requests_tool(action="list", monitor_id=monitor_id)
        list_data = json.loads(list_result)

        assert list_data["monitor_id"] == monitor_id
        assert list_data["total_requests"] == 3
        assert len(list_data["requests"]) == 3

        # Verify we have different types of requests
        request_methods = [req["method"] for req in list_data["requests"]]
        assert "POST" in request_methods
        assert "GET" in request_methods

        request_types = [req["content_type"] for req in list_data["requests"]]
        assert "application/json" in request_types
        assert "image/png" in request_types

        # Step 3: Get content for specific request
        json_request = next(req for req in list_data["requests"] if req["content_type"] == "application/json")
        content_result = await requests_tool(
            action="get",
            monitor_id=monitor_id,
            request_id=json_request["request_id"],
            include_binary=True
        )

        content_data = json.loads(content_result)
        assert content_data["request_id"] == json_request["request_id"]
        assert "request_headers" in content_data
        assert "response_headers" in content_data
        assert content_data["request_body"]["included"] is True
        assert "John Doe" in content_data["request_body"]["content"]

        # Step 4: Stop monitoring
        stop_result = await requests_tool(action="stop", monitor_id=monitor_id)

        stop_data = json.loads(stop_result)
        assert stop_data["monitor_id"] == monitor_id
        assert stop_data["status"] == "stopped"
        assert stop_data["total_requests_captured"] == 25
        assert "statistics" in stop_data

        # Verify all steps sent correct requests
        assert len(mock_extension_server.sent_messages) == 4
        actions = [msg["action"] for msg in mock_extension_server.sent_messages]
        expected_actions = [
            "requests.start_monitoring",
            "requests.list_captured",
            "requests.get_content",
            "requests.stop_monitoring"
        ]
        assert actions == expected_actions

        # Stop sends the monitor id and nothing else: the old `drain_timeout` was a dead
        # wire param (the extension retires the monitor immediately; there is no drain).
        stop_request = mock_extension_server.sent_messages[3]
        assert stop_request["data"] == {"monitor_id": monitor_id}

    @pytest.mark.asyncio
    async def test_recent_request_log(self, mcp_tools, mock_extension_server):
        """Test the retroactive rolling log path (action='recent')"""

        requests_tool = await self._requests_fn(mcp_tools)

        result = await requests_tool(
            action="recent",
            tab_id=123,
            filter="api\\.example\\.com",
            limit=10
        )

        # Payload mapping (snake_case args -> camelCase wire keys)
        sent_request = mock_extension_server.sent_messages[0]
        assert sent_request["action"] == "requests.recent"
        assert sent_request["data"] == {
            "limit": 10,
            "includeStatic": False,
            "tabId": 123,
            "filter": "api\\.example\\.com"
        }

        # Formatted output
        lines = result.split("\n")
        assert lines[0] == "Tab 123: showing 2 of 2 logged requests (newest last):"
        assert lines[1] == "- GET https://api.example.com/posts -> 200 (180ms) [xmlhttprequest] 2.0s ago"
        assert lines[2] == "- POST https://api.example.com/users -> 201 (245ms) [fetch] 800ms ago"

    @pytest.mark.asyncio
    async def test_monitoring_with_tab_filter(self, mcp_tools, mock_extension_server):
        """Test monitoring with tab-specific filtering"""

        # Override mock response to include tab filtering
        mock_extension_server.set_mock_response("requests.start_monitoring", {
            "type": "response",
            "data": {
                "monitor_id": "mon_tab_filtered",
                "status": "active",
                "started_at": datetime.now().isoformat(),
                "url_patterns": ["*"],
                "tab_id": 456,
                "options": {"capture_request_bodies": False}
            }
        })

        requests_tool = await self._requests_fn(mcp_tools)

        result = await requests_tool(
            action="start",
            url_patterns=["*"],
            tab_id=456,
            options={"capture_request_bodies": False}
        )

        data = json.loads(result)
        assert data["tab_id"] == 456

        # Verify tab_id was sent in request
        sent_request = mock_extension_server.sent_messages[0]
        assert sent_request["data"]["tab_id"] == 456
        assert sent_request["data"]["options"]["capture_request_bodies"] is False

    @pytest.mark.asyncio
    async def test_binary_content_handling(self, mcp_tools, mock_extension_server):
        """Test handling of binary content: base64 body, gated on include_binary"""

        # Setup mock response for binary content
        mock_extension_server.set_mock_response("requests.get_content", {
            "type": "response",
            "data": {
                "request_id": "req_binary_test",
                "request_headers": {"Content-Type": "multipart/form-data"},
                "response_headers": {"Content-Type": "image/png", "Content-Length": "15360"},
                "request_body": {
                    "included": True,
                    "content": "LS0tLS1XZWJLaXRGb3JtQm91bmRhcnlabXdNZFZJSUQzWA==",
                    "content_type": "multipart/form-data",
                    "encoding": "base64",
                    "size_bytes": 1024,
                    "truncated": False
                },
                "response_body": {
                    "included": True,
                    "content": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ",
                    "content_type": "image/png",
                    "encoding": "base64",
                    "size_bytes": 15360,
                    "truncated": False
                }
            }
        })

        requests_tool = await self._requests_fn(mcp_tools)

        result = await requests_tool(
            action="get",
            monitor_id="mon_test",
            request_id="req_binary_test",
            include_binary=True
        )

        data = json.loads(result)
        assert data["request_body"]["encoding"] == "base64"
        assert data["response_body"]["encoding"] == "base64"
        assert data["response_body"]["included"] is True
        assert data["response_body"]["content"].startswith("iVBORw0KGgo")

        # The get payload carries include_binary and nothing else beyond the ids
        sent_request = mock_extension_server.sent_messages[0]
        assert sent_request["action"] == "requests.get_content"
        assert sent_request["data"] == {
            "monitor_id": "mon_test",
            "request_id": "req_binary_test",
            "include_binary": True
        }

    @pytest.mark.asyncio
    async def test_error_scenarios(self, mcp_tools, mock_extension_server):
        """Test various error scenarios"""

        requests_tool = await self._requests_fn(mcp_tools)

        # Test invalid monitor_id
        mock_extension_server.set_mock_response("requests.list_captured", {
            "type": "error",
            "data": {"message": "Monitor session not found"}
        })

        result = await requests_tool(action="list", monitor_id="invalid_monitor")
        assert result == "Error listing captured requests: Monitor session not found"

        # Test empty URL patterns — rejected locally, no WS round-trip
        sent_before = len(mock_extension_server.sent_messages)
        result = await requests_tool(action="start", url_patterns=[])
        assert result == 'Error: url_patterns is required for action="start" (e.g. ["*/api/*"])'
        assert len(mock_extension_server.sent_messages) == sent_before

        # Missing ids — rejected locally
        assert await requests_tool(action="stop") == 'Error: monitor_id is required for action="stop"'
        assert (await requests_tool(action="get", monitor_id="mon_x")
                == 'Error: monitor_id and request_id are required for action="get"')
        assert len(mock_extension_server.sent_messages) == sent_before

    @pytest.mark.asyncio
    async def test_monitoring_performance_data(self, mcp_tools, mock_extension_server):
        """Test that performance and timing data is properly captured"""

        requests_tool = await self._requests_fn(mcp_tools)
        result = await requests_tool(action="list", monitor_id="mon_test")

        data = json.loads(result)
        requests = data["requests"]

        # Verify performance data is present
        for request in requests:
            assert "duration_ms" in request
            assert "timestamp" in request
            assert "request_size" in request
            assert "response_size" in request
            assert request["duration_ms"] > 0

        # Check for variety in performance data
        durations = [req["duration_ms"] for req in requests]
        assert len(set(durations)) > 1  # Different requests have different durations

    @pytest.mark.asyncio
    async def test_concurrent_monitoring_requests(self, mcp_tools, mock_extension_server):
        """Test handling of concurrent calls to the requests tool"""

        requests_tool = await self._requests_fn(mcp_tools)

        # Start multiple concurrent requests
        tasks = [
            requests_tool(action="start", url_patterns=["*"]),
            requests_tool(action="list", monitor_id="mon_test"),
            requests_tool(action="get", monitor_id="mon_test", request_id="req_001"),
            requests_tool(action="recent")
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)

        # All should complete without exceptions or error strings
        for result in results:
            assert not isinstance(result, Exception)
            assert isinstance(result, str)
            assert not result.startswith("Error")

        # Verify all requests were sent
        assert len(mock_extension_server.sent_messages) == 4
        sent_actions = {msg["action"] for msg in mock_extension_server.sent_messages}
        assert sent_actions == {
            "requests.start_monitoring",
            "requests.list_captured",
            "requests.get_content",
            "requests.recent"
        }
