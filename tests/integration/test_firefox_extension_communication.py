"""
Test real communication between BrowserPlugServer and Firefox extension
This test starts Firefox with the extension and verifies WebSocket communication
"""

import pytest
import pytest_asyncio
import json
import asyncio
import websockets
import time
import os

from server.server import BrowserPlugServer
from test_config import TEST_PORTS, FIREFOX_TEST_CONFIG
from firefox_test_utils import FirefoxTestManager
from port_coordinator import get_port_by_type


class TestFirefoxExtensionCommunication:
    """Test real communication with Firefox extension"""

    @pytest.fixture
    def firefox_path(self):
        """Get Firefox path from environment or default"""
        return os.environ.get('FIREFOX_PATH', 'firefox')


    @pytest_asyncio.fixture
    async def running_server(self):
        """Start BrowserPlugServer for testing with fixed Firefox test port"""
        # Use fixed ports for Firefox extension testing
        ports = TEST_PORTS['integration']
        server = BrowserPlugServer(
            host="localhost", 
            port=ports['websocket'],
            mcp_port=ports['mcp'], 
            start_mcp=False  # Disable MCP for extension tests
        )
        server_task = asyncio.create_task(server.start_server())
        
        # Wait for server to start
        await asyncio.sleep(0.5)
        
        server._test_port = ports['websocket']
        yield server

        # Cleanup
        try:
            await server.shutdown(server_task)
        except Exception as e:
            # Log but don't fail on cleanup issues
            pass


    @pytest.mark.asyncio
    async def test_server_starts_before_extension_connects(self, running_server):
        """Test server is ready when extension tries to connect"""
        
        # Server should be running
        assert running_server is not None
        
        # Try to connect as client to verify server is accessible
        try:
            uri = f"ws://localhost:{running_server._test_port}"
            websocket = await websockets.connect(uri)
            
            # Send test message
            test_msg = {
                "id": "server-ready-test",
                "type": "request", 
                "action": "ping",
                "data": {"test": True},
                "timestamp": time.time()
            }
            
            await websocket.send(json.dumps(test_msg))
            await websocket.close()
            
        except Exception as e:
            pytest.fail(f"Server should be accessible: {e}")

    @pytest.mark.asyncio
    async def test_extension_can_connect_to_server(self, running_server):
        """Test that Firefox extension can connect to server using coordinated ports"""

        # Create Firefox test manager with coordinated port
        firefox_manager = FirefoxTestManager(
            firefox_path=os.environ.get('FIREFOX_PATH', 'firefox'),
            test_port=running_server._test_port
        )

        try:
            # skip_on_failure=False: a broken profile build / missing XPI raises its
            # real diagnostic instead of reading as "extension didn't connect".
            success = firefox_manager.setup_and_start_firefox(
                headless=True, skip_on_failure=False
            )
            assert success, "Firefox setup and extension installation should succeed"

            # Ceiling, not a fixed wait — returns as soon as the extension shows up.
            # 10s missed under load once the WS gained token auth; use the same
            # budget conftest uses.
            connection_detected = await firefox_manager.async_wait_for_extension_connection(
                timeout=FIREFOX_TEST_CONFIG['extension_install_wait'], server=running_server
            )
            assert connection_detected, \
                f"Extension should connect to the test server on port {running_server._test_port}"
        finally:
            firefox_manager.cleanup()

    @pytest.mark.asyncio
    async def test_bidirectional_message_flow(self, running_server, firefox_path):
        """Test bidirectional message flow between server and extension"""

        # Create Firefox test manager
        firefox = FirefoxTestManager(
            firefox_path=firefox_path,
            test_port=running_server._test_port
        )

        try:
            # Start Firefox with extension. skip_on_failure=False -> a setup failure
            # raises its real diagnostic instead of masking as a skip.
            success = firefox.setup_and_start_firefox(headless=True, skip_on_failure=False)
            assert success, "Firefox setup and extension installation should succeed"

            # Wait for extension to connect using the new awaitable mechanism
            connected = await firefox.async_wait_for_extension_connection(
                timeout=FIREFOX_TEST_CONFIG['extension_install_wait'], server=running_server
            )
            assert connected, "Extension should connect to the test server"
            
            # Test message handling on server side
            test_messages = [
                {
                    "id": "bidirectional-tabs",
                    "type": "request",
                    "action": "tabs.list",
                    "data": {},
                    "timestamp": time.time()
                },
                {
                    "id": "bidirectional-console",
                    "type": "request",
                    "action": "console.read",
                    "data": {"level": "warn", "since": "nav", "limit": 5},
                    "timestamp": time.time()
                },
                {
                    "id": "bidirectional-response",
                    "type": "response",
                    "action": "tabs.list",
                    "data": {"tabs": []},
                    "timestamp": time.time()
                }
            ]
            
            # Test that server can handle these message types
            for msg in test_messages:
                await running_server.handle_extension_message(json.dumps(msg))
            
            # Test server's send capability to the connected extension
            test_send_msg = {
                "id": "server-to-ext",
                "type": "request",
                "action": "tabs.get_active", 
                "data": {},
                "timestamp": time.time()
            }
            
            result = await running_server.rpc.send_to_extension(test_send_msg)
            # The extension is connected (asserted above), so the send must succeed.
            assert result is True, \
                "send_to_extension should succeed while the extension is connected"
        finally:
            # Cleanup Firefox
            firefox.cleanup()

    @pytest.mark.asyncio
    async def test_extension_message_protocol_compatibility(self, running_server):
        """Test that server handles extension messages according to protocol"""
        
        # Test various message formats that extension might send
        extension_messages = [
            # Successful tab list response
            {
                "id": "ext-tabs-001",
                "type": "response",
                "action": "tabs.list",
                "data": {
                    "tabs": [
                        {"id": 1, "url": "https://example.com", "title": "Example", "active": True},
                        {"id": 2, "url": "https://mozilla.org", "title": "Mozilla", "active": False}
                    ]
                },
                "timestamp": time.time()
            },
            
            # Console read response
            {
                "id": "ext-console-001",
                "type": "response",
                "action": "console.read",
                "data": {
                    "tabId": 1,
                    "entries": [
                        {"level": "error", "text": "boom", "ts": time.time() * 1000}
                    ]
                },
                "timestamp": time.time()
            },
            
            # Error response
            {
                "id": "ext-error-001",
                "type": "error",
                "action": "tabs.close",
                "data": {
                    "error": "Tab not found",
                    "code": 404,
                    "details": "Tab with ID 999 does not exist"
                },
                "timestamp": time.time()
            }
        ]
        
        # Test that server can handle all these message types
        for msg in extension_messages:
            try:
                await running_server.handle_extension_message(json.dumps(msg))
                print(f"✓ Handled {msg['type']} message for action {msg['action']}")
            except Exception as e:
                pytest.fail(f"Server should handle extension message {msg['action']}: {e}")


