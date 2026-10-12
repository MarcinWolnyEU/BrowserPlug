"""S14: the /ws-bind host is validated, and an unbindable persisted host cannot brick starts.

Two halves. The endpoint half: a host that is not a documented mode token or an address
assigned to a local interface is a 400 and never reaches config.json. The start-up half: a
host already in config.json (hand-edited, or written before the validation) that cannot be
bound falls back to localhost with a logged ERROR instead of failing every start.
"""

import json
import logging

import pytest

from secure_ext import signed_headers
from server.ws import ws_host_policy
from browserplug_shared.config_store import read_ws_host, write_ws_host, write_ws_mode
from test_ws_token import bind_app, http_post, make_bootstrap_server, make_server

ASSIGNED = "192.168.77.5"


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    monkeypatch.delenv("BROWSERPLUG_WS_HOST", raising=False)
    monkeypatch.delenv("BROWSERPLUG_WS_TOKEN", raising=False)
    write_ws_host(None)
    write_ws_mode(None)
    yield
    write_ws_host(None)
    write_ws_mode(None)


def _server_with_lan(monkeypatch):
    server = make_bootstrap_server()
    monkeypatch.setattr(server, "ws_lan_addresses",
                        lambda: [{"ip": ASSIGNED, "interface": "Ethernet", "recommended": True}])
    return server


async def _bind(server, host):
    return await http_post(bind_app(server), "/ws-bind",
                           body=json.dumps({"host": host}).encode(),
                           headers=signed_headers(server, "POST", "/ws-bind", body=json.dumps({"host": host}).encode()))


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["not a host!", "999.1.1.1", "example.org", "203.0.113.7",
                                 "0.0.0.0/0", "0.0.0.0\n", "host:8772"])
async def test_ws_bind_rejects_an_unusable_host_with_400_and_persists_nothing(monkeypatch, bad):
    server = _server_with_lan(monkeypatch)
    status, body = await _bind(server, bad)
    assert status == 400, body
    assert "host" in json.loads(body)["error"].lower()
    assert read_ws_host() is None, "a rejected host must never reach config.json"
    assert server.ws_host == "localhost"


@pytest.mark.asyncio
@pytest.mark.parametrize("good", ["0.0.0.0", "::", "localhost", "127.0.0.1", ASSIGNED])
async def test_ws_bind_accepts_mode_tokens_and_assigned_addresses(monkeypatch, good):
    server = _server_with_lan(monkeypatch)
    status, body = await _bind(server, good)
    assert status == 200, body
    assert read_ws_host() == good


@pytest.mark.parametrize("host,ok", [
    ("localhost", True), ("LOCALHOST", True), ("127.0.0.1", True), ("::1", True),
    ("0.0.0.0", True), ("::", True), (ASSIGNED, True),
    ("10.9.9.9", False), ("example.org", False), ("", False), ("a b", False),
    ("300.1.1.1", False), ("fe80::1%eth0", False)])
def test_validate_ws_host(host, ok):
    error = ws_host_policy.validate_ws_host(host, [ASSIGNED])
    assert (error is None) == ok, error


def test_a_persisted_unbindable_host_falls_back_to_localhost_with_an_error(caplog):
    # Passes validation as syntax, but no interface owns it: bind() fails with EADDRNOTAVAIL.
    write_ws_host("203.0.113.7")
    with caplog.at_level(logging.ERROR):
        server = make_server()
    assert server.ws_host == "localhost"
    assert server.ws_host_source == "default"
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors and "203.0.113.7" in errors[0].getMessage()


def test_a_persisted_garbage_host_falls_back_too(caplog):
    write_ws_host("not a host!")
    with caplog.at_level(logging.ERROR):
        server = make_server()
    assert server.ws_host == "localhost"
    assert any("not a host!" in r.getMessage() for r in caplog.records)


def test_a_bindable_persisted_host_is_kept():
    write_ws_host("0.0.0.0")
    server = make_server()
    assert server.ws_host == "0.0.0.0"
    assert server.ws_host_source == "file"


def test_an_env_host_is_not_second_guessed(monkeypatch):
    """env and --ws-host are explicit and fail loudly at bind time; only the file falls back."""
    monkeypatch.setenv("BROWSERPLUG_WS_HOST", "203.0.113.7")
    assert make_server().ws_host == "203.0.113.7"


def test_host_is_bindable_probe():
    assert ws_host_policy.host_is_bindable("127.0.0.1")
    assert ws_host_policy.host_is_bindable("localhost")
    assert not ws_host_policy.host_is_bindable("203.0.113.7")
    assert not ws_host_policy.host_is_bindable("no such host!")
