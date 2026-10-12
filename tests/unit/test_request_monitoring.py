"""
Unit tests for the consolidated `requests` tool (network traffic inspection)
Tests the MCP tool without WebSocket communication.

Covers:
- action="recent"  -> WS requests.recent (payload mapping + line formatting)
- action="start"   -> WS requests.start_monitoring
- action="stop"    -> WS requests.stop_monitoring
- action="list"    -> WS requests.list_captured
- action="get"     -> WS requests.get_content
- parameter-validation errors (no WS round-trip)
"""

import pytest
import json
import asyncio

from facade_double import FacadeDouble
from server.tools.mcp_tools import BrowserPlugTools


async def _tool_fn(mcp_tools, name):
    """Fetch a tool's underlying coroutine by name (the registry's get_tool)."""
    tool = await mcp_tools.mcp.get_tool(name)
    return tool.fn


class TestRequestsTool:
    """Test the consolidated requests(action=...) MCP tool"""

    @pytest.fixture
    def mock_websocket_server(self):
        """Create mock WebSocket server for testing"""
        class MockServer(FacadeDouble):
            def __init__(self):
                super().__init__()
                self.pending_requests = {}
                self.sent_messages = []
                self.timeouts = []
                self.mock_responses = {}

            def set_mock_response(self, action, response):
                """Set mock response for specific action"""
                self.mock_responses[action] = response

            async def send_request_and_wait(self, request, timeout=10.0, browser=None):
                """Mock WebSocket communication (accepts the timeout kwarg)"""
                action = request.get("action", "")
                self.sent_messages.append(request)
                self.timeouts.append(timeout)

                if action in self.mock_responses:
                    return self.mock_responses[action]

                # Default response
                return {
                    "type": "response",
                    "data": {"mock": True, "action": action}
                }

        return MockServer()

    @pytest.fixture
    def mcp_tools(self, mock_websocket_server):
        """Create BrowserPlugTools instance with mock server"""
        return BrowserPlugTools(mock_websocket_server)

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_requests_tool_is_async(self, mcp_tools):
        """The requests tool is an async function"""
        fn = await _tool_fn(mcp_tools, "requests")
        assert fn is not None
        assert asyncio.iscoroutinefunction(fn)

    # ------------------------------------------------------------------
    # action="recent"
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_recent_formats_entries(self, mcp_tools, mock_websocket_server):
        """Entries are rendered as '- METHOD url -> status (durMs) [type]' lines"""
        mock_websocket_server.set_mock_response("requests.recent", {
            "type": "response",
            "data": {
                "tabId": 12,
                "total": 5,
                "returned": 3,
                "requests": [
                    {"method": "GET", "url": "https://api.example.com/users",
                     "type": "xmlhttprequest", "status": 200, "durMs": 245, "ageMs": 1500},
                    {"method": "POST", "url": "https://api.example.com/save",
                     "type": "fetch", "error": "NS_ERROR_ABORT"},
                    {"method": "GET", "url": "https://example.org/app.js",
                     "type": "script", "status": 200, "durMs": 1200,
                     "fromCache": True, "ageMs": 500},
                ],
            },
        })
        requests_tool = await _tool_fn(mcp_tools, "requests")

        result = await requests_tool(action="recent")

        lines = result.split("\n")
        assert lines[0] == "Tab 12: showing 3 of 5 logged requests (newest last):"
        assert lines[1] == "- GET https://api.example.com/users -> 200 (245ms) [xmlhttprequest] 1.5s ago"
        # failed request: status rendered as ERR(<error>), no duration; the peer
        # address is EXPLICITLY unknown, never silently absent
        assert lines[2] == "- POST https://api.example.com/save -> ERR(NS_ERROR_ABORT) [dialed: unknown] [fetch]"
        # cached request: [cache] marker, durations >= 1s rendered in seconds
        assert lines[3] == "- GET https://example.org/app.js -> 200 (1.2s) [cache] [script] 500ms ago"

    @pytest.mark.asyncio
    async def test_recent_empty_log(self, mcp_tools, mock_websocket_server):
        """Empty log returns a 'No requests' message that mentions the filter"""
        mock_websocket_server.set_mock_response("requests.recent", {
            "type": "response",
            "data": {"tabId": 12, "total": 0, "returned": 0, "requests": []}
        })
        requests_tool = await _tool_fn(mcp_tools, "requests")

        result = await requests_tool(action="recent", filter="/api/")

        assert result.startswith("No requests in the log for tab 12")
        assert "matching filter '/api/'" in result

    @pytest.mark.asyncio
    async def test_recent_error_response(self, mcp_tools, mock_websocket_server):
        """Extension errors surface as 'Error reading request log: ...'"""
        mock_websocket_server.set_mock_response("requests.recent", {
            "type": "error",
            "data": {"message": "No such tab"}
        })
        requests_tool = await _tool_fn(mcp_tools, "requests")

        result = await requests_tool(action="recent", tab_id=999)

        assert result == "Error reading request log: No such tab"

    # ------------------------------------------------------------------
    # action="start"
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_start_success(self, mcp_tools, mock_websocket_server):
        """action='start' sends requests.start_monitoring and returns the data as JSON"""
        mock_websocket_server.set_mock_response("requests.start_monitoring", {
            "type": "response",
            "data": {
                "monitor_id": "mon_test123",
                "status": "active",
                "started_at": "2025-01-15T10:30:00.000Z",
                "url_patterns": ["https://api.example.com/*"],
            },
        })
        requests_tool = await _tool_fn(mcp_tools, "requests")

        result = await requests_tool(
            action="start",
            url_patterns=["https://api.example.com/*"],
            options={"max_body_size": 100000},
        )

        response_data = json.loads(result)
        assert response_data["monitor_id"] == "mon_test123"
        assert response_data["status"] == "active"

        assert len(mock_websocket_server.sent_messages) == 1
        sent = mock_websocket_server.sent_messages[0]
        assert sent["action"] == "requests.start_monitoring"
        assert sent["data"]["url_patterns"] == ["https://api.example.com/*"]
        # caller options are merged over the defaults (100000 is within the clamp)
        assert sent["data"]["options"]["max_body_size"] == 100000
        assert sent["data"]["options"]["capture_response_bodies"] is True
        assert sent["data"]["options"]["content_types_to_capture"] == [
            "application/json", "text/plain"]

    @pytest.mark.asyncio
    async def test_start_sends_no_dead_options(self, mcp_tools, mock_websocket_server):
        """Only options the extension reads go on the wire.

        `capture_request_bodies` was never read by the extension (request bodies come
        from the webRequest "requestBody" spec and are always captured) — a flag that
        gates nothing is the dead-wire class GATE 1.8 was opened to kill.
        """
        requests_tool = await _tool_fn(mcp_tools, "requests")

        await requests_tool(action="start", url_patterns=["*/api/*"])

        opts = mock_websocket_server.sent_messages[0]["data"]["options"]
        assert "capture_request_bodies" not in opts
        assert "save_request_body_to" not in opts and "save_response_body_to" not in opts
        assert set(opts) == {"capture_response_bodies", "max_body_size",
                             "content_types_to_capture", "sensitive_headers"}

    @pytest.mark.asyncio
    async def test_start_sensitive_headers_default_redacts_credentials(
            self, mcp_tools, mock_websocket_server):
        """The safety-shaped default actually names every credential-bearing header.

        sensitive_headers is a DISPLAY filter (the extension redacts these from the
        action="get" output), so Set-Cookie — which get_content returns among the
        response headers — must be in the default list, not just the request-side pair.
        """
        requests_tool = await _tool_fn(mcp_tools, "requests")

        await requests_tool(action="start", url_patterns=["*/api/*"])

        opts = mock_websocket_server.sent_messages[0]["data"]["options"]
        assert opts["sensitive_headers"] == ["Authorization", "Cookie", "Set-Cookie"]

        # ...and a caller who explicitly wants them can opt out
        await requests_tool(action="start", url_patterns=["*"],
                            options={"sensitive_headers": []})
        assert mock_websocket_server.sent_messages[1]["data"]["options"][
            "sensitive_headers"] == []

    @pytest.mark.asyncio
    async def test_start_clamps_max_body_size(self, mcp_tools, mock_websocket_server):
        """max_body_size is clamped to 1000..1000000 before it goes on the wire.

        The extension keeps captured bodies under a bounded TOTAL byte budget with
        oldest-first eviction: an absurd per-body cap makes one capture blow the whole
        budget and evict every stored body, including the one just captured.
        """
        requests_tool = await _tool_fn(mcp_tools, "requests")

        async def start_with(size):
            await requests_tool(action="start", url_patterns=["*"],
                                options={"max_body_size": size})
            return mock_websocket_server.sent_messages[-1]["data"]["options"]["max_body_size"]

        assert await start_with(500_000_000) == 1_000_000   # absurdly high -> 1 MB
        assert await start_with(1) == 1_000                 # uselessly low -> 1 KB
        assert await start_with(0) == 1_000                 # 0 would disable capture
        assert await start_with(-5) == 1_000
        assert await start_with(250_000) == 250_000         # in range -> untouched
        assert await start_with("nonsense") == 50_000       # junk -> the default
        assert await start_with(None) == 50_000

    @pytest.mark.asyncio
    async def test_start_without_url_patterns(self, mcp_tools, mock_websocket_server):
        """action='start' without url_patterns is rejected locally (no WS round-trip)"""
        requests_tool = await _tool_fn(mcp_tools, "requests")

        for bad_patterns in (None, []):
            result = await requests_tool(action="start", url_patterns=bad_patterns)
            assert result == 'Error: url_patterns is required for action="start" (e.g. ["*/api/*"])'

        assert len(mock_websocket_server.sent_messages) == 0

    # ------------------------------------------------------------------
    # action="stop"
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_stop_success(self, mcp_tools, mock_websocket_server):
        """action='stop' sends requests.stop_monitoring with the monitor_id ONLY.

        The old `drain_timeout: 5` was dead wire: the extension's stop handler retires
        the monitor immediately (there is no drain), and never read the field.
        """
        mock_websocket_server.set_mock_response("requests.stop_monitoring", {
            "type": "response",
            "data": {
                "monitor_id": "mon_test123",
                "status": "stopped",
                "total_requests_captured": 42,
                "statistics": {"duration_seconds": 300},
            },
        })
        requests_tool = await _tool_fn(mcp_tools, "requests")

        result = await requests_tool(action="stop", monitor_id="mon_test123")

        response_data = json.loads(result)
        assert response_data["monitor_id"] == "mon_test123"
        assert response_data["status"] == "stopped"
        assert response_data["total_requests_captured"] == 42

        sent = mock_websocket_server.sent_messages[0]
        assert sent["action"] == "requests.stop_monitoring"
        assert sent["data"] == {"monitor_id": "mon_test123"}
        assert "drain_timeout" not in sent["data"]

    # ------------------------------------------------------------------
    # action="list"
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_list_success(self, mcp_tools, mock_websocket_server):
        """action='list' sends requests.list_captured and returns the summaries as JSON"""
        mock_websocket_server.set_mock_response("requests.list_captured", {
            "type": "response",
            "data": {
                "monitor_id": "mon_test123",
                "total_requests": 2,
                "requests": [
                    {
                        "request_id": "req_001",
                        "url": "https://api.example.com/users",
                        "method": "POST",
                        "status_code": 201,
                        "duration_ms": 245,
                    },
                    {
                        "request_id": "req_002",
                        "url": "https://api.example.com/posts",
                        "method": "GET",
                        "status_code": 200,
                        "duration_ms": 180,
                    },
                ],
            },
        })
        requests_tool = await _tool_fn(mcp_tools, "requests")

        result = await requests_tool(action="list", monitor_id="mon_test123")

        response_data = json.loads(result)
        assert response_data["total_requests"] == 2
        assert len(response_data["requests"]) == 2
        assert response_data["requests"][0]["request_id"] == "req_001"

        sent = mock_websocket_server.sent_messages[0]
        assert sent["action"] == "requests.list_captured"
        assert sent["data"] == {"monitor_id": "mon_test123"}

    # ------------------------------------------------------------------
    # action="get"
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_get_default_options(self, mcp_tools, mock_websocket_server):
        """action='get' defaults: include_binary False"""
        mock_websocket_server.set_mock_response("requests.get_content", {
            "type": "response",
            "data": {
                "request_id": "req_001",
                "request_headers": {"Content-Type": "application/json"},
                "response_headers": {"Content-Type": "application/json"},
                "request_body": {"included": False, "size_bytes": 89},
                "response_body": {"included": False, "size_bytes": 156},
            },
        })
        requests_tool = await _tool_fn(mcp_tools, "requests")

        result = await requests_tool(
            action="get", monitor_id="mon_test123", request_id="req_001"
        )

        response_data = json.loads(result)
        assert response_data["request_id"] == "req_001"
        assert response_data["request_body"]["included"] is False
        assert response_data["response_body"]["included"] is False

        sent = mock_websocket_server.sent_messages[0]
        assert sent["action"] == "requests.get_content"
        assert sent["data"] == {
            "monitor_id": "mon_test123",
            "request_id": "req_001",
            "include_binary": False,
        }

    @pytest.mark.asyncio
    async def test_get_with_binary(self, mcp_tools, mock_websocket_server):
        """action='get' forwards include_binary; base64 body comes back verbatim"""
        mock_websocket_server.set_mock_response("requests.get_content", {
            "type": "response",
            "data": {
                "request_id": "req_002",
                "request_body": {"included": False, "size_bytes": 0},
                "response_body": {
                    "included": True,
                    "content": "iVBORw0KGgoAAAANSUhEUg==",
                    "content_type": "image/png",
                    "encoding": "base64",
                },
            },
        })
        requests_tool = await _tool_fn(mcp_tools, "requests")

        result = await requests_tool(
            action="get",
            monitor_id="mon_test123",
            request_id="req_002",
            include_binary=True,
        )

        response_data = json.loads(result)
        assert response_data["response_body"]["encoding"] == "base64"
        assert response_data["response_body"]["content"] == "iVBORw0KGgoAAAANSUhEUg=="

        sent = mock_websocket_server.sent_messages[0]
        assert sent["data"] == {
            "monitor_id": "mon_test123",
            "request_id": "req_002",
            "include_binary": True,
        }

    @pytest.mark.asyncio
    async def test_get_without_ids(self, mcp_tools, mock_websocket_server):
        """action='get' requires both monitor_id and request_id"""
        requests_tool = await _tool_fn(mcp_tools, "requests")

        expected = 'Error: monitor_id and request_id are required for action="get"'
        assert await requests_tool(action="get") == expected
        assert await requests_tool(action="get", monitor_id="mon_1") == expected
        assert await requests_tool(action="get", request_id="req_1") == expected
        assert len(mock_websocket_server.sent_messages) == 0

    # ------------------------------------------------------------------
    # Docstring truth (the docstring IS the agent-facing tool description)
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_requests_docstring_is_truthful(self, mcp_tools):
        """Every advertised option must exist, and no stripped/dead one may be advertised."""
        tool = await mcp_tools.mcp.get_tool("requests")
        doc = (tool.description or "") + (tool.fn.__doc__ or "")

        # dead params must leave no trace in the agent-facing description
        for dead in ("drain_timeout", "capture_request_bodies",
                     "save_request_body_to", "save_response_body_to"):
            assert dead not in doc, f"docstring still advertises the dead param {dead}"

        # the real ones, described truthfully
        assert "ttl_seconds" in doc and "10 minutes" in doc
        assert "1000..1000000" in doc            # the max_body_size clamp is stated
        assert "include_binary" in doc and "base64" in doc
        assert "content_types_to_capture" in doc
        # sensitive_headers: a redaction/display filter, NOT a capture filter
        assert "sensitive_headers" in doc
        assert "set-cookie" in doc.lower()
        assert "not a capture filter" in doc
        # ...and it ADDS to a floor it cannot lower. The docstring used to say "pass []
        # to see them verbatim", which the extension has never honoured (redactHeaders
        # seeds its set from ALWAYS_REDACTED_HEADERS unconditionally). An agent
        # debugging an auth failure follows that instruction and is misled.
        assert "pass [] to see them verbatim" not in doc
        assert "no argument reveals them" in doc, \
            "the docstring must say the redaction floor cannot be turned off"

        # The RESPONSE SCHEMA must be described, not just its caveats. A Haiku agent
        # given only this docstring could not answer "what does request_headers == {}
        # mean?" — because nothing here told it the field exists. Every field an agent
        # has to interpret must be named.
        for field in ("request_headers", "response_headers", "request_headers_note",
                      "request_body", "response_body"):
            assert field in doc, f"agents must interpret {field} but the docstring never names it"
        # {} is NOT "no headers were sent" — the ambiguity C-F5 existed to kill.
        assert 'request_headers == {}' in doc and "never means" in doc
        # Headers exist only on "get" — an agent should not hunt for them in "recent".
        assert "Headers appear ONLY here" in doc
        # get's monitor_id is explained
        assert "must belong to that monitor" in doc

    # ------------------------------------------------------------------
    # Error handling / timeout plumbing
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_extension_error_response(self, mcp_tools, mock_websocket_server):
        """A type='error' extension response becomes an 'Error stopping capture: ...' string"""
        mock_websocket_server.set_mock_response("requests.stop_monitoring", {
            "type": "error",
            "data": {"message": "Invalid monitor_id"}
        })
        requests_tool = await _tool_fn(mcp_tools, "requests")

        result = await requests_tool(action="stop", monitor_id="invalid_id")

        assert result == "Error stopping capture: Invalid monitor_id"

    @pytest.mark.asyncio
    async def test_websocket_communication_error(self, mcp_tools, mock_websocket_server):
        """A transport-level {'error': ...} response is surfaced"""
        mock_websocket_server.set_mock_response(
            "requests.start_monitoring", {"error": "Connection failed"})
        requests_tool = await _tool_fn(mcp_tools, "requests")

        result = await requests_tool(action="start", url_patterns=["*"])

        assert result == "Error starting capture: Connection failed"

    @pytest.mark.asyncio
    async def test_timeout_passed_to_send_request_and_wait(self, mcp_tools, mock_websocket_server):
        """BrowserPlugTools._request threads its timeout into send_request_and_wait"""
        mock_websocket_server.set_mock_response("requests.recent", {
            "type": "response",
            "data": {"tabId": 1, "total": 0, "returned": 0, "requests": []}
        })
        requests_tool = await _tool_fn(mcp_tools, "requests")

        await requests_tool(action="recent")

        assert mock_websocket_server.timeouts == [30.0]
