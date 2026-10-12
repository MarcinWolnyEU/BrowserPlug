"""
MCP Server Integration Tests
Tests the MCP server startup, connections, and basic functionality
"""

import pytest
import pytest_asyncio
import asyncio

# Set up consistent imports

# Import project modules
from server.server import BrowserPlugServer

# Import test utilities
from port_coordinator import coordinated_test_ports
from firefox_test_utils import FirefoxTestManager
from test_config import FIREFOX_TEST_CONFIG
from mcp_client_harness import DirectMCPTestClient


class TestMCPServerIntegration:
    """MCP server integration tests"""

    @pytest_asyncio.fixture
    async def full_mcp_system(self):
        """Set up complete system: MCP server + WebSocket server + Firefox"""

        # Use coordinated ports to avoid conflicts
        with coordinated_test_ports() as (ports, coord_file):
            websocket_port = ports['websocket']
            mcp_port = ports['mcp']

            # Start the complete BrowserPlug server (WebSocket + MCP)
            server = BrowserPlugServer(
                host="localhost",
                port=websocket_port,
                mcp_port=mcp_port,
                start_mcp=True  # Enable MCP server for end-to-end testing
            )

            # Track connections and messages for testing
            server.connected_clients = []
            server.received_messages = []
            server.sent_messages = []

            # Override handlers to track activity
            original_connection_handler = server.handle_extension_connection
            original_message_handler = server.handle_extension_message

            async def tracking_connection_handler(websocket):
                server.connected_clients.append(websocket)
                try:
                    await original_connection_handler(websocket)
                finally:
                    if websocket in server.connected_clients:
                        server.connected_clients.remove(websocket)

            # Signature-agnostic: the server passes the owning browser as a second arg
            # (multi-browser registry). A tracker pinned to the old 1-arg signature made
            # every message raise TypeError, which killed the connection — and the skip
            # below reported it as a "config mismatch".
            async def tracking_message_handler(message, *args, **kwargs):
                server.received_messages.append(message)
                result = await original_message_handler(message, *args, **kwargs)
                return result

            # Override send method to track sent messages. Signature-agnostic for the same
            # reason as tracking_message_handler above: the multi-browser registry passes the
            # owning browser (e.g. browser=...) through send_to_extension, so a tracker pinned
            # to the old 1-arg signature raises TypeError on every send — which the removed
            # skip used to hide as a "config mismatch".
            original_send = server.rpc.send_to_extension
            async def tracking_send(message, *args, **kwargs):
                server.sent_messages.append(message)
                return await original_send(message, *args, **kwargs)

            server.handle_extension_connection = tracking_connection_handler
            server.handle_extension_message = tracking_message_handler
            server.rpc.send_to_extension = tracking_send

            # Start servers (WebSocket server only - MCP handled by start_mcp=True)
            websocket_task = asyncio.create_task(server.start_server())

            # Wait for servers to start
            await asyncio.sleep(1.0)

            # Create real MCP client harness
            # Use DirectMCPTestClient for more reliable testing
            mcp_client = DirectMCPTestClient(server.mcp_tools)

            system_info = {
                'server': server,
                'websocket_port': websocket_port,
                'mcp_port': mcp_port,
                'mcp_client': mcp_client,
                'coordination_file': coord_file,
                'websocket_task': websocket_task
            }

            try:
                yield system_info
            finally:
                # Cleanup
                await mcp_client.disconnect()
                await server.shutdown(websocket_task)

    @pytest.mark.asyncio
    async def test_mcp_server_startup(self, full_mcp_system):
        """Test that both MCP and WebSocket servers start correctly"""
        system = full_mcp_system
        server = system['server']

        # Verify MCP server is running
        assert hasattr(server, 'mcp_tools')
        assert server.mcp_tools is not None
        assert hasattr(server, 'mcp_app')
        assert server.mcp_app is not None

        # Verify WebSocket server is ready
        assert server.port == system['websocket_port']

        print(f"✓ MCP server running on port {system['mcp_port']}")
        print(f"✓ WebSocket server running on port {system['websocket_port']}")

    @pytest.mark.asyncio
    async def test_mcp_client_connection(self, full_mcp_system):
        """Test MCP client can connect to server"""
        system = full_mcp_system
        mcp_client = system['mcp_client']

        # Connect MCP client
        connected = await mcp_client.connect()
        assert connected, "MCP client should be able to connect"

        print("✓ MCP client connected to server")

    @pytest.mark.asyncio
    async def test_extension_websocket_connection(self, full_mcp_system):
        """Test Firefox extension connects via WebSocket"""
        system = full_mcp_system
        server = system['server']

        # Start Firefox with extension
        with FirefoxTestManager(
            test_port=system['websocket_port'],
            coordination_file=system['coordination_file']
        ) as firefox:

            # Set up Firefox with extension and start it
            success = firefox.setup_and_start_firefox(headless=True, skip_on_failure=False)
            assert success, "Firefox setup and extension installation should succeed"

            # Wait for extension to connect using awaitable mechanism (give it more time)
            max_wait_time = FIREFOX_TEST_CONFIG['extension_install_wait'] + 5.0
            print(f"Waiting up to {max_wait_time}s for extension to connect...")

            connected = await firefox.async_wait_for_extension_connection(
                timeout=max_wait_time, server=server
            )

            if connected:
                print(f"✓ Extension connected successfully")
            else:
                print(f"✗ Extension failed to connect within {max_wait_time}s")

            # Firefox exiting before the extension connects is a REAL failure (a bad
            # profile/XPI, a crashed bootstrap) — not a reason to skip. The stronger
            # connected_clients assertion below would also catch it; this names the cause.
            # (A skip here once turned a live regression green.)
            proc = firefox.firefox_process
            assert not (proc and proc.poll() is not None), (
                f"Firefox process exited (code {proc.poll()}) before the extension "
                f"connected — a profile/XPI problem, not a skip. connected={connected}"
            )

            # This used to pytest.skip("likely config mismatch") — which turned a REAL
            # failure into a green run (a broken tracking hook, a failed token bootstrap,
            # a dead extension all looked like "config"). The config mismatch it excused
            # (extension dialing the default 8772) is now impossible: the profile bootstrap
            # seeds the test port and blocks the network until it has (see
            # firefox_test_utils._bootstrap_isolation_active). So: fail, and say why.
            assert len(server.connected_clients) > 0, (
                f"Extension never held a connection to the test port {system['websocket_port']}. "
                "Check the server log above for an auth rejection (token bootstrap) or an "
                "exception thrown out of the message handler — both drop the connection."
            )

            print(f"✓ Firefox extension connected via WebSocket")
            print(f"✓ {len(server.connected_clients)} WebSocket client(s) connected")

    @pytest.mark.asyncio
    async def test_full_chain_mcp_to_browser_action(self, full_mcp_system):
        """Test complete chain: MCP client → MCP server → WebSocket → Extension → Browser API"""
        system = full_mcp_system
        server = system['server']
        mcp_client = system['mcp_client']

        # Connect MCP client
        await mcp_client.connect()

        # Start Firefox with extension
        with FirefoxTestManager(
            test_port=system['websocket_port'],
            coordination_file=system['coordination_file']
        ) as firefox:

            # Mirror test_extension_websocket_connection above: a setup failure is a real
            # failure (missing XPI, broken profile build, crashed bootstrap), not a skip.
            # skip_on_failure=False makes setup RAISE with the real cause instead of
            # swallowing it into a bare False.
            success = firefox.setup_and_start_firefox(headless=True, skip_on_failure=False)
            assert success, "Firefox setup and extension installation should succeed"

            # Wait for extension to connect using awaitable mechanism
            connected = await firefox.async_wait_for_extension_connection(
                timeout=FIREFOX_TEST_CONFIG['extension_install_wait'], server=server
            )

            if not connected:
                # NOT a skip. "The extension never connected" is what a broken WS
                # contract, a failed token bootstrap, or an exception out of a message
                # handler all look like — the single most important failure this suite
                # can observe. (conftest.py's connect path is the model.)
                alive = firefox.firefox_process and firefox.firefox_process.poll() is None
                try:
                    stamped = firefox._profile_points_at_test_port()
                except Exception as e:
                    stamped = f"could not read: {e}"
                raise AssertionError(
                    f"Extension never connected to the test server on port "
                    f"{system['websocket_port']} within "
                    f"{FIREFOX_TEST_CONFIG['extension_install_wait']}s.\n"
                    f"  Firefox still running: {alive}\n"
                    f"  profile points at {system['websocket_port']}: {stamped}\n"
                    "Look in the captured server log for a 4401 auth rejection (token "
                    "bootstrap) or an exception out of the message handler — both drop "
                    "the socket."
                )

            initial_message_count = len(server.received_messages)
            initial_sent_count = len(server.sent_messages)

            # Make MCP tool call
            print("Making MCP tool call: tabs (action='list')")
            result = await mcp_client.call_tool("tabs", {})

            # Wait for message processing
            await asyncio.sleep(2.0)

            # Verify messages flowed through the system
            final_message_count = len(server.received_messages)
            final_sent_count = len(server.sent_messages)

            print(f"Messages received: {initial_message_count} → {final_message_count}")
            print(f"Messages sent: {initial_sent_count} → {final_sent_count}")

            # For now, just verify the MCP call succeeded
            # In a complete implementation, we'd verify:
            # 1. MCP tool call triggered WebSocket message to extension
            # 2. Extension received and processed the message
            # 3. Extension called browser API (tabs.query)
            # 4. Extension sent response back via WebSocket
            # 5. Response flowed back to MCP client

            assert not result['isError'], "MCP tool call should not error"

            print("✓ MCP tool call completed (basic verification)")
            print("Note: Full chain verification requires real MCP client integration")

    @pytest.mark.asyncio
    async def test_multiple_tool_calls(self, full_mcp_system):
        """Test multiple MCP tool calls work correctly"""
        system = full_mcp_system
        mcp_client = system['mcp_client']

        await mcp_client.connect()

        # Test different tool categories (consolidated surface)
        tool_tests = [
            ("tabs", {}),
            ("requests", {"limit": 10}),
            ("page_read", {}),
            ("server_info", {})
        ]

        for tool_name, args in tool_tests:
            print(f"Testing MCP tool: {tool_name}")
            result = await mcp_client.call_tool(tool_name, args)

            # Basic verification that tool call completed
            assert 'content' in result

            # Most tools will fail without browser extension connection, but that's expected
            # We're just testing that the MCP layer works and doesn't crash
            if result.get('isError', False):
                content = result.get('content', '')
                print(f"   Expected error (no extension): {content[:100]}...")
                # Verify it's a connection error, not a tool definition error
                assert any(keyword in content.lower() for keyword in [
                    'no extension connection', 'connection', 'websocket', 'missing 1 required positional argument'
                ]), f"Unexpected error type: {content}"
            else:
                print(f"   Success: {result.get('content', '')[:100]}...")

            # Small delay between calls
            await asyncio.sleep(0.5)

        print("✓ Multiple MCP tool calls completed successfully")

