"""
Integration tests for MCP server functionality
Tests MCP server integration with WebSocket communication
"""

import pytest
import socket
import uuid
from datetime import datetime
from unittest.mock import Mock

from facade_double import FacadeDouble
from server.server import BrowserPlugServer
from server.tools.mcp_tools import BrowserPlugTools
from port_coordinator import get_port_by_type


class TestMCPIntegration:
    """Test MCP server integration"""
    
    @pytest.fixture
    def mock_websocket_server(self):
        """Create mock WebSocket server for testing"""
        class MockServer(FacadeDouble):
            def __init__(self):
                super().__init__()
                self.pending_requests = {}
                self.sent_messages = []
            
            async def send_to_extension(self, message):
                self.sent_messages.append(message)
                return True
            
            async def send_request_and_wait(self, request, timeout=10.0, browser=None):
                # Mock successful responses for different actions
                action = request.get("action", "")
                request_id = request.get("id")
                
                mock_responses = {
                    "tabs.list": {
                        "data": {
                            "tabs": [
                                {"id": 1, "url": "https://example.com", "title": "Example", "active": True},
                                {"id": 2, "url": "https://test.com", "title": "Test", "active": False}
                            ]
                        }
                    },
                    "tabs.create": {
                        "data": {
                            "tab": {"id": 3, "url": "https://newsite.com", "title": "New Site", "active": True}
                        }
                    },
                    "console.read": {
                        "data": {
                            "tabId": 1,
                            "total": 1,
                            "entries": [
                                {"level": "error", "text": "boom", "ts": 1735732800000}
                            ]
                        }
                    }
                }
                
                if action in mock_responses:
                    return {
                        "id": request_id,
                        "type": "response", 
                        "action": action,
                        **mock_responses[action],
                        "timestamp": datetime.now().isoformat()
                    }
                else:
                    return {
                        "id": request_id,
                        "type": "error",
                        "action": action,
                        "data": {"error": f"Unknown action: {action}"},
                        "timestamp": datetime.now().isoformat()
                    }
        
        return MockServer()
    
    def test_mcp_tools_initialization(self, mock_websocket_server):
        """Test that MCP tools initialize correctly"""
        mcp_tools = BrowserPlugTools(mock_websocket_server)
        
        assert mcp_tools is not None
        assert mcp_tools.websocket_server == mock_websocket_server
        assert mcp_tools.mcp is not None
        assert hasattr(mcp_tools, 'get_mcp_app')
        
        mcp_app = mcp_tools.get_mcp_app()
        assert mcp_app is not None
    
    def test_server_with_mcp_integration(self):
        """Test server initialization with MCP integration"""
        # Coordinator-allocated ports (never hardcoded literals — those bypass the port
        # coordinator and can collide with a concurrent test or the dev's live server).
        ws_port = get_port_by_type('test_individual')
        mcp_port = get_port_by_type('test_mcp_individual')
        server = BrowserPlugServer(port=ws_port, mcp_port=mcp_port)
        
        assert server.mcp_tools is not None
        assert server.mcp_app is not None
        assert server.mcp_port == mcp_port
        assert hasattr(server, 'start_mcp_server')
    
    @pytest.mark.asyncio
    async def test_mcp_tool_call_simulation(self, mock_websocket_server):
        """Test the consolidated 'tabs' tool end-to-end via the MCP client harness.

        The per-verb tabs_list tool is gone — listing is now tabs(action='list'),
        which the harness invokes as call_tool('tabs', {}) (action defaults to 'list').
        Internally that issues the 'tabs.list' WebSocket protocol action.
        """
        from mcp_client_harness import DirectMCPTestClient

        mcp_tools = BrowserPlugTools(mock_websocket_server)
        client = DirectMCPTestClient(mcp_tools)
        await client.connect()

        result = await client.call_tool("tabs", {})

        assert result["success"], f"tabs(action='list') should succeed: {result}"
        content = result["content"]
        assert isinstance(content, str), "tabs should return a formatted string"
        # The mock returns two tabs; both should be rendered into the listing
        assert "https://example.com" in content
        assert "https://test.com" in content

        await client.disconnect()
    
    @pytest.mark.asyncio  
    async def test_error_handling_in_mcp_tools(self, mock_websocket_server):
        """A wire action neither extension handles is refused loudly, not answered.

        FacadeDouble (review Q4) holds every scripted server to what the real one and a
        real extension do: an id-less request raises, and an action with no `case` in
        both background.js files fails the test. This used to expect the mock's own
        canned "Unknown action" error reply, which is exactly the answer that hid a
        misspelt action; the mock's fallback branch is now unreachable by design.
        """
        BrowserPlugTools(mock_websocket_server)

        request = {
            "id": str(uuid.uuid4()),
            "type": "request",
            "action": "unknown.action",
            "data": {},
            "timestamp": datetime.now().isoformat()
        }
        with pytest.raises(AssertionError, match="extensions do not handle"):
            await mock_websocket_server.send_request_and_wait(request)

        no_id = {"type": "request", "action": "tabs.list", "data": {}}
        with pytest.raises(ValueError, match="must have an ID"):
            await mock_websocket_server.send_request_and_wait(no_id)
    
    @pytest.mark.asyncio
    async def test_different_tool_categories(self, mock_websocket_server):
        """Test different categories of MCP tools"""
        mcp_tools = BrowserPlugTools(mock_websocket_server)
        
        # Test each tool category
        test_actions = [
            "tabs.list",
            "tabs.create",
            "console.read"
        ]
        
        for action in test_actions:
            request = {
                "id": str(uuid.uuid4()),
                "type": "request",
                "action": action,
                "data": {},
                "timestamp": datetime.now().isoformat()
            }
            
            response = await mock_websocket_server.send_request_and_wait(request)
            
            assert response["type"] == "response", f"Action {action} should return response"
            assert response["action"] == action
            assert "data" in response


