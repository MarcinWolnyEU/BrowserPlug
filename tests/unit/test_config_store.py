"""The server's settings file: config.json, and the reset that deletes it.

Two things are pinned here. The STORE itself (atomic writes, a corrupt file reading as
"no settings", read-modify-write not dropping keys it doesn't own), and POST
/reset-config, the server half of the options page's "Reset all settings to Defaults".

The invariant behind most of it: this one file carries every setting, so a writer that
loses a key it wasn't thinking about silently resets that setting for the user.
"""
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from browserplug_shared.config_store import (
    CONFIG_VERSION,
    STATE_DIR_ENV,
    config_path,
    load,
    read_ws_host,
    read_ws_mode,
    reset,
    save,
    state_dir,
    write_ws_host,
    write_ws_mode,
)
from server.server import BrowserPlugServer, WS_HOST_ENV
from browserplug_shared.ws_token import WS_TOKEN_ENV, read_token_file


@pytest.fixture(autouse=True)
def clean_config():
    """config.json is real, persistent state living in the session's tmp state dir, so
    one test's settings would otherwise be the next test's starting settings."""
    reset()
    yield
    reset()


def secret():
    """A token, minted the only way this project allows: never a literal."""
    import secrets
    return secrets.token_urlsafe(32)


# --- the store ------------------------------------------------------------------------

def test_everything_lives_in_one_file_in_one_directory():
    bootstrap_server()      # mints and persists the token, as a first run would
    write_ws_host("0.0.0.0")
    write_ws_mode("wan")

    assert os.path.dirname(config_path()) == state_dir()
    # `http-nonces` is the replay memory of signed HTTP requests (request_signing.py): it
    # holds no setting and no secret, and exists once any signed request was verified.
    assert sorted(p.name for p in Path(state_dir()).iterdir()
                  if p.is_file() and p.name != "http-nonces") == \
        ["config.json", "ws-token"], "one settings file, one secret, nothing else"
    assert json.loads(Path(config_path()).read_text(encoding="utf-8")) == {
        "version": CONFIG_VERSION, "ws": {"host": "0.0.0.0", "mode": "wan"}}


def test_the_token_is_not_in_the_config():
    """The whole reason for two files: a settings write can never touch the secret."""
    bootstrap_server()
    write_ws_host("0.0.0.0")
    token = read_token_file()
    assert token, "the server minted and persisted one"
    assert token not in Path(config_path()).read_text(encoding="utf-8")


def test_a_writer_keeps_the_keys_it_does_not_own():
    save({"ws": {"host": "0.0.0.0"}, "future_feature": {"kept": True}})
    write_ws_mode("lan")

    stored = load()
    assert stored["ws"] == {"host": "0.0.0.0", "mode": "lan"}
    assert stored["future_feature"] == {"kept": True}, \
        "read-modify-write, or every writer resets the settings it wasn't thinking about"


def test_clearing_the_last_ws_key_leaves_no_empty_section():
    write_ws_host("0.0.0.0")
    write_ws_host(None)
    assert "ws" not in load()
    assert read_ws_host() is None


@pytest.mark.parametrize("content", ["", "   ", "not json at all", "[1,2,3]", '"a string"'])
def test_an_unreadable_config_reads_as_no_settings_not_as_an_error(content):
    """A corrupt file must not stop the server from starting on its defaults, and must
    not be mistaken for a stored bind."""
    Path(config_path()).write_text(content, encoding="utf-8")

    assert load() == {}
    assert read_ws_host() is None and read_ws_mode() is None
    assert BrowserPlugServer(host="localhost", port=8772, start_mcp=False).ws_host == "localhost"


def test_a_failed_write_leaves_the_previous_config_intact_and_no_litter():
    """Atomicity: the temp file is replaced into place, so a crash mid-write cannot
    produce a half-written config — the state every reader would then misread."""
    write_ws_host("0.0.0.0")
    before = Path(config_path()).read_text(encoding="utf-8")

    with patch("browserplug_shared.config_store.os.replace", side_effect=OSError("disk full")):
        assert write_ws_mode("wan") is None, "the failure is reported, not swallowed"

    assert Path(config_path()).read_text(encoding="utf-8") == before
    assert read_ws_host() == "0.0.0.0" and read_ws_mode() is None
    assert [p.name for p in Path(state_dir()).iterdir() if p.name.startswith("config.json.")] == [], \
        "the temp file is cleaned up"


def test_reset_deletes_the_file_and_is_fine_when_there_is_nothing_to_delete():
    write_ws_host("0.0.0.0")
    assert os.path.exists(config_path())

    assert reset() is True
    assert not os.path.exists(config_path())
    assert load() == {} and read_ws_host() is None

    assert reset() is True, "'no stored settings' is the state either way"


def test_the_state_dir_env_moves_the_whole_directory(tmp_path, monkeypatch):
    """One knob for both files — what the test suite itself relies on."""
    monkeypatch.setenv(STATE_DIR_ENV, str(tmp_path / "elsewhere"))
    assert state_dir() == str(tmp_path / "elsewhere")
    write_ws_host("0.0.0.0")
    assert (tmp_path / "elsewhere" / "config.json").exists()


# --- POST /reset-config ---------------------------------------------------------------

async def http_post(app, path, headers=(), client=("127.0.0.1", 5555)):
    """POST through the ASGI wrapper. `headers` are (name, value) byte pairs."""
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    await app({"type": "http", "method": "POST", "path": path,
               "headers": list(headers), "client": client}, receive, send)
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, body


