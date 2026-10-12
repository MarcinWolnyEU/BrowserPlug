"""
GET /diagnose-wan — server-side self-diagnosis for the WAN "Available online" check.

Backs extension-gecko/wan-diagnosis.js. All logic lives in server/reporting/wan_diagnosis.py,
isolated from server.py (which only dispatches to it) — see that module's docstring.
Style mirrors test_reachable.py (real-socket listening proof) and test_ws_token.py's
ASGI-harness helpers (http_get/bind_app, duplicated here at the same small scale those
two files already duplicate them rather than sharing).
"""

import asyncio
import json
import socket

import pytest

from server import server as server_module
from server.reporting import wan_diagnosis
from server.server import BrowserPlugServer
from secure_ext import signed_headers


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(autouse=True)
def fresh_diagnosis_cache(monkeypatch):
    """/diagnose-wan caches its answer for 30 s; one test's answer is not the next one's."""
    monkeypatch.setattr(wan_diagnosis, "_CACHE", {})


async def http_get(app, path, client=("127.0.0.1", 5555), headers=None):
    """Drive the server's ASGI wrapper directly — no port, no uvicorn."""
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": "GET", "path": path, "headers": headers or [],
             "client": client}
    await app(scope, receive, send)
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, body


PORTS = [{"port": 8772, "label": "the extension WebSocket"},
         {"port": 2830, "label": "the options page HTTP port"}]


def bind_app(server):
    return server._wrap_client_tracking(lambda *a: None)


# --- _wan_listening_detail: OS-level, not just "bind() didn't throw" -------------------

@pytest.mark.asyncio
async def test_listening_detail_finds_a_real_bound_socket():
    port = _free_port()
    with socket.socket() as s:
        s.bind(("127.0.0.1", port))
        s.listen(1)
        ok, detail = wan_diagnosis._wan_listening_detail(port)
    assert ok is True
    assert str(port) in detail


def test_listening_detail_false_when_nothing_bound():
    port = _free_port()  # bound-then-released, nothing listens on it now
    ok, detail = wan_diagnosis._wan_listening_detail(port)
    assert ok is False
    assert str(port) in detail


def test_listening_detail_degrades_on_psutil_failure(monkeypatch):
    import psutil

    def boom(kind='inet'):
        raise OSError("no permission")

    monkeypatch.setattr(psutil, "net_connections", boom)
    ok, detail = wan_diagnosis._wan_listening_detail(12345)
    assert ok is None
    assert "no permission" in detail


# --- _windows_wan_diagnostics: platform gate + subprocess degrade ----------------------

@pytest.mark.asyncio
async def test_windows_diagnostics_skips_subprocess_on_non_windows(monkeypatch):
    monkeypatch.setattr(wan_diagnosis.platform, "system", lambda: "Linux")

    async def must_not_run(*a, **kw):
        raise AssertionError("must not spawn a subprocess on non-Windows")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", must_not_run)
    result = await wan_diagnosis._windows_wan_diagnostics(PORTS)
    assert result == {"firewall": None, "gateway_ip": None,
                      "rules": [], "active_networks": []}


class _FakeProcess:
    def __init__(self, stdout=b"", hang=False, raise_exc=None):
        self._stdout = stdout
        self._hang = hang
        self._raise = raise_exc

    async def communicate(self):
        if self._raise:
            raise self._raise
        if self._hang:
            await asyncio.sleep(10)
        return self._stdout, b""


@pytest.mark.asyncio
async def test_windows_diagnostics_parses_powershell_json(monkeypatch):
    monkeypatch.setattr(wan_diagnosis.platform, "system", lambda: "Windows")

    async def fake_exec(*a, **kw):
        return _FakeProcess(stdout=json.dumps({
            "rules": [{"name": "BrowserPlug WS 8772 (LAN)", "profiles": "Private",
                       "ports": ["8772", "2830"]}],
            "active_networks": [{"name": "home", "category": "Private"}],
            "gateway": "192.168.0.1",
        }).encode())

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    result = await wan_diagnosis._windows_wan_diagnostics(PORTS)
    assert result["gateway_ip"] == "192.168.0.1"
    assert result["firewall"]["ok"] is True
    assert result["rules"][0]["name"] == "BrowserPlug WS 8772 (LAN)"


@pytest.mark.asyncio
async def test_windows_diagnostics_normalizes_a_single_object_into_a_list(monkeypatch):
    """ConvertTo-Json emits a bare object, not a one-element array, for a single match."""
    monkeypatch.setattr(wan_diagnosis.platform, "system", lambda: "Windows")

    async def fake_exec(*a, **kw):
        return _FakeProcess(stdout=json.dumps({
            "rules": {"name": "one", "profiles": "Private", "ports": ["8772", "2830"]},
            "active_networks": {"name": "home", "category": "Private"},
            "gateway": "192.168.0.1",
        }).encode())

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    result = await wan_diagnosis._windows_wan_diagnostics(PORTS)
    assert result["rules"][0]["name"] == "one"
    assert result["active_networks"] == [{"name": "home", "category": "Private"}]
    assert result["firewall"]["ok"] is True


