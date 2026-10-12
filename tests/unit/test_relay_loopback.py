"""L9 and L17: the launcher talks to the shared server over literal 127.0.0.1, and both
transports tell the server which client process spawned them.

L9: "localhost" resolves to ::1 first, but in LAN/WAN mode uvicorn binds 0.0.0.0 only, so
every urllib POST paid a failed IPv6 connect (about 2 s where a firewall drops the SYN).
L17: X-BrowserPlug-Client-Pid was parsed by the server and sent by no launcher.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from browserplug_shared import mcp_port
from launcher import launcher
from launcher.client_pid import client_pid
from launcher.stdio_relay import StdioRelay
from browserplug_shared.version import CLIENT_PID_HEADER


def _relay(port=2830):
    return StdioRelay(port, log=lambda *a: None, out=None, poll_seconds=0)


class TestLoopbackLiteral:
    def test_relay_urls_use_the_ipv4_literal(self):
        r = _relay()
        for url in (r.mcp_url, r.health_url, r.timing_url):
            assert url.startswith("http://127.0.0.1:2830/"), url

    def test_launcher_proxy_and_probe_urls_use_the_ipv4_literal(self):
        assert launcher.backend_url(2830) == "http://127.0.0.1:2830/mcp"
        assert launcher.health_url(2830) == "http://127.0.0.1:2830/health"

    def test_seek_health_url_maps_localhost_to_the_literal(self):
        assert mcp_port.health_url(2830) == "http://127.0.0.1:2830/health"
        assert mcp_port.health_url(2830, "localhost") == "http://127.0.0.1:2830/health"
        # A real remote host is left alone.
        assert mcp_port.health_url(2830, "10.0.0.5") == "http://10.0.0.5:2830/health"


class _Capture(BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *a):
        pass

    def _reply(self):
        _Capture.seen.append((self.command, self.path, dict(self.headers.items())))
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self._reply()

    do_GET = do_DELETE = _reply


@pytest.fixture
def ipv4_only_server():
    """Bound to 0.0.0.0 only, as uvicorn is in LAN/WAN mode."""
    _Capture.seen = []
    srv = ThreadingHTTPServer(("0.0.0.0", 0), _Capture)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()


class TestClientPidHeader:
    def test_the_header_name_is_the_one_the_server_reads(self):
        assert CLIENT_PID_HEADER.lower() == "x-browserplug-client-pid"
        from server.web import http_endpoints
        assert http_endpoints.CLIENT_PID_HEADER == CLIENT_PID_HEADER

    def test_the_relay_sends_its_parent_pid_on_every_request(self, ipv4_only_server):
        r = _relay(ipv4_only_server)
        status, _h, _t = r.post(b'{"jsonrpc":"2.0","id":1,"method":"ping"}')
        assert status == 200
        r._session_id = "sid"
        r.close_session()
        r.health()
        assert [s[0] for s in _Capture.seen] == ["POST", "DELETE", "GET"]
        for _cmd, _path, headers in _Capture.seen:
            lowered = {k.lower(): v for k, v in headers.items()}
            assert lowered.get(CLIENT_PID_HEADER.lower()) == str(client_pid())

    def test_the_proxy_transport_carries_the_same_header(self):
        """The launcher hands its client pid to the proxy child on the command line, and
        the child's backend client sends it on every request."""
        from server.proxy import policy
        from server.proxy.backend import Backend
        config = policy.parse_args(launcher.proxy_command("python", 2830)[2:])
        headers = {k.lower(): v for k, v in Backend(config).http_client().headers.items()}
        assert headers.get(CLIENT_PID_HEADER.lower()) == str(client_pid())

    def test_the_forwarded_pid_becomes_the_routing_key(self):
        from server.browsers import routing
        # The key names the process and its start time; the bare pid is not enough.
        key = routing.client_key(client_pid=client_pid())
        assert key == routing.pid_client_key(client_pid())
        assert key.startswith(f"pid:{client_pid()}")


class TestRelayModeMain:
    """The default transport through main(); test_launcher.py pins the proxy for its file."""

    def test_the_relay_is_served_with_the_launcher_identity_headers(self, monkeypatch):
        seen = {}
        import launcher.stdio_relay as sr
        monkeypatch.setattr(sr, "run_relay", lambda port, log, identity_headers=None, **kw:
                            seen.update(port=port, headers=identity_headers, **kw))
        launcher.serve_via_relay(2831)
        assert seen["port"] == 2831
        assert seen["headers"] == launcher.launcher_identity_headers()
        # The relay recovers a dead server with the launcher's own seek-then-spawn (L2).
        assert seen["recover"] is launcher.ensure_server

    def test_main_with_no_server_still_relays_to_the_default_port(self, monkeypatch):
        calls = []
        monkeypatch.delenv(launcher.RELAY_ENV, raising=False)
        monkeypatch.delenv(launcher.MCP_PORT_ENV, raising=False)
        monkeypatch.setattr(launcher, "ensure_server", lambda: None)
        monkeypatch.setattr(launcher, "serve_via_relay", lambda p: calls.append(("relay", p)))
        monkeypatch.setattr(launcher, "serve_via_proxy", lambda p: calls.append(("proxy", p)))
        launcher.main()
        assert calls == [("relay", launcher.MCP_PORT_DEFAULT)]

    def test_main_warns_once_on_version_skew_in_relay_mode(self, monkeypatch, capsys):
        monkeypatch.delenv(launcher.RELAY_ENV, raising=False)
        monkeypatch.setattr(launcher, "ensure_server", lambda: 2830)
        monkeypatch.setattr(launcher, "serve_via_relay", lambda p: None)
        monkeypatch.setattr(launcher, "browserplug_health",
                            lambda port: {"server_version": "0.0.1-skew"})
        launcher.main()
        assert capsys.readouterr().err.count("version mismatch") == 1
