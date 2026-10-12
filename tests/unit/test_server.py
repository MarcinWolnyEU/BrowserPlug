"""
Unit tests for WebSocket server
"""

import pytest
import json
import asyncio
import logging
from unittest.mock import AsyncMock, patch
from server.server import BrowserPlugServer

class TestBrowserPlugServer:
    
    @pytest.fixture
    def server(self):
        """Create BrowserPlugServer instance"""
        return BrowserPlugServer(host="localhost", port=8772)
    
    def test_server_initialization(self, server):
        """Test server initialization"""
        assert server.host == "localhost"
        assert isinstance(server.port, int)  # Port should be an integer (dynamic in tests)
        assert server.port > 0  # Port should be positive
        assert server.extension_connection is None
    
    @pytest.mark.asyncio
    async def test_handle_extension_connection_success(self, server, mock_websocket):
        """Test successful extension connection handling"""
        async def mock_aiter():
            return
            yield  # Make this a generator

        mock_websocket.__aiter__ = mock_aiter
        mock_websocket.remote_address = ("127.0.0.1", 12345)
        # A local peer on a server WITH an MCP HTTP app must authenticate first
        # (auth_mode -> "bootstrap"). That is the encrypted handshake, covered by
        # test_ws_token.py and test_ws_crypto_interop.py; here it is stood in for, so the
        # registration path below is what gets tested.
        authenticated = AsyncMock(return_value=(True, [json.dumps(
            {"type": "extension_hello", "data": {"tokenBootstrap": True}})], mock_websocket))

        seen = []
        original = server.registry.register
        with patch.object(server.registry, "register",
                          side_effect=lambda ws, **kw: seen.append(ws) or original(ws, **kw)), \
                patch.object(server.ws_auth, "authenticate", authenticated):
            await server.handle_extension_connection(mock_websocket)

        assert seen == [mock_websocket]                # it really was registered
        assert server.extension_connection is None     # ...and cleaned up on the way out
    
    @pytest.mark.asyncio
    async def test_handle_extension_message_response_settles_pending_request(self, server):
        """A 'response' from the extension settles the matching pending future.

        handle_extension_message routes type response/error to
        handle_extension_response, which pops pending_requests[id] and settles
        the future with the FULL message dict via call_soon_threadsafe on the
        future's own loop — the correlation every tool call's reply rides on.
        """
        payload = {
            "id": "test_001",
            "type": "response",
            "action": "tabs.list",
            "data": {"tabs": []}
        }
        future = asyncio.get_running_loop().create_future()
        server.rpc.pending_requests["test_001"] = future

        await server.handle_extension_message(json.dumps(payload))

        # Settled via call_soon_threadsafe -> the wakeup must actually land here.
        result = await asyncio.wait_for(future, timeout=2.0)
        assert result == payload                          # settled with the whole message
        assert "test_001" not in server.rpc.pending_requests  # consumed, not leaked
    
    @pytest.mark.asyncio
    async def test_handle_extension_message_server_health(self, server, mock_websocket):
        """A server.health request gets a proper response echoing the id."""
        server.extension_connection = mock_websocket
        message = json.dumps({
            "id": "health_001",
            "type": "request",
            "action": "server.health",
            "data": {}
        })

        await server.handle_extension_message(message)

        mock_websocket.send.assert_called_once()
        reply = json.loads(mock_websocket.send.call_args[0][0])
        assert reply["id"] == "health_001"          # echoes the request id so the extension promise resolves
        assert reply["type"] == "response"          # NOT "request" — must resolve, not trigger a pong
        assert reply["action"] == "server.health"
        assert reply["data"]["status"] == "ok"
        assert reply["data"]["mcp_port"] == server.mcp_port

    @pytest.mark.asyncio
    @pytest.mark.parametrize("sent,expected", [(False, False), (True, True)])
    async def test_get_connections_honours_kilo_details_off_switch(
            self, server, mock_websocket, sent, expected):
        """server.get_connections must read kilo_details from the request PAYLOAD.

        handle_extension_message binds `data` to the whole envelope
        {id,type,action,data}, so the old `data.get('kilo_details', True)` read a key
        that is never at that level and always fell through to the True default. The
        options-page checkbox was therefore a no-op: every popup poll (2s) still did a
        per-PID open_files() + log tail. The extension really sends it nested --
        see requestFromServer('server.get_connections', {kilo_details: ...}).
        """
        server.extension_connection = mock_websocket
        message = json.dumps({
            "id": "conn_001",
            "type": "request",
            "action": "server.get_connections",
            "data": {"kilo_details": sent},
        })

        with patch.object(server, "get_connections_info",
                          return_value={"connections": []}) as spy:
            await server.handle_extension_message(message)

        spy.assert_called_once_with(kilo_details=expected, viewer_host=None)

    @pytest.mark.asyncio
    async def test_get_connections_kilo_details_defaults_on_when_unsent(
            self, server, mock_websocket):
        """An older extension that sends no kilo_details still gets the full detail."""
        server.extension_connection = mock_websocket
        message = json.dumps({
            "id": "conn_002",
            "type": "request",
            "action": "server.get_connections",
            "data": {},
        })

        with patch.object(server, "get_connections_info",
                          return_value={"connections": []}) as spy:
            await server.handle_extension_message(message)

        spy.assert_called_once_with(kilo_details=True, viewer_host=None)

    @pytest.mark.asyncio
    async def test_server_row_names_the_host_the_CALLER_dialled(self, server, mock_websocket):
        """`ws://localhost:8772` is a lie to a browser on a phone: localhost is the one
        address it can never reach. Only the caller knows which address it dialled (the
        server sees the peer address, not its own destination), so the extension sends it
        and the server row is rendered from it."""
        server.extension_connection = mock_websocket
        message = json.dumps({
            "id": "conn_003",
            "type": "request",
            "action": "server.get_connections",
            "data": {"server_host": "192.168.0.114"},
        })
        await server.handle_extension_message(message)

        reply = json.loads(mock_websocket.send.call_args[0][0])
        row = [r for r in reply["data"]["connections"] if r["kind"] == "server"][0]
        assert f"ws://192.168.0.114:{server.port}" in row["detail"]
        assert f"http://192.168.0.114:{server.mcp_port}" in row["detail"]
        assert "localhost" not in row["detail"]

    @pytest.mark.parametrize("sent", [
        None, "", 42, {}, "not a host!", "evil host", "a" * 81,
        "192.168.0.1 <script>alert(1)</script>",
    ])
    def test_a_hostile_viewer_host_falls_back_to_localhost(self, server, sent):
        """It arrives from an extension, which may be a REMOTE browser nobody trusts, and
        it is rendered in every OTHER browser's popup. Anything that is not host-shaped is
        refused rather than echoed."""
        rows = server.get_connections_info(viewer_host=sent)["connections"]
        detail = [r for r in rows if r["kind"] == "server"][0]["detail"]
        assert detail.startswith(f"ws://localhost:{server.port}")

    def test_an_absent_viewer_host_keeps_the_old_string(self, server):
        """An extension too old to send it must not change what the row has always said."""
        rows = server.get_connections_info()["connections"]
        detail = [r for r in rows if r["kind"] == "server"][0]["detail"]
        assert detail == (f"ws://localhost:{server.port}  +  "
                          f"http://localhost:{server.mcp_port}/mcp")

    @pytest.mark.asyncio
    async def test_replaced_connection_does_not_null_successor(self, server):
        """A departing WS handler must not clear a successor that already replaced it.

        Reproduces the 'No extension connection available' race: connection B takes
        over the single slot while connection A's handler is still running; A's
        finally must NOT null B's registration (else the live socket can receive but
        not be sent to).
        """
        sock_a = AsyncMock()
        sock_a.remote_address = ("127.0.0.1", 1111)
        sock_a.close_code = None
        # This server has an MCP HTTP app, so a local peer is authenticated before it is
        # registered (auth_mode -> "bootstrap"). The encrypted handshake is tested
        # elsewhere; here it is stood in for.
        authenticated = AsyncMock(return_value=(True, [json.dumps(
            {"type": "extension_hello", "data": {"tokenBootstrap": True}})], sock_a))
        sock_b = AsyncMock()

        async def a_stream():
            # Mid-stream, connection B arrives and becomes the registered connection.
            server.extension_connection = sock_b
            server.extension_version = "from-B"
            return
            yield  # make this an async generator (never actually yields)
        sock_a.__aiter__ = lambda *args: a_stream()

        with patch.object(server, "_probe_browser_identity", return_value={}), \
             patch.object(server, "_notify_connection_waiters"), \
             patch.object(server.ws_auth, "authenticate", authenticated):
            await server.handle_extension_connection(sock_a)

        # A's finally ran, but B is preserved because A was no longer the current socket.
        assert server.extension_connection is sock_b
        assert server.extension_version == "from-B"

    @pytest.mark.asyncio
    async def test_handle_extension_message_server_restart(self, server, mock_websocket):
        """A server.restart request acks over the WS and schedules the restart thread."""
        server.extension_connection = mock_websocket
        message = json.dumps({
            "id": "restart_001",
            "type": "request",
            "action": "server.restart",
            "data": {}
        })

        # Patch the thread so the real body (_spawn_restart_process + os._exit, which
        # would kill pytest) never runs — just assert it was scheduled and started.
        with patch("server.runtime.lifecycle.threading.Thread") as MockThread:
            await server.handle_extension_message(message)
            MockThread.assert_called_once()
            MockThread.return_value.start.assert_called_once()

        mock_websocket.send.assert_called_once()
        reply = json.loads(mock_websocket.send.call_args[0][0])
        assert reply["id"] == "restart_001"           # echoes id so the extension promise resolves
        assert reply["type"] == "response"
        assert reply["action"] == "server.restart"
        assert reply["data"]["status"] == "restarting"

    @pytest.mark.asyncio
    async def test_handle_extension_message_invalid_json(self, server, mock_websocket, caplog):
        """Invalid JSON is swallowed by the JSONDecodeError branch: the specific
        'Invalid JSON received' error is logged, and NO reply goes on the wire."""
        server.extension_connection = mock_websocket
        invalid_message = "invalid json{"

        with caplog.at_level(logging.ERROR, logger="server.server"):
            await server.handle_extension_message(invalid_message)

        assert any("Invalid JSON received" in r.getMessage() for r in caplog.records)
        mock_websocket.send.assert_not_called()
    
    @pytest.mark.asyncio
    async def test_send_to_extension_no_connection(self, server):
        """Test sending message when no extension connected"""
        message = {"type": "request", "action": "test"}
        
        result = await server.rpc.send_to_extension(message)
        
        assert result is False
    
    @pytest.mark.asyncio
    async def test_send_to_extension_success(self, server, mock_websocket):
        """Test successful message sending to extension"""
        server.extension_connection = mock_websocket
        message = {"type": "request", "action": "test"}
        
        result = await server.rpc.send_to_extension(message)
        
        assert result is True
        mock_websocket.send.assert_called_once()
        
        # Verify message has timestamp added
        sent_message = json.loads(mock_websocket.send.call_args[0][0])
        assert "timestamp" in sent_message
        assert sent_message["type"] == "request"
        assert sent_message["action"] == "test"
    
    @pytest.mark.asyncio
    async def test_send_to_extension_error(self, server, mock_websocket):
        """Test error handling when sending to extension"""
        server.extension_connection = mock_websocket
        mock_websocket.send.side_effect = Exception("Send failed")
        
        message = {"type": "request", "action": "test"}
        result = await server.rpc.send_to_extension(message)
        
        assert result is False