class TestFirefoxConnectionResilience:
    """Test connection resilience and recovery"""

    @pytest.mark.asyncio
    async def test_server_handles_connection_loss(self):
        """Test server handles connection loss gracefully"""
        # Use individual dynamic ports for resilience testing
        port = get_port_by_type('test_individual')
        mcp_port = get_port_by_type('test_mcp_individual')
        server = BrowserPlugServer(host="localhost", port=port, mcp_port=mcp_port, start_mcp=False)
        
        # Start server
        server_task = asyncio.create_task(server.start_server())
        await asyncio.sleep(0.3)
        
        try:
            # Connect and disconnect multiple times
            for i in range(3):
                websocket = await websockets.connect(f"ws://localhost:{port}")
                
                # Send message
                msg = {
                    "id": f"resilience-{i}",
                    "type": "request",
                    "action": "ping",
                    "data": {},
                    "timestamp": time.time()
                }
                await websocket.send(json.dumps(msg))
                
                # Abruptly close connection
                await websocket.close()
                
                # Brief pause
                await asyncio.sleep(0.1)
            
            # Server should still be running
            assert not server_task.done()
            
        finally:
            await server.shutdown(server_task)

    @pytest.mark.asyncio
    async def test_multiple_connection_attempts(self):
        """Test server can handle multiple connection attempts"""
        # Use individual dynamic ports for multiple connection testing
        port = get_port_by_type('test_individual')
        mcp_port = get_port_by_type('test_mcp_individual')
        server = BrowserPlugServer(host="localhost", port=port, mcp_port=mcp_port, start_mcp=False)
        
        server_task = asyncio.create_task(server.start_server())
        await asyncio.sleep(0.3)
        
        try:
            # Multiple simultaneous connections
            connections = []
            
            for i in range(5):
                websocket = await websockets.connect(f"ws://localhost:{port}")
                connections.append(websocket)
                
                # Send unique message
                msg = {
                    "id": f"multi-conn-{i}",
                    "type": "request", 
                    "action": "tabs.list",
                    "data": {"client_id": i},
                    "timestamp": time.time()
                }
                await websocket.send(json.dumps(msg))
            
            # Close all connections
            for websocket in connections:
                await websocket.close()
                
        finally:
            await server.shutdown(server_task)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])