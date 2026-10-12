"""S2: /shutdown, /restart, /timing and /diagnose-wan demand a signed request.

Before this, any web page could POST these to the loopback port (a no-cors form post needs no
preflight). The signature header is custom, so a page cannot send it; a cross-site or
Origin-carrying request is refused as a second layer. Side effects are mocked: nothing here
restarts or stops anything.
"""

import json
import os

import pytest

from secure_ext import signed_headers
from test_http_endpoints import REMOTE_PEER, _server, call
from server.web import http_endpoints
from server.reporting import wan_diagnosis

CONTROL_ROUTES = [("POST", "/restart"), ("POST", "/shutdown"), ("POST", "/timing"),
                  ("GET", "/diagnose-wan")]


class Effects:
    """Records every side effect a control endpoint can have."""

    def __init__(self):
        self.calls = []


@pytest.fixture
def effects(monkeypatch):
    fx = Effects()

    async def fake_diagnose(*a, **kw):
        fx.calls.append("diagnose")
        return {"fake": True}

    monkeypatch.setattr(wan_diagnosis, "diagnose_wan_reachability", fake_diagnose)
    monkeypatch.setattr(wan_diagnosis, "_CACHE", {}, raising=False)
    # /shutdown ends in os._exit on a timer thread; never let a test reach the real one.
    monkeypatch.setattr(os, "_exit", lambda code: fx.calls.append(f"exit{code}"))
    return fx


def _app(effects):
    server = _server()
    server._begin_restart = lambda trigger: effects.calls.append("restart")
    server.record_launcher_timing = lambda body: effects.calls.append("timing") or False
    return server, server._wrap_client_tracking(lambda *a: None)


def _hdr(name, value):
    return (name.encode("latin-1"), value.encode("latin-1"))


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path", CONTROL_ROUTES)
async def test_an_unsigned_local_request_is_refused_before_the_handler(method, path, effects):
    server, app = _app(effects)
    status, _, body = await call(app, method, path, body=b"{}")
    assert status == 403, f"{method} {path} reached its handler with no token"
    assert "token" in json.loads(body)["error"]
    assert effects.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path", CONTROL_ROUTES)
@pytest.mark.parametrize("extra", [
    [_hdr("sec-fetch-site", "cross-site")],
    [_hdr("origin", "https://evil.example")],
    [_hdr("origin", "null")],
    [_hdr("origin", "http://localhost:2830")],
])
async def test_a_browser_cross_site_request_is_refused_even_when_signed(
        method, path, extra, effects):
    server, app = _app(effects)
    headers = signed_headers(server, method, path, body=b"{}") + extra
    status, _, body = await call(app, method, path, headers=headers, body=b"{}")
    assert status == 403
    assert effects.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path,marker", [
    ("POST", "/restart", "restart"), ("POST", "/shutdown", "exit0"),
    ("POST", "/timing", "timing"), ("GET", "/diagnose-wan", "diagnose")])
async def test_a_signed_request_reaches_the_handler(method, path, marker, effects):
    import asyncio
    server, app = _app(effects)
    status, _, _ = await call(app, method, path,
                              headers=signed_headers(server, method, path, body=b"{}"),
                              body=b"{}")
    assert status == 200
    if marker == "exit0":
        await asyncio.sleep(0.6)
    assert marker in effects.calls


@pytest.mark.asyncio
async def test_a_signed_request_from_the_extension_page_is_admitted(effects):
    """The options page's HTTP fallback runs on a moz-extension origin and signs."""
    server, app = _app(effects)
    headers = signed_headers(server, "POST", "/restart") + [
        _hdr("origin", "moz-extension://0123abcd-0000-0000-0000-000000000000"),
        _hdr("sec-fetch-site", "cross-site")]
    status, _, _ = await call(app, "POST", "/restart", headers=headers)
    assert status == 200
    assert effects.calls == ["restart"]


@pytest.mark.asyncio
async def test_a_remote_signed_request_still_works(effects):
    server, app = _app(effects)
    status, _, _ = await call(app, "POST", "/timing", client=REMOTE_PEER,
                              headers=signed_headers(server, "POST", "/timing"))
    assert status == 200


def test_the_four_rows_carry_the_control_policy():
    rows = {r.name: r.policy for r in http_endpoints.ROUTES}
    for name in ("shutdown", "restart", "timing", "diagnose-wan"):
        assert rows[name] == http_endpoints.CONTROL, name


# --- /diagnose-wan: cache, and the powershell child dies on timeout ---------------------

@pytest.mark.asyncio
async def test_diagnose_wan_is_cached_briefly(effects, monkeypatch):
    server, app = _app(effects)
    for _ in range(3):
        status, _, _ = await call(app, "GET", "/diagnose-wan",
                                  headers=signed_headers(server, "GET", "/diagnose-wan"))
        assert status == 200
    assert effects.calls.count("diagnose") == 1

    real_now = wan_diagnosis._now()
    monkeypatch.setattr(wan_diagnosis, "_now", lambda: real_now + 31)
    await call(app, "GET", "/diagnose-wan",
               headers=signed_headers(server, "GET", "/diagnose-wan"))
    assert effects.calls.count("diagnose") == 2