def reset_app(server):
    """The wrapper, with the restart RECORDED instead of performed — the real one calls
    os._exit(), which would take the test runner with it."""
    server.restarted_onto = []
    server._restart_onto = server.restarted_onto.append
    return server._wrap_client_tracking(lambda *a: None)


def bootstrap_server(**kwargs):
    return BrowserPlugServer(host="localhost", port=8772, start_mcp=True, **kwargs)


def auth(server):
    from secure_ext import signed_headers
    return signed_headers(server, "POST", "/reset-config")


@pytest.mark.asyncio
async def test_reset_config_deletes_the_file_and_restarts_onto_the_default_bind(monkeypatch):
    """The reset a user actually notices: a LAN bind must not survive it."""
    monkeypatch.delenv(WS_TOKEN_ENV, raising=False)
    monkeypatch.delenv(WS_HOST_ENV, raising=False)
    write_ws_host("0.0.0.0")
    write_ws_mode("wan")
    server = bootstrap_server()
    assert server.ws_host == "0.0.0.0" and server.ws_host_source == "file"

    status, body = await http_post(reset_app(server), "/reset-config", headers=auth(server))

    assert status == 200
    payload = json.loads(body)
    assert payload["config_deleted"] is True and payload["restarting"] is True
    assert payload["ws_host"] == "localhost" and payload["ws_mode"] == "local"
    assert not os.path.exists(config_path())
    assert server.restarted_onto == ["localhost"], "the new bind only takes effect on restart"

    fresh = bootstrap_server()
    assert fresh.ws_host == "localhost" and fresh.ws_host_source == "default"


@pytest.mark.asyncio
async def test_reset_config_keeps_the_token(monkeypatch):
    """Deleting it would lock out every remote browser the user configured, and rotation
    is gen_ws_token.py, which at least prints the replacement."""
    monkeypatch.delenv(WS_TOKEN_ENV, raising=False)
    server = bootstrap_server()
    before = read_token_file()

    status, _ = await http_post(reset_app(server), "/reset-config", headers=auth(server))

    assert status == 200
    assert read_token_file() == before and server.ws_token == before


@pytest.mark.asyncio
async def test_reset_config_does_not_restart_when_the_bind_is_already_the_default(monkeypatch):
    """Nothing to rebind onto: restarting anyway would drop every connected browser for
    no reason at all."""
    monkeypatch.delenv(WS_TOKEN_ENV, raising=False)
    monkeypatch.delenv(WS_HOST_ENV, raising=False)
    server = bootstrap_server()

    status, body = await http_post(reset_app(server), "/reset-config", headers=auth(server))

    assert status == 200
    assert json.loads(body)["restarting"] is False
    assert server.restarted_onto == []


@pytest.mark.asyncio
async def test_reset_config_leaves_an_env_bind_alone(monkeypatch):
    """BROWSERPLUG_WS_HOST never came from the file, so deleting the file cannot revoke
    it — claiming otherwise would send the user looking for a bind that is still live."""
    monkeypatch.setenv(WS_TOKEN_ENV, secret())
    monkeypatch.setenv(WS_HOST_ENV, "192.168.1.9")
    write_ws_mode("wan")
    server = bootstrap_server()
    assert server.ws_host_source == "env"

    status, body = await http_post(reset_app(server), "/reset-config", headers=auth(server))

    assert status == 200
    assert json.loads(body)["restarting"] is False
    assert server.restarted_onto == []
    assert server.ws_host == "192.168.1.9", "the env still wins"
    assert not os.path.exists(config_path()), "and the file is still deleted"


@pytest.mark.asyncio
async def test_reset_config_needs_the_token(monkeypatch):
    """Any web page can POST to 127.0.0.1. A wrong token, or none, changes nothing."""
    monkeypatch.delenv(WS_TOKEN_ENV, raising=False)
    write_ws_host("0.0.0.0")
    server = bootstrap_server()
    app = reset_app(server)

    for headers in ([], [(b"x-browserplug-token", secret().encode())]):
        status, body = await http_post(app, "/reset-config", headers=headers)
        assert status == 403
        assert "token" in json.loads(body)["error"]

    assert read_ws_host() == "0.0.0.0", "nothing was reset"
    assert server.restarted_onto == []


@pytest.mark.asyncio
async def test_reset_config_refuses_a_remote_peer_without_the_token(monkeypatch):
    """Since 2026-09-02 this port follows the LAN bind, so the peer test alone stopped
    being the protection; a remote caller is refused for want of a token instead, and the
    settings file is untouched either way."""
    monkeypatch.delenv(WS_TOKEN_ENV, raising=False)
    write_ws_host("0.0.0.0")
    server = bootstrap_server()

    status, body = await http_post(reset_app(server), "/reset-config",
                                   client=("192.168.1.50", 4444))

    assert status == 403
    assert "token" in json.loads(body)["error"]
    assert read_ws_host() == "0.0.0.0"


@pytest.mark.asyncio
async def test_reset_config_reports_a_file_it_could_not_delete(monkeypatch):
    """A reset that failed must not answer 200: the caller would tell the user their
    settings are gone while the next start reads them right back."""
    monkeypatch.delenv(WS_TOKEN_ENV, raising=False)
    write_ws_host("0.0.0.0")
    server = bootstrap_server()

    with patch("browserplug_shared.config_store.os.remove", side_effect=OSError("in use")):
        status, body = await http_post(reset_app(server), "/reset-config", headers=auth(server))

    assert status == 500
    assert "config.json" in json.loads(body)["error"]
    assert read_ws_host() == "0.0.0.0"
    assert server.restarted_onto == []