@pytest.mark.asyncio
async def test_windows_diagnostics_no_rule_found(monkeypatch):
    monkeypatch.setattr(wan_diagnosis.platform, "system", lambda: "Windows")

    async def fake_exec(*a, **kw):
        return _FakeProcess(stdout=json.dumps({
            "rules": [],
            "active_networks": [{"name": "home", "category": "Private"}],
            "gateway": "10.0.0.1",
        }).encode())

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    result = await wan_diagnosis._windows_wan_diagnostics(PORTS)
    assert result["gateway_ip"] == "10.0.0.1"
    assert result["firewall"]["ok"] is False
    assert result["firewall"]["reason"] == "no_rule"


@pytest.mark.asyncio
async def test_windows_diagnostics_degrades_on_timeout(monkeypatch):
    monkeypatch.setattr(wan_diagnosis.platform, "system", lambda: "Windows")
    monkeypatch.setattr(wan_diagnosis, "WINDOWS_DIAG_TIMEOUT_SECONDS", 0.05)

    async def fake_exec(*a, **kw):
        return _FakeProcess(hang=True)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    result = await wan_diagnosis._windows_wan_diagnostics(PORTS)
    assert result == {"firewall": None, "gateway_ip": None,
                      "rules": [], "active_networks": []}


@pytest.mark.asyncio
async def test_windows_diagnostics_degrades_on_spawn_failure(monkeypatch):
    monkeypatch.setattr(wan_diagnosis.platform, "system", lambda: "Windows")

    async def fake_exec(*a, **kw):
        raise FileNotFoundError("powershell.exe not found")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    result = await wan_diagnosis._windows_wan_diagnostics(PORTS)
    assert result == {"firewall": None, "gateway_ip": None,
                      "rules": [], "active_networks": []}


@pytest.mark.asyncio
async def test_windows_diagnostics_degrades_on_bad_json(monkeypatch):
    monkeypatch.setattr(wan_diagnosis.platform, "system", lambda: "Windows")

    async def fake_exec(*a, **kw):
        return _FakeProcess(stdout=b"not json")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    result = await wan_diagnosis._windows_wan_diagnostics(PORTS)
    assert result == {"firewall": None, "gateway_ip": None,
                      "rules": [], "active_networks": []}


# --- diagnose_wan_reachability: full assembly, including lan_ip and host_bind ----------

@pytest.mark.asyncio
async def test_diagnose_wan_reachability_assembles_full_shape(monkeypatch):
    monkeypatch.setattr(wan_diagnosis.platform, "system", lambda: "Windows")

    async def fake_exec(*a, **kw):
        return _FakeProcess(stdout=json.dumps({
            "rules": [{"name": "BrowserPlug WS 8772 (LAN)", "profiles": "Private",
                       "ports": ["8772", "2830"]}],
            "active_networks": [{"name": "home", "category": "Private"}],
            "gateway": "192.168.0.1",
        }).encode())

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(wan_diagnosis, "_wan_listening_detail",
                        lambda port: (True, f"0.0.0.0:{port} LISTEN (pid 1)"))

    lan_rows = [{"ip": "192.168.0.114", "interface": "WiFi", "recommended": True}]

    result = await wan_diagnosis.diagnose_wan_reachability(8772, "0.0.0.0", 2830,
                                                           lan_rows=lan_rows)
    assert result["listening"] == {"ok": True, "detail": "0.0.0.0:8772 LISTEN (pid 1)"}
    assert result["host_bind"] == {"ok": True, "host": "0.0.0.0"}
    assert result["gateway_ip"] == "192.168.0.1"
    assert result["lan_ip"] == "192.168.0.114"
    assert result["port"] == 8772
    # Both ports the remote browser needs are reported, and judged together.
    assert result["ports"] == [8772, 2830]
    assert result["firewall"]["checked"] is True
    assert result["firewall"]["ok"] is True
    assert result["firewall"]["reason"] == "ok"
    assert result["firewall"]["fix_command"] is None


@pytest.mark.asyncio
async def test_diagnose_wan_reachability_flags_narrow_bind(monkeypatch):
    monkeypatch.setattr(wan_diagnosis.platform, "system", lambda: "Linux")
    monkeypatch.setattr(wan_diagnosis, "_wan_listening_detail",
                        lambda port: (True, "127.0.0.1:8772 LISTEN (pid 1)"))

    result = await wan_diagnosis.diagnose_wan_reachability(8772, "127.0.0.1")
    assert result["host_bind"] == {"ok": False, "host": "127.0.0.1"}
    assert result["firewall"]["checked"] is False
    assert result["firewall"]["ok"] is None
    assert result["firewall"]["reason"] == "unknown"
    assert result["gateway_ip"] is None