class TestMCPServerConfiguration:
    """Test MCP server configuration options"""
    
    # Serial phase (pytest.ini `timing`): a server given no MCP port walks the production
    # range 2830..2942, the range a live launcher seeks, and this test then binds the port
    # it picked.
    @pytest.mark.timing
    def test_default_ports(self):
        """A server given no MCP port picks a FREE one for itself.

        This used to assert `server.mcp_port == get_port_by_type('mcp')` — i.e. that
        PRODUCTION code reached into the TEST port coordinator. It did (server.py put
        tests/ on sys.path), which is a defect, not a contract: a release ships no tests/,
        and a live server could relocate itself into the test port pool. The server now
        scans for its own free port, so the real contract is: a usable, unoccupied port.
        """
        server = BrowserPlugServer(start_mcp=False)

        assert isinstance(server.port, int) and server.port > 0
        assert isinstance(server.mcp_port, int) and server.mcp_port > 0
        # and it must be genuinely free — not a port something else is already serving
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            probe.bind(("localhost", server.mcp_port))  # raises if it is in use
    
    def test_custom_ports(self):
        """An explicitly requested port is honored.

        The MCP port used to be hard-coded 4000 here, which passed only because the old
        probe set SO_REUSEADDR and therefore reported an OCCUPIED port as free (something
        else on this box listens on 4000). With an honest probe the server correctly
        relocates off a busy port, so the test was asserting the lie. Ask the allocator for
        a port that is genuinely free, and assert the real contract.
        """
        test_port = get_port_by_type('test_individual')
        mcp_port = get_port_by_type('test_mcp_individual')
        server = BrowserPlugServer(port=test_port, mcp_port=mcp_port)

        assert server.port == test_port
        assert server.mcp_port == mcp_port
    
    def test_mcp_components_initialized(self):
        """Test that MCP components are properly initialized"""
        # A coordinator port: given none, the server would walk the production range.
        server = BrowserPlugServer(start_mcp=False,
                                   mcp_port=get_port_by_type('test_mcp_individual'))
        
        # MCP tools should be initialized
        assert hasattr(server, 'mcp_tools')
        assert server.mcp_tools is not None
        
        # MCP app should be available
        assert hasattr(server, 'mcp_app') 
        assert server.mcp_app is not None
        
        # Should have MCP server startup method
        assert hasattr(server, 'start_mcp_server')
        assert callable(server.start_mcp_server)


class TestMCPToolStructure:
    """Test MCP tool structure and organization"""
    
    def test_tool_categories_exist(self):
        """Test that all expected tool categories are set up"""
        mock_server = Mock()
        mcp_tools = BrowserPlugTools(mock_server)

        # Check that setup methods exist for all categories (consolidated 9-tool surface)
        expected_setup_methods = [
            '_setup_browser_tools',   # tabs, navigate
            '_setup_page_tools',      # page_read/snapshot/act/eval/screenshot/console
            '_setup_data_tools',      # requests, history
            '_setup_server_tools',    # server_info
        ]

        for method_name in expected_setup_methods:
            assert hasattr(mcp_tools, method_name), f"Missing setup method: {method_name}"
            assert callable(getattr(mcp_tools, method_name))

        # The pre-2.0 per-area setup methods must NOT exist anymore
        removed_setup_methods = [
            '_setup_window_tools',
            '_setup_tab_tools',
            '_setup_page_interaction_tools',
            '_setup_history_tools',
            '_setup_request_monitoring_tools',
        ]
        for method_name in removed_setup_methods:
            assert not hasattr(mcp_tools, method_name), \
                f"Removed setup method still present: {method_name}"
    
    def test_mcp_app_creation(self):
        """Test MCP application creation"""
        mock_server = Mock()
        mcp_tools = BrowserPlugTools(mock_server)
        
        mcp_app = mcp_tools.get_mcp_app()
        
        # Should return the low-level MCP SDK server
        from mcp.server.lowlevel import Server
        assert isinstance(mcp_app, Server)


if __name__ == "__main__":
    # Run tests directly if needed
    pytest.main([__file__, "-v"])