@pytest.mark.asyncio
async def test_a_powershell_timeout_kills_the_child(monkeypatch):
    import asyncio
    killed = []

    class Hung:
        returncode = None

        async def communicate(self):
            await asyncio.sleep(10)

        def kill(self):
            killed.append(True)

        async def wait(self):
            return 0

    async def fake_exec(*a, **kw):
        return Hung()

    monkeypatch.setattr(wan_diagnosis.platform, "system", lambda: "Windows")
    monkeypatch.setattr(wan_diagnosis, "WINDOWS_DIAG_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    result = await wan_diagnosis._windows_wan_diagnostics([{"port": 8772}])
    assert result["firewall"] is None
    assert killed == [True]


# --- server/control.py --------------------------------------------------------------------

def test_control_signs_with_the_token_file_of_the_state_dir(tmp_path, monkeypatch):
    from server import control
    from browserplug_shared import local_auth, request_signing, ws_token
    monkeypatch.delenv(ws_token.WS_TOKEN_ENV, raising=False)
    monkeypatch.setenv("BROWSERPLUG_STATE_DIR", str(tmp_path))
    monkeypatch.delenv(ws_token.WS_TOKEN_FILE_ENV, raising=False)
    master = ws_token.generate_token()
    ws_token.write_token_file(master)
    monkeypatch.setattr(local_auth, "_master_cache", None)

    headers = control.signed_headers("POST", "/restart")

    from browserplug_shared import device_tokens
    assert request_signing.verify(headers[request_signing.AUTH_HEADER],
                                  lambda label: device_tokens.secret_for(master, label),
                                  "POST", "/restart", guard=request_signing.ReplayGuard())


def test_control_without_a_token_signs_nothing(tmp_path, monkeypatch):
    from server import control
    from browserplug_shared import local_auth, ws_token
    monkeypatch.delenv(ws_token.WS_TOKEN_ENV, raising=False)
    monkeypatch.setenv("BROWSERPLUG_STATE_DIR", str(tmp_path))
    monkeypatch.delenv(ws_token.WS_TOKEN_FILE_ENV, raising=False)
    monkeypatch.setattr(local_auth, "_master_cache", None)
    assert control.signed_headers("POST", "/restart") == {}


def test_control_posts_a_signed_request_to_the_found_port(monkeypatch, capsys):
    import http.server
    import threading
    from server import control
    from browserplug_shared import local_auth, ws_token
    seen = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            seen.append((self.path, self.headers.get("x-browserplug-auth")))
            body = b'{"status": "restarting", "pid": 1}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv(ws_token.WS_TOKEN_ENV, ws_token.generate_token())
        monkeypatch.setattr(local_auth, "_master_cache", None)
        monkeypatch.setattr(control, "find_port", lambda: httpd.server_address[1])
        assert control.main(["restart"]) == 0
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert seen[0][0] == "/restart" and seen[0][1].startswith("v1 ")
    assert "restarting" in capsys.readouterr().out


def test_control_reports_no_server(monkeypatch, capsys):
    from server import control
    monkeypatch.setattr(control, "find_port", lambda: None)
    assert control.main(["status"]) == 2
    assert control.main(["bogus"]) == 2


# --- /ws-bind and /reset-config: token plus the same cross-site layer --------------------

TOKEN_ROUTES = [("POST", "/ws-bind"), ("POST", "/reset-config")]


def _token_app(effects):
    server = _server()
    server.set_ws_host = lambda host, mode: effects.calls.append("bind") or (host, None)
    server.reset_config = lambda: effects.calls.append("reset") or (None, None)
    server._restart_onto = lambda host: effects.calls.append("restart")
    return server, server._wrap_client_tracking(lambda *a: None)


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path", TOKEN_ROUTES)
@pytest.mark.parametrize("extra", [
    [_hdr("sec-fetch-site", "cross-site")],
    [_hdr("origin", "https://evil.example")],
    [_hdr("origin", "null")],
])
async def test_a_page_request_to_a_token_route_is_refused_even_when_signed(
        method, path, extra, effects):
    server, app = _token_app(effects)
    headers = signed_headers(server, method, path, body=b"{}") + extra
    status, _, _ = await call(app, method, path, headers=headers, body=b"{}")
    assert status == 403
    assert effects.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path,marker", [("POST", "/ws-bind", "bind"),
                                                ("POST", "/reset-config", "reset")])
async def test_the_extension_page_still_reaches_a_token_route(method, path, marker, effects):
    server, app = _token_app(effects)
    headers = signed_headers(server, method, path, body=b"{}") + [
        _hdr("origin", "moz-extension://0123abcd-0000-0000-0000-000000000000"),
        _hdr("sec-fetch-site", "cross-site")]
    status, _, _ = await call(app, method, path, headers=headers, body=b"{}")
    assert status == 200
    assert marker in effects.calls


def test_every_row_that_demands_a_signature_carries_the_cross_site_flag():
    for row in http_endpoints.ROUTES:
        if row.policy in (http_endpoints.TOKEN, http_endpoints.CONTROL):
            assert row.refuse_cross_site, row.name