# --- GET /diagnose-wan over the ASGI wrapper --------------------------------------------

@pytest.mark.asyncio
async def test_diagnose_wan_endpoint_returns_200_for_local_peer(monkeypatch):
    seen = {}

    async def fake_diagnose(ws_port, ws_host, mcp_port=None, lan_rows=()):
        seen["lan_rows"] = lan_rows
        return {"listening": {"ok": True, "detail": "x"}, "host_bind": {"ok": True, "host": ws_host},
                "firewall": {"checked": False, "ok": None}, "gateway_ip": None,
                "lan_ip": None, "port": ws_port}

    monkeypatch.setattr(wan_diagnosis, "diagnose_wan_reachability", fake_diagnose)

    server = BrowserPlugServer(host="localhost", port=8772, start_mcp=False)
    rows = [{"ip": "192.168.0.9", "interface": "Ethernet", "recommended": True}]
    monkeypatch.setattr(server_module, "ws_lan_addresses", lambda: rows)
    status, body = await http_get(bind_app(server), "/diagnose-wan",
                                  headers=signed_headers(server, "GET", "/diagnose-wan"))
    assert status == 200
    # The caller injects the machine's addresses; wan_diagnosis never reaches for them.
    assert seen["lan_rows"] == rows
    data = json.loads(body)
    # server.py reassigns off 8772 when it's already bound (e.g. a real dev server running
    # on this box) — assert against whatever it actually landed on, not the literal.
    assert data["port"] == server.port
    assert data["host_bind"]["host"] == "localhost"


@pytest.mark.asyncio
async def test_diagnose_wan_endpoint_refuses_a_remote_peer_without_the_token():
    """The payload names the LAN IP, the gateway and the firewall's rules. The peer check
    that used to guard it moved to http_auth; a remote caller now needs the token."""
    server = BrowserPlugServer(host="localhost", port=8772, start_mcp=False)
    status, body = await http_get(bind_app(server), "/diagnose-wan",
                                  client=("192.168.1.50", 4444))
    assert status == 403
    assert "token" in json.loads(body)["error"]


@pytest.mark.asyncio
async def test_diagnose_wan_endpoint_accepts_trailing_slash(monkeypatch):
    async def fake_diagnose(ws_port, ws_host, mcp_port=None, lan_rows=()):
        return {"port": ws_port}

    monkeypatch.setattr(wan_diagnosis, "diagnose_wan_reachability", fake_diagnose)
    server = BrowserPlugServer(host="localhost", port=8772, start_mcp=False)
    status, _ = await http_get(bind_app(server), "/diagnose-wan/",
                               headers=signed_headers(server, "GET", "/diagnose-wan/"))
    assert status == 200


# --- firewall_verdict: does a rule APPLY, on every port, not merely exist --------------
#
# The question this answers is the one the old check got wrong three ways. It looked only
# at rules carrying an application filter, so a perfectly good port rule read as "no rule".
# It never compared a rule's profile against the category Windows assigns the active
# network. And it asked about the WebSocket alone, which reported a confident "ok" for a
# machine whose options page still could not load from another host, because the HTTP port
# was blocked while the WebSocket was open. All three fail silently: the SYN is dropped, so
# nothing reaches the server to be logged and the remote browser only says Disconnected.

WS = {"port": 8772, "label": "the extension WebSocket"}
HTTP = {"port": 2830, "label": "the options page's HTTP port"}
BOTH = [WS, HTTP]
PUBLIC_WIFI = [{"name": "AC-Eclipse-5GHz 2", "category": "Public"}]


def rule(name, profiles, ports=("8772", "2830")):
    return {"name": name, "profiles": profiles, "ports": list(ports)}


def test_verdict_both_ports_covered_is_one_allowing_sentence():
    v = wan_diagnosis.firewall_verdict(
        [rule("BrowserPlug (LAN)", "Public")], PUBLIC_WIFI, BOTH)
    assert v["ok"] is True
    assert v["reason"] == "ok"
    assert v["fix_command"] is None
    # ONE sentence naming both ports, not a per-port scoreboard.
    assert "8772 and 2830" in v["detail"]
    assert "AC-Eclipse-5GHz 2" in v["detail"]


def test_verdict_reports_one_open_and_one_blocked_port_in_a_single_message():
    """The live 2026-09-02 case: the WS rule had just been fixed, the HTTP port had none."""
    v = wan_diagnosis.firewall_verdict(
        [rule("BrowserPlug WS 8772 (LAN)", "Private, Public", ports=("8772",))],
        PUBLIC_WIFI, BOTH)
    assert v["ok"] is False
    assert v["reason"] == "no_rule"
    assert "Port 8772 is allowed" in v["detail"]
    assert "port 2830" in v["detail"]
    assert "the options page's HTTP port" in v["detail"]
    assert "New-NetFirewallRule" in v["fix_command"]
    assert "-LocalPort 2830" in v["fix_command"]
    assert "8772" not in v["fix_command"], "do not touch the port that already works"


def test_verdict_two_broken_ports_produce_two_commands_in_one_block():
    v = wan_diagnosis.firewall_verdict([], PUBLIC_WIFI, BOTH)
    assert v["reason"] == "no_rule"
    lines = v["fix_command"].splitlines()
    assert len(lines) == 2
    assert "-LocalPort 8772" in lines[0]
    assert "-LocalPort 2830" in lines[1]


def test_verdict_mixes_a_missing_rule_and_a_wrong_profile():
    v = wan_diagnosis.firewall_verdict(
        [rule("BrowserPlug WS 8772 (LAN)", "Private", ports=("8772",))],
        PUBLIC_WIFI, BOTH)
    assert v["reason"] == "mixed"
    assert "Set-NetFirewallRule" in v["fix_command"]
    assert "New-NetFirewallRule" in v["fix_command"]


def test_verdict_one_rule_covering_both_blocked_ports_is_not_repeated():
    """Two ports behind one mis-scoped rule must print its fix once."""
    v = wan_diagnosis.firewall_verdict(
        [rule("BrowserPlug (LAN)", "Private")], PUBLIC_WIFI, BOTH)
    assert v["reason"] == "profile_mismatch"
    assert v["fix_command"].count("Set-NetFirewallRule") == 1
    assert v["fix_command"].endswith("-Profile Private,Public")


def test_verdict_no_rule_at_all_offers_a_rule_to_create():
    v = wan_diagnosis.firewall_verdict([], [{"name": "home", "category": "Private"}], [WS])
    assert v["ok"] is False
    assert v["reason"] == "no_rule"
    assert "-LocalPort 8772" in v["fix_command"]
    assert "-Profile Private" in v["fix_command"]
    assert "-RemoteAddress LocalSubnet" in v["fix_command"]


def test_verdict_private_rule_on_a_public_network_is_a_mismatch():
    v = wan_diagnosis.firewall_verdict(
        [rule("BrowserPlug WS 8772 (LAN)", "Private", ports=("8772",))],
        PUBLIC_WIFI, [WS])
    assert v["reason"] == "profile_mismatch"
    assert "BrowserPlug WS 8772 (LAN)" in v["detail"]
    assert "AC-Eclipse-5GHz 2" in v["detail"]
    assert v["fix_command"] == ("Set-NetFirewallRule -DisplayName "
                                "'BrowserPlug WS 8772 (LAN)' -Profile Private,Public")


def test_verdict_any_profile_covers_every_category():
    v = wan_diagnosis.firewall_verdict(
        [rule("wide open", "Any")], [{"name": "cafe wifi", "category": "Public"}], BOTH)
    assert v["ok"] is True


def test_verdict_maps_domainauthenticated_to_the_domain_profile():
    """Get-NetConnectionProfile says DomainAuthenticated where a rule says Domain."""
    v = wan_diagnosis.firewall_verdict(
        [rule("corp", "Domain")],
        [{"name": "corp.example", "category": "DomainAuthenticated"}], BOTH)
    assert v["ok"] is True


def test_verdict_names_the_domain_profile_when_that_is_the_uncovered_one():
    """The fix must not hardcode 'Public'; a corporate network is Domain."""
    v = wan_diagnosis.firewall_verdict(
        [rule("r", "Private")],
        [{"name": "corp.example", "category": "DomainAuthenticated"}], [WS])
    assert v["reason"] == "profile_mismatch"
    assert v["fix_command"].endswith("-Profile Domain,Private")


def test_verdict_requires_a_rule_to_cover_every_active_network():
    """A second adapter sitting on an uncovered profile is the case that bites."""
    v = wan_diagnosis.firewall_verdict(
        [rule("private only", "Private")],
        [{"name": "wired", "category": "Private"},
         {"name": "wifi", "category": "Public"}], [WS])
    assert v["ok"] is False


def test_verdict_doubles_an_apostrophe_in_a_rule_name():
    """A display name is the user's, and a stray quote would break the command."""
    v = wan_diagnosis.firewall_verdict(
        [rule("Marcin's rule", "Private")], PUBLIC_WIFI, [WS])
    assert "-DisplayName 'Marcin''s rule'" in v["fix_command"]


def test_verdict_will_not_guess_when_no_network_is_connected():
    v = wan_diagnosis.firewall_verdict([rule("r", "Private")], [], BOTH)
    assert v["ok"] is None
    assert v["reason"] == "unknown_network"
    assert v["fix_command"] is None
