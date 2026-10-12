"""
Unit tests for the extension WebSocket's token: where it comes from, who has to present
it, and how a browser gets hold of one. No Firefox, no psutil.

The extension WebSocket is *full control of a logged-in browser*. It stays localhost-only
by default; BROWSERPLUG_WS_HOST opts into a LAN bind for a browser on another machine.

The server now MINTS a token when none is configured and PERSISTS it (ws_token.py), so a
token always exists — which changes the question from "is auth on?" to "can this peer
obtain the token?" (BrowserPlugServer.auth_mode):

  * a REMOTE peer must always present it — no exceptions, ever;
  * a LOCAL peer must too, whenever it can bootstrap one (the loopback-only GET
    /ws-token on the MCP HTTP app) or the user configured one on purpose;
  * a LOCAL peer with NO bootstrap channel (--no-mcp / --stdio) is let in as before —
    nothing on this machine could learn the token, so demanding it would lock the user out
    of their own browser;
  * an extension too old to bootstrap a token is let in on a local socket (and ONLY there,
    and only when the token is auto-minted): rejecting it would brick a live browser that
    has no way back.

A browser WebSocket cannot set request headers, so the token rides in the first
`extension_hello` (background.js: `data.token`, key omitted entirely when unset), and the
4401 close reason is what tells a local extension where to fetch one.

Token values are never written down: every token here is minted at runtime with
`secrets`, and test_no_literal_token_in_the_repo enforces that for the whole tree.
"""

import asyncio
import io
import json
import os
import re
import secrets
import socket
import stat
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from unittest.mock import patch

import pytest

import gen_ws_token
from secure_ext import SecureClient, secure_fake_hello, signed_headers
from browserplug_shared import device_tokens
from server.server import (
    WS_HOST_ENV,
    WS_TOKEN_FILE_ENV,
    BrowserPlugServer,
)
from server.ws.ws_auth import (
    AUTH_BOOTSTRAP,
    AUTH_OFF,
    AUTH_STRICT,
    WS_CLOSE_UNAUTHORIZED,
    WS_PREAUTH_FRAME_LIMIT,
    WS_TOKEN_ENDPOINT_PATH,
    is_localhost_bind,
    ws_bind_addresses,
    ws_bind_refusal,
)
from browserplug_shared.ws_token import perms_are_owner_only
from token_test_helpers import is_valid_device_token
from browserplug_shared.config_store import (
    STATE_DIR_ENV,
    config_path,
    read_ws_host,
    read_ws_mode,
    write_ws_host,
    write_ws_mode,
)
from browserplug_shared.ws_token import (
    WS_TOKEN_ENV,
    SOURCE_ENV,
    SOURCE_FILE,
    SOURCE_GENERATED,
    default_token_path,
    read_token_file,
    resolve_token,
    write_token_file,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
_EOF = object()


def mint():
    """A fresh token, exactly as gen_ws_token.py would produce it. Never a literal."""
    return secrets.token_urlsafe(32)


@pytest.fixture(autouse=True)
def clean_ws_host_file():
    """The stored bind is real, persistent state (that is its whole point), and the
    session-scoped tmp state dir means one test's bind would otherwise be the next test's
    starting bind — every server built after it would silently come up on 0.0.0.0.
    The stored mode is the same kind of state, and leaks the same way."""
    write_ws_host(None)
    write_ws_mode(None)
    yield
    write_ws_host(None)
    write_ws_mode(None)


class FakeWS:
    """A WebSocket the connection handler can both `recv()` (auth path) and iterate."""

    def __init__(self, peer=("127.0.0.1", 5555)):
        self.remote_address = peer
        self.close_code = None
        self.close_reason = None
        self.sent = []
        self._q = asyncio.Queue()

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    async def close(self, code=1000, reason=""):
        if self.close_code is None:
            self.close_code = code
            self.close_reason = reason
        self._q.put_nowait(_EOF)

    def feed(self, message):
        self._q.put_nowait(json.dumps(message))

    def feed_raw(self, raw):
        self._q.put_nowait(raw)

    def drop(self, code=1006):
        self.close_code = code
        self._q.put_nowait(_EOF)

    async def recv(self):
        item = await self._q.get()
        if item is _EOF:
            raise ConnectionError("connection closed")
        return item

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self._q.get()
        if item is _EOF:
            raise StopAsyncIteration
        return item


def make_server(**kwargs):
    """A server with no MCP HTTP app (start_mcp=False) — so no bootstrap channel, and a
    LOCAL peer is in AUTH_OFF unless a token is configured explicitly. This is the shape
    most unit tests want; make_bootstrap_server() is the one with the endpoint."""
    return BrowserPlugServer(host="localhost", port=8772, start_mcp=False, **kwargs)


def make_bootstrap_server(**kwargs):
    """A server as production runs it: an MCP HTTP app (never actually bound here) means a
    local browser CAN fetch the token, so it is required to."""
    return BrowserPlugServer(host="localhost", port=8772, start_mcp=True, **kwargs)


async def wait_until(predicate, what, timeout=2.0, interval=0.01):
    """Poll `predicate` until it holds — the bounded-poll idiom from
    test_command_timings.py.

    Used wherever a fixed sleep was really waiting for an async step to LAND (a hello
    being authenticated and registered, the identity probe firing). A fixed sleep bets
    the step finishes inside N ms; on a loaded box it loses and the assertion below
    misreports a half-finished handshake as a rejected one. This exits as soon as the
    condition holds and fails naming what never happened.

    NOT used where the point is that nothing happens (an unauthenticated peer must get no
    answer): a negative has no edge to poll for, so those keep their settle window.
    """
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out after {timeout}s waiting until {what}")
        await asyncio.sleep(interval)


async def registered(server, instance_id, timeout=2.0):
    """Wait for `instance_id` to appear in the registry — what a hello that passes
    authentication is FOR. Also satisfied by a rejection, which never registers: those
    tests await the handler task instead (it returns when the socket is closed)."""
    await wait_until(lambda: instance_id in server.browsers,
                     f"the hello registered browser {instance_id!r}", timeout=timeout)


async def connect(server, ws):
    with patch.object(server, "_probe_browser_identity", return_value={}):
        task = asyncio.create_task(server.handle_extension_connection(ws))
        # The peer probe runs on an executor (finding C7): registration takes a thread hop.
        # Only a socket that registers on connect (AUTH_OFF) is waited for; the others
        # authenticate first and register later.
        await asyncio.sleep(0.01)
        if server.ws_auth.auth_mode(peer_local=True) == AUTH_OFF:
            for _ in range(400):
                if server.registry.browser_for_ws(ws) is not None or task.done():
                    break
                await asyncio.sleep(0.005)
    return task


def hello_frame(token=None, instance_id="ff-a", bootstrap=True, **extra):
    """The hello background.js really sends. `token=None` omits the key ENTIRELY —
    the extension never sends an empty string (getConnectionToken trims to '' and
    sendExtensionHello drops the field).

    `bootstrap=False` is an OLD extension: one built before the auto-pull, which therefore
    cannot answer a 4401 by fetching the token. The server tells them apart by exactly this
    flag."""
    data = {"version": "1.4.0", "instance_id": instance_id, **extra}
    if token is not None:
        data["token"] = token
    if bootstrap:
        data["tokenBootstrap"] = True
    return {"type": "extension_hello", "data": data}


# --- no token CONFIGURED, no bootstrap channel: the degraded path -------------------
# (--no-mcp / --stdio: nothing on this machine could learn the auto-minted token, so a
#  local browser is let in exactly as it was before tokens existed.)

@pytest.mark.asyncio
async def test_no_token_configured_and_no_bootstrap_channel_is_todays_behavior(monkeypatch):
    """The degrade rule. A token EXISTS (auto-minted), but with no /ws-token endpoint a
    local extension has no way to obtain it — so it is not demanded, and the hello
    registers the browser immediately, as it always did."""
    monkeypatch.delenv(WS_TOKEN_ENV, raising=False)
    monkeypatch.delenv(WS_HOST_ENV, raising=False)
    server = make_server()
    assert server.ws_token, "a token is always resolved now — minted when not configured"
    assert server.ws_token_explicit is False
    assert server.ws_auth.token_endpoint_url() is None, "no MCP HTTP app -> no bootstrap channel"
    assert server.ws_auth.auth_mode(peer_local=True) == AUTH_OFF
    assert server.ws_host == "localhost"

    ws = FakeWS()
    task = await connect(server, ws)
    # Registered on CONNECT, before any hello — as it always was.
    assert len(server.browsers) == 1
    ws.feed(hello_frame())
    await registered(server, "ff-a")

    assert list(server.browsers) == ["ff-a"]
    assert ws.close_code is None
    ws.drop()
    await asyncio.wait_for(task, timeout=2.0)


@pytest.mark.asyncio
async def test_a_remote_peer_is_never_degraded(monkeypatch):
    """The one exception that has no exceptions. Same server as above (AUTH_OFF for a
    local peer), but a peer from another machine still has to present the token — it is
    the only thing standing between the LAN and a logged-in browser."""
    monkeypatch.delenv(WS_TOKEN_ENV, raising=False)
    server = make_server()
    assert server.ws_auth.auth_mode(peer_local=True) == AUTH_OFF
    assert server.ws_auth.auth_mode(peer_local=False) == AUTH_STRICT

    ws = FakeWS(peer=("192.168.1.50", 51000))
    task = await connect(server, ws)
    ws.feed(hello_frame(instance_id="ff-remote"))          # no token
    await asyncio.wait_for(task, timeout=2.0)

    assert server.browsers == {}
    assert ws.close_code == WS_CLOSE_UNAUTHORIZED

    # ...and with the token it gets in.
    ws2 = FakeWS(peer=("192.168.1.50", 51001))
    task2 = await connect(server, ws2)
    await secure_fake_hello(ws2, server, hello_frame(instance_id="ff-remote"))
    await registered(server, "ff-remote")
    assert list(server.browsers) == ["ff-remote"]
    ws2.drop()
    await asyncio.wait_for(task2, timeout=2.0)


@pytest.mark.asyncio
async def test_no_token_configured_tolerates_a_hello_that_carries_one(monkeypatch):
    """An extension configured with a token talking to a server without one still works
    (the field is simply ignored) — and the token is never stored on the record."""
    monkeypatch.delenv(WS_TOKEN_ENV, raising=False)
    server = make_server()
    ws = FakeWS()
    task = await connect(server, ws)
    ws.feed(hello_frame(token=mint()))
    await registered(server, "ff-a")

    browser = server.browsers["ff-a"]
    assert browser.version == "1.4.0"
    assert not any("token" in str(v).lower() for v in vars(browser).values()
                   if isinstance(v, dict))
    ws.drop()
    await asyncio.wait_for(task, timeout=2.0)


# --- token auth: accept / reject / missing / timeout --------------------------------

@pytest.mark.asyncio
async def test_valid_token_is_accepted_and_registers_the_browser():
    token = mint()
    server = make_server(ws_token=token)
    ws = FakeWS()

    task = await connect(server, ws)
    # NOT registered on connect: the socket is unauthenticated until its hello lands.
    assert server.browsers == {}

    await secure_fake_hello(ws, server, hello_frame())
    await registered(server, "ff-a")

    assert list(server.browsers) == ["ff-a"]
    assert server.browsers["ff-a"].version == "1.4.0"
    assert server.browsers["ff-a"].encrypted
    assert ws.close_code is None
    ws.drop()
    await asyncio.wait_for(task, timeout=2.0)


@pytest.mark.asyncio
async def test_wrong_token_is_rejected_and_never_registered():
    server = make_server(ws_token=mint())
    ws = FakeWS()

    task = await connect(server, ws)
    ws.feed(hello_frame(token=mint()))   # a valid-shaped token — just not ours
    await asyncio.wait_for(task, timeout=2.0)

    assert server.browsers == {}, "an unauthenticated socket must never enter the registry"
    assert ws.close_code == 4401
    assert server.default_browser is None


@pytest.mark.asyncio
async def test_missing_token_is_rejected():
    """The exact wire shape of an extension with no token configured: no `token` key."""
    server = make_server(ws_token=mint())
    ws = FakeWS()

    task = await connect(server, ws)
    ws.feed(hello_frame())               # token key absent entirely
    await asyncio.wait_for(task, timeout=2.0)

    assert server.browsers == {}
    assert ws.close_code == 4401


@pytest.mark.asyncio
async def test_empty_token_is_rejected():
    """Belt and braces: '' must never satisfy the check, even though background.js
    can't send it."""
    server = make_server(ws_token=mint())
    ws = FakeWS()

    task = await connect(server, ws)
    ws.feed(hello_frame(token=""))
    await asyncio.wait_for(task, timeout=2.0)

    assert server.browsers == {}
    assert ws.close_code == 4401


@pytest.mark.asyncio
async def test_hello_that_never_arrives_times_out_and_closes():
    server = make_server(ws_token=mint())
    server.ws_auth_timeout = 0.05        # keep the test fast
    ws = FakeWS()

    task = await connect(server, ws)
    assert server.browsers == {}
    await asyncio.wait_for(task, timeout=2.0)   # returns on its own once the window closes

    assert server.browsers == {}
    assert ws.close_code == 4401


@pytest.mark.asyncio
async def test_localhost_must_also_present_the_token():
    """When a token is set, ALL connections need it — a local peer is not exempt."""
    server = make_server(ws_token=mint())
    ws = FakeWS(peer=("127.0.0.1", 5555))

    task = await connect(server, ws)
    ws.feed(hello_frame())
    await asyncio.wait_for(task, timeout=2.0)

    assert server.browsers == {}
    assert ws.close_code == 4401


@pytest.mark.asyncio
async def test_frames_before_the_hello_are_held_then_replayed():
    """background.js fires its health probe synchronously in onopen while
    sendExtensionHello() is still awaiting storage — so a `server.health` request
    routinely overtakes the hello. It must NOT be answered before authentication, and it
    must NOT be lost either."""
    token = mint()
    server = make_server(ws_token=token)
    ws = FakeWS()

    task = await connect(server, ws)
    # The handshake proves the token; the hello is still what names the browser, and a
    # frame that overtakes it inside the channel is held until it lands.
    await secure_fake_hello(ws, server, None)
    ws.feed({"id": "probe-1", "type": "request", "action": "server.health", "data": {}})
    await asyncio.sleep(0.01)
    assert ws.sent == [], "a peer that has not said hello gets no answer"
    assert server.browsers == {}

    ws.feed(hello_frame())
    # Registration happens BEFORE the held frames are replayed, so waiting on the replayed
    # answer waits for both — and cannot read a registry that is only half-way there.
    await wait_until(lambda: any(m.get("id") == "probe-1" for m in ws.sent),
                     "the held probe was replayed and answered")

    assert list(server.browsers) == ["ff-a"]
    answers = [m for m in ws.sent if m.get("id") == "probe-1"]
    assert len(answers) == 1, "the held probe must be answered once the peer authenticates"
    assert answers[0]["data"]["status"] == "ok"
    ws.drop()
    await asyncio.wait_for(task, timeout=2.0)


@pytest.mark.asyncio
async def test_a_flood_of_pre_auth_frames_is_cut_off():
    server = make_server(ws_token=mint())
    ws = FakeWS()

    task = await connect(server, ws)
    for i in range(WS_PREAUTH_FRAME_LIMIT + 1):
        ws.feed({"id": f"junk-{i}", "type": "request", "action": "server.health", "data": {}})
    await asyncio.wait_for(task, timeout=2.0)

    assert server.browsers == {}
    assert ws.sent == []
    assert ws.close_code == 4401


@pytest.mark.asyncio
async def test_garbage_before_the_hello_does_not_authenticate():
    server = make_server(ws_token=mint())
    server.ws_auth_timeout = 0.05
    ws = FakeWS()

    task = await connect(server, ws)
    ws.feed_raw("not json at all")
    ws.feed_raw(json.dumps(["a", "list"]))
    await asyncio.wait_for(task, timeout=2.0)

    assert server.browsers == {}
    assert ws.close_code == 4401


@pytest.mark.asyncio
async def test_a_signed_request_is_compared_in_constant_time():
    """The WebSocket proof is an AEAD tag, checked by the cipher itself. The HTTP signature
    is an HMAC the server compares, and that comparison must not be ==."""
    import hmac as _hmac
    from browserplug_shared import request_signing
    server = make_server(ws_token=mint())
    scope = {"method": "GET", "path": "/health",
             "headers": signed_headers(server, "GET", "/health")}
    with patch.object(request_signing.hmac, "compare_digest",
                      side_effect=_hmac.compare_digest) as spy:
        assert request_signing.verify_scope(scope, server.ws_auth.device_secret)
    assert spy.called


# --- bind address + the fail-safe ---------------------------------------------------

@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "::1", "LOCALHOST"])
def test_localhost_binds_need_no_token(host):
    assert is_localhost_bind(host) is True
    assert ws_bind_refusal(host, None) is None


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "::", "my-desktop.lan"])
def test_non_localhost_bind_is_refused_without_a_token(host):
    assert is_localhost_bind(host) is False
    refusal = ws_bind_refusal(host, None)
    assert refusal, f"{host} must not be bindable without a token"
    assert WS_TOKEN_ENV in refusal and "gen_ws_token.py" in refusal
    # With a token, the same bind is allowed.
    assert ws_bind_refusal(host, mint()) is None


@pytest.mark.parametrize("host", ["0.0.0.0", " 0.0.0.0 ", "::"])
def test_a_wildcard_bind_listens_on_BOTH_families(host):
    """`0.0.0.0` is the IPv4 wildcard only. Binding it alone leaves `::1` empty, and a
    browser resolving `localhost` tries `::1` first — so every reload logged an
    ERR_CONNECTION_REFUSED for a server that was up, then quietly fell back to
    127.0.0.1. Both families or the phantom error comes back."""
    addresses = ws_bind_addresses(host)
    assert set(addresses) == {"0.0.0.0", "::"}
    assert addresses[0] == host.strip(), "the configured wildcard stays first"


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "::1", "192.168.1.10"])
def test_a_named_or_specific_bind_is_passed_through_untouched(host):
    """Only a wildcard is expanded. `localhost` already resolves to both families, and
    widening a SPECIFIC address would bind an interface nobody asked for."""
    assert ws_bind_addresses(host) == host


def test_a_lan_bind_always_has_a_token_so_it_is_never_refused(monkeypatch):
    """The fail-safe used to be "no token, no LAN bind — refuse to start". The server now
    MINTS one instead, so exposure without a token is impossible by construction rather
    than by refusal, and `--ws-host 0.0.0.0` just works. What must NOT change: the peer
    that arrives over that LAN socket still has to present the token."""
    monkeypatch.delenv(WS_TOKEN_ENV, raising=False)
    server = make_server(ws_host="0.0.0.0")

    assert server.ws_host == "0.0.0.0"
    assert server.host == "localhost", "--host is untouched; the MCP bind itself follows the WS via mcp_bind_host"
    assert server.ws_token, "a LAN-bound socket is never tokenless"
    assert server.ws_auth.auth_mode(peer_local=False) == AUTH_STRICT
    assert ws_bind_refusal(server.ws_host, server.ws_token) is None


def test_a_lan_address_with_a_token_binds():
    server = make_server(ws_host="0.0.0.0", ws_token=mint())
    assert server.ws_host == "0.0.0.0"
    assert server.host == "localhost", "--host is untouched; the MCP bind itself follows the WS via mcp_bind_host"


def test_env_vars_configure_the_bind_and_the_token(monkeypatch):
    token = mint()
    monkeypatch.setenv(WS_HOST_ENV, "0.0.0.0")
    monkeypatch.setenv(WS_TOKEN_ENV, token)
    server = make_server()
    assert server.ws_host == "0.0.0.0"
    assert server.ws_token == token
    assert server.ws_token_source == SOURCE_ENV
    assert server.ws_token_explicit is True, "an env token is deliberate — enforce it"


def test_the_env_bind_without_an_env_token_mints_one(monkeypatch):
    """The old behavior was to refuse to start. Now the bind comes up with an auto-minted
    token — the operator reads it off the console (or the token file) and pastes it into
    the remote browser. Nothing is exposed unauthenticated."""
    monkeypatch.setenv(WS_HOST_ENV, "0.0.0.0")
    monkeypatch.delenv(WS_TOKEN_ENV, raising=False)
    server = make_server()
    assert server.ws_host == "0.0.0.0"
    assert server.ws_token
    assert server.ws_token_explicit is False
    assert server.ws_auth.auth_mode(peer_local=False) == AUTH_STRICT


def test_a_blank_env_token_is_no_token(monkeypatch, tmp_path):
    """A shell that exports an empty var must not read as "auth configured" — that would
    let ANY connection through the compare. It falls through to the file/mint path."""
    monkeypatch.setenv(WS_TOKEN_ENV, "   ")
    monkeypatch.setenv(WS_TOKEN_FILE_ENV, str(tmp_path / "ws-token"))
    monkeypatch.delenv(WS_HOST_ENV, raising=False)
    server = make_server()
    assert server.ws_token, "blank env -> mint, not 'no token'"
    assert server.ws_token.strip() == server.ws_token
    assert server.ws_token_source == SOURCE_GENERATED
    assert server.ws_token_explicit is False, "an empty var is not a configuration"


def test_health_reports_the_bind_state_but_never_the_token():
    token = mint()
    server = make_server(ws_host="0.0.0.0", ws_token=token)
    health = server.get_health_info()
    assert health["ws_host"] == "0.0.0.0"
    assert health["ws_auth_required"] is True
    assert health["ws_auth_mode"] == AUTH_STRICT
    assert token not in json.dumps(health), "the token is a secret — never in /health"


def test_health_reports_the_auth_mode_and_the_token_source(monkeypatch):
    monkeypatch.delenv(WS_TOKEN_ENV, raising=False)
    health = make_bootstrap_server().get_health_info()
    assert health["ws_auth_mode"] == AUTH_BOOTSTRAP
    assert health["ws_auth_required"] is True
    assert health["ws_token_source"] in (SOURCE_FILE, SOURCE_GENERATED)
    assert health["ws_token_endpoint"].endswith(WS_TOKEN_ENDPOINT_PATH)


def test_health_defaults_report_no_auth(monkeypatch):
    """No bootstrap channel, no configured token -> a local peer is not challenged."""
    monkeypatch.delenv(WS_TOKEN_ENV, raising=False)
    monkeypatch.delenv(WS_HOST_ENV, raising=False)
    health = make_server().get_health_info()
    assert health["ws_host"] == "localhost"
    assert health["ws_auth_required"] is False
    assert health["ws_auth_mode"] == AUTH_OFF


# --- remote browser identity --------------------------------------------------------

@pytest.mark.asyncio
async def test_a_remote_peer_skips_the_local_probe_and_is_named_by_its_ip():
    """A browser on another machine has no local process to inspect — the pid/profile
    probe must not even be attempted, and the roster names it `remote-<ip>`."""
    token = mint()
    server = make_server(ws_host="0.0.0.0", ws_token=token)
    ws = FakeWS(peer=("192.168.1.50", 51000))

    with patch.object(server, "_probe_browser_identity") as probe:
        task = asyncio.create_task(server.handle_extension_connection(ws))
        await asyncio.sleep(0.01)
        await secure_fake_hello(ws, server, hello_frame(
            instance_id="ff-remote", platform={"os": "mac", "arch": "aarch64"}))
        # Wait for the registration that WOULD have probed — asserting "not called" before
        # that point would pass against a handshake that had not got there yet.
        await registered(server, "ff-remote")
        probe.assert_not_called()

    browser = server.browsers["ff-remote"]
    assert browser.is_remote is True
    assert browser.remote_addr == "192.168.1.50"
    assert browser.browser_pid is None
    assert browser.name == "remote-192.168.1.50"
    assert browser.describe() == "Firefox [1] remote-192.168.1.50"
    ws.drop()
    await asyncio.wait_for(task, timeout=2.0)


@pytest.mark.asyncio
async def test_a_local_peer_still_gets_the_identity_probe():
    """The counterpart: local browsers keep their pid enrichment."""
    server = make_server()
    ws = FakeWS(peer=("127.0.0.1", 5555))

    ident = {"pid": 4242, "is_firefox": True,
             "friendly": "Firefox Developer Edition", "exe_path": "firefox.exe"}
    with patch.object(server, "_probe_browser_identity", return_value=ident) as probe:
        task = asyncio.create_task(server.handle_extension_connection(ws))
        await wait_until(lambda: probe.called, "the handler probed the local peer's identity")
        probe.assert_called_once()
    ws.feed(hello_frame(instance_id="ff-local"))
    await registered(server, "ff-local")

    browser = server.browsers["ff-local"]
    assert browser.is_remote is False
    assert browser.browser_pid == 4242
    assert browser.name == "Firefox Developer Edition pid 4242"
    ws.drop()
    await asyncio.wait_for(task, timeout=2.0)


# --- the token itself: minted once, persisted, stable -------------------------------

def test_a_token_is_minted_once_and_REUSED_across_restarts(tmp_path, monkeypatch):
    """The whole point of persisting it. A token re-minted on every restart would silently
    invalidate every remote browser the user had configured — the failure would look like
    "my other machine randomly stopped working"."""
    monkeypatch.delenv(WS_TOKEN_ENV, raising=False)
    monkeypatch.setenv(WS_TOKEN_FILE_ENV, str(tmp_path / "ws-token"))

    first = make_server()
    assert first.ws_token_source == SOURCE_GENERATED, "nothing stored yet -> mint"
    assert read_token_file(str(tmp_path / "ws-token")) == first.ws_token

    for _ in range(3):                       # restart, restart, restart
        again = make_server()
        assert again.ws_token == first.ws_token, "a restart must not change the token"
        assert again.ws_token_source == SOURCE_FILE, "it was read back, not re-minted"


def test_the_env_var_overrides_the_persisted_file(tmp_path, monkeypatch):
    """An explicitly configured token always wins — and does NOT overwrite the file, so
    unsetting the env var later brings the persisted one back."""
    monkeypatch.setenv(WS_TOKEN_FILE_ENV, str(tmp_path / "ws-token"))
    monkeypatch.delenv(WS_TOKEN_ENV, raising=False)
    persisted = make_server().ws_token

    pinned = mint()
    monkeypatch.setenv(WS_TOKEN_ENV, pinned)
    server = make_server()
    assert server.ws_token == pinned
    assert server.ws_token_source == SOURCE_ENV
    assert read_token_file(str(tmp_path / "ws-token")) == persisted, "file untouched"

    monkeypatch.delenv(WS_TOKEN_ENV)
    assert make_server().ws_token == persisted


def test_an_explicit_argument_beats_everything(tmp_path, monkeypatch):
    monkeypatch.setenv(WS_TOKEN_FILE_ENV, str(tmp_path / "ws-token"))
    monkeypatch.setenv(WS_TOKEN_ENV, mint())
    explicit = mint()
    server = make_server(ws_token=explicit)
    assert server.ws_token == explicit
    assert server.ws_token_explicit is True


def test_the_token_file_is_owner_only_as_far_as_the_OS_allows(tmp_path):
    """Honest about Windows. POSIX gets a real 0600. NTFS has no mode bits — os.chmod only
    flips the read-only flag and Python cannot write a DACL — so there the file is
    protected by the ACL it INHERITS from the per-user state dir (%LOCALAPPDATA%), and
    perms_are_owner_only() says False rather than pretending otherwise."""
    path = tmp_path / "ws-token"
    token = mint()
    assert write_token_file(token, str(path)) == str(path)
    assert read_token_file(str(path)) == token

    if os.name == "nt":
        assert perms_are_owner_only(str(path)) is False, (
            "Windows must not claim owner-only permissions it cannot enforce")
    else:
        assert perms_are_owner_only(str(path)) is True
        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert mode == 0o600, f"world/group must have no access, got {oct(mode)}"
        assert not mode & (stat.S_IRGRP | stat.S_IROTH)


def test_the_token_file_is_CREATED_owner_only_never_widened_afterwards(tmp_path):
    """The mode has to be right at open() time, not chmod'd a moment later: between the two
    the secret would sit on disk at the umask's mercy. Asserted on the call itself so a
    Windows box — where the bits are inert and os.stat can never catch this — still guards
    the POSIX behavior."""
    path = tmp_path / "ws-token"
    real_open = os.open
    modes = []

    def spy(file, flags, mode=0o777, **kw):
        if str(file) == str(path):
            modes.append(mode)
        return real_open(file, flags, mode, **kw)

    with patch("browserplug_shared.ws_token.os.open", side_effect=spy):
        write_token_file(mint(), str(path))

    assert modes == [0o600], f"the token file must be created 0600, got {[oct(m) for m in modes]}"


def test_the_token_lives_in_a_per_user_state_dir_not_in_temp(monkeypatch):
    """Where it must NOT be: the system temp dir (world-readable on POSIX, wiped on
    reboot — and a token that dies on reboot breaks every configured remote browser)."""
    monkeypatch.delenv(WS_TOKEN_FILE_ENV, raising=False)
    # ...and the state dir the suite redirects into tmp, or this asserts about tmp.
    monkeypatch.delenv(STATE_DIR_ENV, raising=False)
    path = Path(default_token_path())

    import tempfile
    assert tempfile.gettempdir().lower() not in str(path).lower()
    home = str(Path.home()).lower()
    local_appdata = (os.environ.get("LOCALAPPDATA") or "").lower()
    assert str(path).lower().startswith(home) or (
        local_appdata and str(path).lower().startswith(local_appdata))


def test_a_token_that_cannot_be_persisted_still_serves_this_process(tmp_path, monkeypatch):
    """Read-only home / no permission: minting still happens (a local browser can pull it
    from /ws-token and keep working). It just isn't stable across restarts, which the
    caller is told about rather than left to discover."""
    monkeypatch.delenv(WS_TOKEN_ENV, raising=False)
    target = tmp_path / "unwritable" / "ws-token"
    warnings = []
    with patch("browserplug_shared.ws_token.write_token_file", return_value=None):
        token, source = resolve_token(path=str(target), log=warnings.append)
    assert token and source == SOURCE_GENERATED
    assert warnings and "restart" in warnings[0].lower()


def test_the_first_mint_prints_a_device_token_and_never_the_master(tmp_path, monkeypatch):
    """The first run shows a device token a remote browser can paste. The master itself is
    never printed: no browser should hold it (shared-py/browserplug_shared/device_tokens.py)."""
    monkeypatch.delenv(WS_TOKEN_ENV, raising=False)
    # Changed deliberately (S11): the device token goes to the process's own terminal,
    # not through `log`; the log-side contract is in the tests at the end of this file.
    printed = _Tty(True)
    token, source = resolve_token(path=str(tmp_path / "ws-token"), log=lambda m: None,
                                  out=printed)

    assert source == SOURCE_GENERATED
    assert token not in printed.getvalue(), "the master is never printed"
    shown = [w for w in printed.getvalue().split() if device_tokens.parse(w)]
    assert len(shown) == 1 and is_valid_device_token(token, shown[0])

    # ...and NOT again on the next start, when it is merely read back.
    again, logged = _Tty(True), []
    token2, source2 = resolve_token(path=str(tmp_path / "ws-token"), log=logged.append,
                                    out=again)
    assert token2 == token and source2 == SOURCE_FILE
    assert logged == [] and again.getvalue() == ""


# --- who has to authenticate: the enforcement matrix ---------------------------------

def test_auth_mode_matrix(monkeypatch):
    """The rule, stated once. A remote peer is ALWAYS strict; a local one depends on
    whether it could possibly get hold of the token."""
    monkeypatch.delenv(WS_TOKEN_ENV, raising=False)

    no_channel = make_server()                       # --no-mcp: nothing to fetch from
    assert no_channel.ws_auth.auth_mode(peer_local=True) == AUTH_OFF
    assert no_channel.ws_auth.auth_mode(peer_local=False) == AUTH_STRICT

    channel = make_bootstrap_server()                # production: /ws-token exists
    assert channel.ws_auth.auth_mode(peer_local=True) == AUTH_BOOTSTRAP
    assert channel.ws_auth.auth_mode(peer_local=False) == AUTH_STRICT

    configured = make_server(ws_token=mint())        # the user asked for auth
    assert configured.ws_auth.auth_mode(peer_local=True) == AUTH_STRICT
    assert configured.ws_auth.auth_mode(peer_local=False) == AUTH_STRICT

    stdio = BrowserPlugServer(host="localhost", port=8772, start_mcp=True, use_stdio=True)
    assert stdio.ws_auth.token_endpoint_url() is None, "stdio has no HTTP listener to serve it"
    assert stdio.ws_auth.auth_mode(peer_local=True) == AUTH_OFF


@pytest.mark.asyncio
async def test_a_local_extension_without_a_token_is_rejected_TOWARDS_the_endpoint(monkeypatch):
    """Bootstrap mode: the rejection is not a dead end — it is an instruction. The close
    reason names the endpoint (with the server's REAL, possibly dynamic, MCP port), which
    is the only way the extension can find it before it is allowed to talk."""
    monkeypatch.delenv(WS_TOKEN_ENV, raising=False)
    server = make_bootstrap_server()
    ws = FakeWS()

    task = await connect(server, ws)
    ws.feed(hello_frame())                       # new extension, no token yet
    await asyncio.wait_for(task, timeout=2.0)

    assert server.browsers == {}
    assert ws.close_code == WS_CLOSE_UNAUTHORIZED
    assert f"http://127.0.0.1:{server.mcp_port}{WS_TOKEN_ENDPOINT_PATH}" in ws.close_reason
    assert len(ws.close_reason.encode("utf-8")) <= 123, "WebSocket close-reason limit"

    # ...and once it has fetched that token, it is in.
    ws2 = FakeWS()
    task2 = await connect(server, ws2)
    await secure_fake_hello(ws2, server, hello_frame())
    await registered(server, "ff-a")
    assert list(server.browsers) == ["ff-a"]
    assert ws2.close_code is None
    ws2.drop()
    await asyncio.wait_for(task2, timeout=2.0)


@pytest.mark.asyncio
async def test_an_OLD_local_extension_with_no_token_is_no_longer_let_in(monkeypatch):
    """The grace for an extension too old to bootstrap a token (accepted with no token at
    all) ended in 1.9. Next to the encrypted handshake it would be the one way in that
    skips both the token and the encryption."""
    monkeypatch.delenv(WS_TOKEN_ENV, raising=False)
    server = make_bootstrap_server()
    ws = FakeWS()

    task = await connect(server, ws)
    ws.feed(hello_frame(bootstrap=False))        # an extension built before the feature
    await asyncio.wait_for(task, timeout=2.0)

    assert server.browsers == {}
    assert ws.close_code == WS_CLOSE_UNAUTHORIZED
    assert "/ws-token" in ws.close_reason


@pytest.mark.asyncio
async def test_the_old_extension_grace_does_NOT_extend_to_remote_peers(monkeypatch):
    """The grace exists so a local browser is never bricked. A peer on the network has no
    such claim on us — it can be handed the token by hand, and must be."""
    monkeypatch.delenv(WS_TOKEN_ENV, raising=False)
    server = make_bootstrap_server(ws_host="0.0.0.0")
    ws = FakeWS(peer=("192.168.1.50", 51000))

    task = await connect(server, ws)
    ws.feed(hello_frame(bootstrap=False))        # the same "old extension" hello
    await asyncio.wait_for(task, timeout=2.0)

    assert server.browsers == {}
    assert ws.close_code == WS_CLOSE_UNAUTHORIZED
    # A remote peer is never sent to a 127.0.0.1 endpoint it cannot reach.
    assert "127.0.0.1" not in ws.close_reason
    assert "token" in ws.close_reason.lower()


@pytest.mark.asyncio
async def test_the_old_extension_grace_does_NOT_apply_when_a_token_was_configured(monkeypatch):
    """A user who sets BROWSERPLUG_WS_TOKEN is asking for auth. Honor it for everyone —
    they can paste the value into the options page."""
    monkeypatch.delenv(WS_TOKEN_ENV, raising=False)
    server = make_bootstrap_server(ws_token=mint())
    ws = FakeWS()

    task = await connect(server, ws)
    ws.feed(hello_frame(bootstrap=False))
    await asyncio.wait_for(task, timeout=2.0)

    assert server.browsers == {}
    assert ws.close_code == WS_CLOSE_UNAUTHORIZED


@pytest.mark.asyncio
async def test_an_old_extension_that_sends_a_WRONG_token_is_still_rejected(monkeypatch):
    """The grace is for an extension that cannot know the token — not for one that got it
    wrong. Presenting a token at all is a claim to be token-aware."""
    monkeypatch.delenv(WS_TOKEN_ENV, raising=False)
    server = make_bootstrap_server()
    ws = FakeWS()

    task = await connect(server, ws)
    ws.feed(hello_frame(token=mint(), bootstrap=False))
    await asyncio.wait_for(task, timeout=2.0)

    assert server.browsers == {}
    assert ws.close_code == WS_CLOSE_UNAUTHORIZED


# --- the bootstrap channel: GET /ws-token --------------------------------------------

async def http_get(app, path, client=("127.0.0.1", 5555)):
    """Drive the server's ASGI wrapper directly — no port, no uvicorn."""
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": "GET", "path": path, "headers": [], "client": client}
    await app(scope, receive, send)
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, body


async def http_post(app, path, body=b"", headers=(), client=("127.0.0.1", 5555)):
    """POST through the ASGI wrapper. `headers` are (name, value) byte pairs."""
    sent = []

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": path,
             "headers": list(headers), "client": client}
    await app(scope, receive, send)
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    out = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, out


def bind_app(server):
    """The wrapper, with the restart stubbed — the real one calls os._exit()."""
    server._restart_onto = lambda host: None
    return server._wrap_client_tracking(lambda *a: None)


# --- POST /ws-bind: the options page's "accept remote browsers" button ----------------

@pytest.mark.asyncio
async def test_ws_bind_moves_the_socket_off_localhost_and_persists_it(monkeypatch):
    """The button has to STICK: a browser cannot set an env var, and the always-on server
    is started by a scheduled task that would never see one anyway."""
    monkeypatch.delenv(WS_TOKEN_ENV, raising=False)
    server = make_bootstrap_server()
    assert server.ws_host == "localhost"

    status, body = await http_post(
        bind_app(server), "/ws-bind", body=json.dumps({"host": "0.0.0.0"}).encode(),
        headers=signed_headers(server, "POST", "/ws-bind", body=json.dumps({"host": "0.0.0.0"}).encode()))

    assert status == 200
    assert json.loads(body)["ws_accepts_remote"] is True
    assert server.ws_host == "0.0.0.0"
    assert read_ws_host() == "0.0.0.0", "and it survives the restart it just asked for"

    # The next server to start reads it back — that is what "survives" means.
    assert make_bootstrap_server().ws_host == "0.0.0.0"


@pytest.mark.asyncio
async def test_ws_bind_needs_the_token_so_a_web_page_cannot_flip_it(monkeypatch):
    """ANY web page can POST to 127.0.0.1. Exposing the WebSocket to the LAN is the last
    thing a random page should be able to do, so the token is required — and it travels in
    a CUSTOM HEADER, which a cross-origin page cannot send without a CORS preflight this
    app never answers."""
    monkeypatch.delenv(WS_TOKEN_ENV, raising=False)
    server = make_bootstrap_server()

    body = json.dumps({"host": "0.0.0.0"}).encode()
    no_token, _ = await http_post(bind_app(server), "/ws-bind", body=body)
    wrong, _ = await http_post(bind_app(server), "/ws-bind", body=body,
                               headers=signed_headers(server, "POST", "/ws-bind",
                                                      token=device_tokens.issue(mint())))
    # The raw header of extensions before 1.9 is refused even with the master itself.
    raw, _ = await http_post(bind_app(server), "/ws-bind", body=body,
                             headers=[(b"x-browserplug-token", server.ws_token.encode())])

    assert no_token == 403 and wrong == 403 and raw == 403
    assert server.ws_host == "localhost", "the bind never moved"
    assert read_ws_host() is None, "and nothing was persisted"


@pytest.mark.asyncio
async def test_ws_bind_refuses_a_remote_peer_without_the_token(monkeypatch):
    """A remote caller WITH the token is allowed since 2026-09-02 (the port follows the
    LAN bind and the token replaced the peer test), so the refusal worth pinning is the
    tokenless one. The success path is the next test, with the restart stubbed
    (`bind_app`); the real restart is in test_server_process_lifecycle.py."""
    monkeypatch.delenv(WS_TOKEN_ENV, raising=False)
    server = make_bootstrap_server()

    status, _ = await http_post(
        bind_app(server), "/ws-bind", body=json.dumps({"host": "0.0.0.0"}).encode(),
        client=("192.168.1.50", 4444))

    assert status == 403
    assert server.ws_host == "localhost"


@pytest.mark.asyncio
async def test_ws_bind_accepts_a_remote_peer_that_signs_with_a_device_token(monkeypatch):
    """The success half of the test above: a browser on another machine that holds a device
    token can move the bind, since the token and not the peer address is the credential.
    Stubbed restart, scratch state dir (the session fixture), so nothing is rebound."""
    monkeypatch.delenv(WS_TOKEN_ENV, raising=False)
    server = make_bootstrap_server()

    status, body = await http_post(
        bind_app(server), "/ws-bind", body=json.dumps({"host": "0.0.0.0"}).encode(),
        headers=signed_headers(server, "POST", "/ws-bind", body=json.dumps({"host": "0.0.0.0"}).encode()),
        client=("192.168.1.50", 4444))

    assert status == 200, body
    assert json.loads(body)["ws_host"] == "0.0.0.0"
    assert server.ws_host == "0.0.0.0"
    assert read_ws_host() == "0.0.0.0"


@pytest.mark.asyncio
async def test_ws_bind_will_not_fight_the_environment(monkeypatch):
    """BROWSERPLUG_WS_HOST wins at every start. Letting the button "succeed" against it
    would revert on the next restart — a lie is worse than a refusal."""
    monkeypatch.setenv(WS_HOST_ENV, "127.0.0.1")
    monkeypatch.delenv(WS_TOKEN_ENV, raising=False)
    server = make_bootstrap_server()
    assert server.ws_host_source == "env"

    status, body = await http_post(
        bind_app(server), "/ws-bind", body=json.dumps({"host": "0.0.0.0"}).encode(),
        headers=signed_headers(server, "POST", "/ws-bind", body=json.dumps({"host": "0.0.0.0"}).encode()))

    assert status == 409
    assert WS_HOST_ENV in json.loads(body)["error"]
    assert server.ws_host == "127.0.0.1"


def test_the_bind_source_is_reported_so_the_page_knows_who_wins(monkeypatch):
    """arg > env > file > default. The options page disables its button on `env`."""
    monkeypatch.delenv(WS_HOST_ENV, raising=False)
    write_ws_host(None)
    assert make_server().ws_host_source == "default"

    write_ws_host("0.0.0.0")
    assert make_server().ws_host == "0.0.0.0"
    assert make_server().ws_host_source == "file"

    monkeypatch.setenv(WS_HOST_ENV, "192.168.1.9")
    assert make_server().ws_host == "192.168.1.9", "the env outranks the saved file"
    assert make_server().ws_host_source == "env"

    assert make_server(ws_host="10.0.0.1").ws_host == "10.0.0.1", "and --ws-host outranks both"
    assert make_server(ws_host="10.0.0.1").ws_host_source == "arg"
    write_ws_host(None)


# --- the stored LAN-vs-WAN choice (config.json's ws.mode) ----------------------------

def test_ws_mode_round_trip_and_garbage_reads_as_absent():
    assert read_ws_mode() is None, "clean state"
    for mode in ("lan", "wan"):
        assert write_ws_mode(mode) is not None
        assert read_ws_mode() == mode

    assert write_ws_mode(None) is not None
    assert read_ws_mode() is None
    assert "mode" not in json.loads(Path(config_path()).read_text()).get("ws", {}), \
        "None removes the key rather than storing an empty one"

    # A value we never wrote (hand-edited, or a future version's) reads as absent —
    # the caller's default is always a safe answer — and is refused on the write side.
    Path(config_path()).write_text(json.dumps({"ws": {"mode": "tor"}}), encoding="utf-8")
    assert read_ws_mode() is None
    assert write_ws_mode("tor") is None, "an unknown mode is refused"
    assert json.loads(Path(config_path()).read_text())["ws"]["mode"] == "tor", \
        "and the refusal touched nothing"


@pytest.mark.asyncio
async def test_ws_bind_accepts_a_mode_and_persists_it_beside_the_host(monkeypatch):
    """LAN and WAN both bind 0.0.0.0 — the mode is the user's stated intent, and it has
    to survive the restart the bind triggers, exactly like the host does."""
    monkeypatch.delenv(WS_TOKEN_ENV, raising=False)
    server = make_bootstrap_server()

    status, body = await http_post(
        bind_app(server), "/ws-bind",
        body=json.dumps({"host": "0.0.0.0", "mode": "wan"}).encode(),
        headers=signed_headers(server, "POST", "/ws-bind", body=json.dumps({"host": "0.0.0.0", "mode": "wan"}).encode()))

    assert status == 200
    assert json.loads(body)["ws_mode"] == "wan", "the response says what /health will"
    assert read_ws_mode() == "wan"

    # The replacement process re-reads both files — that is the whole persistence story.
    fresh = make_bootstrap_server()
    assert fresh.ws_host == "0.0.0.0"
    assert fresh.current_ws_mode() == "wan"


@pytest.mark.asyncio
async def test_ws_bind_rejects_a_bad_mode(monkeypatch):
    monkeypatch.delenv(WS_TOKEN_ENV, raising=False)
    server = make_bootstrap_server()

    status, body = await http_post(
        bind_app(server), "/ws-bind",
        body=json.dumps({"host": "0.0.0.0", "mode": "tor"}).encode(),
        headers=signed_headers(server, "POST", "/ws-bind", body=json.dumps({"host": "0.0.0.0", "mode": "tor"}).encode()))

    assert status == 400
    assert "mode" in json.loads(body)["error"]
    assert server.ws_host == "localhost", "the bind never moved"
    assert read_ws_host() is None and read_ws_mode() is None


@pytest.mark.asyncio
async def test_ws_bind_revert_to_localhost_clears_the_mode(monkeypatch):
    """Local is derived, never stored — a revert must not leave a stale 'wan' behind
    to resurrect the moment some future bind goes non-local."""
    monkeypatch.delenv(WS_TOKEN_ENV, raising=False)
    server = make_bootstrap_server()

    await http_post(bind_app(server), "/ws-bind",
                    body=json.dumps({"host": "0.0.0.0", "mode": "wan"}).encode(),
                    headers=signed_headers(server, "POST", "/ws-bind", body=json.dumps({"host": "0.0.0.0", "mode": "wan"}).encode()))
    assert read_ws_mode() == "wan"

    status, body = await http_post(bind_app(server), "/ws-bind",
                                   body=json.dumps({"host": "localhost"}).encode(),
                                   headers=signed_headers(server, "POST", "/ws-bind", body=json.dumps({"host": "localhost"}).encode()))
    assert status == 200
    assert json.loads(body)["ws_mode"] == "local"
    assert read_ws_mode() is None, "reverting cleared the stored mode"
    assert server.current_ws_mode() == "local"


@pytest.mark.asyncio
async def test_ws_bind_without_mode_keeps_the_stored_mode(monkeypatch):
    """An options page from before the mode existed POSTs only {host} — it must not
    silently demote a WAN server to LAN."""
    monkeypatch.delenv(WS_TOKEN_ENV, raising=False)
    server = make_bootstrap_server()
    write_ws_mode("wan")

    status, _ = await http_post(
        bind_app(server), "/ws-bind", body=json.dumps({"host": "0.0.0.0"}).encode(),
        headers=signed_headers(server, "POST", "/ws-bind", body=json.dumps({"host": "0.0.0.0"}).encode()))

    assert status == 200
    assert read_ws_mode() == "wan"
    assert server.current_ws_mode() == "wan"


def test_health_ws_mode_local_for_localhost_else_stored_defaulting_lan(monkeypatch):
    monkeypatch.delenv(WS_HOST_ENV, raising=False)
    assert make_server().get_health_info()["ws_mode"] == "local"

    # 0.0.0.0 is NOT local (it accepts the network), and with no stored choice the
    # honest default is the milder claim: LAN.
    open_server = make_server(ws_host="0.0.0.0", ws_token=mint())
    assert open_server.get_health_info()["ws_mode"] == "lan"

    write_ws_mode("wan")
    assert open_server.get_health_info()["ws_mode"] == "wan", "re-read per call, not cached"


def test_health_ws_mode_under_an_env_bind(monkeypatch):
    """BROWSERPLUG_WS_HOST wins for WHERE the socket binds; the stored mode still
    qualifies a non-local env bind. An env bind back to loopback is simply local —
    a stale 'wan' file must not contradict the actual bind."""
    monkeypatch.setenv(WS_TOKEN_ENV, mint())
    write_ws_mode("wan")

    monkeypatch.setenv(WS_HOST_ENV, "192.168.1.9")
    assert make_server().get_health_info()["ws_mode"] == "wan"

    monkeypatch.setenv(WS_HOST_ENV, "127.0.0.1")
    assert make_server().get_health_info()["ws_mode"] == "local"


@pytest.mark.asyncio
async def test_ws_token_endpoint_returns_the_token_to_a_local_peer(monkeypatch):
    monkeypatch.delenv(WS_TOKEN_ENV, raising=False)
    server = make_bootstrap_server()

    async def downstream(scope, receive, send):       # pragma: no cover - must not run
        raise AssertionError("/ws-token must be handled by the wrapper")

    app = server._wrap_client_tracking(downstream)
    status, body = await http_get(app, WS_TOKEN_ENDPOINT_PATH)

    assert status == 200
    payload = json.loads(body)
    # A device token, never the master, and a fresh one per call: every browser its own.
    assert payload["token"] != server.ws_token
    assert is_valid_device_token(server.ws_token, payload["token"])
    status2, body2 = await http_get(app, WS_TOKEN_ENDPOINT_PATH)
    assert json.loads(body2)["token"] != payload["token"]
    assert payload["ws_port"] == server.port


@pytest.mark.asyncio
async def test_ws_token_endpoint_refuses_a_non_local_peer(monkeypatch):
    """Defense in depth: the MCP app is bound to localhost, so this should be unreachable
    anyway — but a token served to a peer that has not authenticated is not a secret, and
    that must be true even if someone rebinds the HTTP app one day."""
    monkeypatch.delenv(WS_TOKEN_ENV, raising=False)
    server = make_bootstrap_server()
    app = server._wrap_client_tracking(lambda *a: None)

    status, body = await http_get(app, WS_TOKEN_ENDPOINT_PATH, client=("192.168.1.50", 4444))

    assert status == 403
    assert server.ws_token not in body.decode()


@pytest.mark.asyncio
async def test_the_token_is_not_in_health(monkeypatch):
    monkeypatch.delenv(WS_TOKEN_ENV, raising=False)
    server = make_bootstrap_server()
    app = server._wrap_client_tracking(lambda *a: None)

    status, body = await http_get(app, "/health")
    assert status == 200
    assert server.ws_token not in body.decode(), "/health is not an authentication bypass"


@pytest.mark.asyncio
async def test_the_endpoint_only_exists_on_the_localhost_bound_http_app(monkeypatch):
    """The bootstrap channel is HTTP-only, by construction. There is no way to ask the
    WebSocket for the token (that would defeat the point), and the HTTP app it lives on
    only ever binds `host`, which main() pins to localhost."""
    monkeypatch.delenv(WS_TOKEN_ENV, raising=False)
    server = make_bootstrap_server(ws_host="0.0.0.0")   # WS exposed to the LAN...

    assert server.host == "localhost", "...the HTTP app is not"
    assert server.ws_auth.token_endpoint_url() == \
        f"http://127.0.0.1:{server.mcp_port}{WS_TOKEN_ENDPOINT_PATH}"

    # The WS protocol has no token-serving action: an unauthenticated peer's frames are
    # held and never answered (test_frames_before_the_hello_are_held_then_replayed), and
    # nothing in the handler reads self.ws_token except the comparison.
    ws = FakeWS(peer=("192.168.1.50", 51000))
    task = await connect(server, ws)
    ws.feed({"id": "probe", "type": "request", "action": "server.health", "data": {}})
    await asyncio.sleep(0.01)
    assert ws.sent == []
    ws.drop()
    await asyncio.wait_for(task, timeout=2.0)


@pytest.mark.asyncio
async def test_end_to_end_bootstrap_over_real_sockets(monkeypatch, tmp_path):
    """The whole handshake, with a real WebSocket and a real HTTP listener — everything the
    extension does, minus Firefox:

        connect with no token -> 4401 + "GET http://127.0.0.1:<port>/ws-token"
        -> fetch that URL -> reconnect with the token -> registered.
    """
    import websockets

    monkeypatch.delenv(WS_TOKEN_ENV, raising=False)
    monkeypatch.setenv(WS_TOKEN_FILE_ENV, str(tmp_path / "ws-token"))

    # Ports of this test's own, so it can never be pointed at another server: fetching the
    # token from a *different* server's /ws-token asserts against the wrong secret — a
    # confusing failure at best, a false pass at worst. (port_coordinator now allocates
    # genuinely-free, per-session ports too; this stays self-contained on purpose.)
    def _free_port():
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    ws_port, mcp_port = _free_port(), _free_port()
    server = BrowserPlugServer(host="localhost", port=ws_port, mcp_port=mcp_port,
                          start_mcp=True)
    await server.start_mcp_server()
    assert server.mcp_port == mcp_port, "the MCP app must own the port we fetch the token from"
    ws_server = await websockets.serve(server.handle_extension_connection,
                                       server.ws_host, server.port)
    url = f"ws://localhost:{server.port}"
    try:
        # 1. A brand-new extension knows no token. It is turned away — with directions.
        async with websockets.connect(url) as sock:
            await sock.send(json.dumps(hello_frame()))
            with pytest.raises(websockets.exceptions.ConnectionClosed) as closed:
                await asyncio.wait_for(sock.recv(), timeout=5.0)
        assert closed.value.rcvd.code == WS_CLOSE_UNAUTHORIZED
        reason = closed.value.rcvd.reason
        assert server.browsers == {}

        # 2. It follows them (extension: parseTokenEndpoint + fetch).
        endpoint = re.search(r"https?://\S+", reason).group(0)
        assert endpoint == f"http://127.0.0.1:{server.mcp_port}{WS_TOKEN_ENDPOINT_PATH}"
        with urllib.request.urlopen(endpoint, timeout=5) as response:
            token = json.loads(response.read())["token"]
        assert is_valid_device_token(server.ws_token, token), "a device token of this server's"

        # 3. And comes back with it, through the encrypted handshake.
        async with websockets.connect(url) as sock:
            await SecureClient.connect(sock, token, hello_frame())
            # A REAL socket and a real serve loop: how long the hello takes to cross them
            # is not ours to guess, and 0.3s of it was the flakiest bet in this file.
            await registered(server, "ff-a", timeout=5.0)
            assert list(server.browsers) == ["ff-a"], "the bootstrapped browser is in"
            assert server.browsers["ff-a"].version == "1.4.0"
    finally:
        ws_server.close()
        await ws_server.wait_closed()
        server.lifecycle.stop_mcp_server()


# --- gen_ws_token.py ----------------------------------------------------------------

def test_generate_token_is_long_and_never_repeats():
    tokens = {gen_ws_token.generate_token() for _ in range(20)}
    assert len(tokens) == 20, "every call must mint a fresh token"
    for token in tokens:
        assert len(token) >= 32
        assert re.fullmatch(r"[A-Za-z0-9_-]+", token), "URL-safe alphabet"


def run_gen_script(*args, env=None):
    proc = subprocess.run([sys.executable, str(REPO_ROOT / "gen_ws_token.py"), *args],
                          capture_output=True, text=True, timeout=30,
                          env={**os.environ, **(env or {})})
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def printed_tokens(out):
    """The token is the only long URL-safe run on a line of its own."""
    return [line.strip() for line in out.splitlines()
            if re.fullmatch(r"\s*[A-Za-z0-9_.-]{32,}\s*", line)]


def test_the_script_prints_a_usable_token_and_the_setup_steps():
    out = run_gen_script()
    printed = printed_tokens(out)
    assert len(printed) == 1, f"expected exactly one token line, got {printed}"
    assert len(printed[0]) >= 32

    assert WS_TOKEN_ENV in out and WS_HOST_ENV in out
    assert "--device" in out                 # browsers get device tokens, not this value

    assert run_gen_script() != out, "two runs must not print the same token"


def test_the_script_makes_a_device_token_from_the_persisted_master(tmp_path):
    path = tmp_path / "ws-token"
    env = {WS_TOKEN_FILE_ENV: str(path)}
    run_gen_script("--write", env=env)
    master = read_token_file(str(path))
    out = run_gen_script("--device", env=env)
    devices = printed_tokens(out)
    assert len(devices) == 1 and is_valid_device_token(master, devices[0])
    assert run_gen_script("--device", env=env) != out, "a new label every time"


def test_the_script_is_a_ROTATION_tool_and_says_so():
    """It is no longer the way to GET a token — the server mints one by itself. It is how
    you REPLACE that token, and printing without --write must change nothing."""
    out = run_gen_script().lower()
    assert "rotate" in out or "replace" in out
    assert "--write" in out, "the way to actually rotate must be spelled out"
    assert "nothing was changed" in out, "a bare run must be a no-op, and say so"


def test_the_script_can_rotate_the_persisted_token(tmp_path):
    """--write replaces the server's token file, so the next server start picks the new
    value up. (And a same-machine browser re-pulls it from /ws-token by itself.)"""
    path = tmp_path / "ws-token"
    env = {WS_TOKEN_FILE_ENV: str(path)}

    first = run_gen_script("--write", env=env)
    token = read_token_file(str(path))
    assert token and str(path) in first
    assert token not in first, "the master is never printed"
    device = printed_tokens(first)[0]
    assert is_valid_device_token(token, device), "it prints a device token of the new master"

    run_gen_script("--write", env=env)
    rotated = read_token_file(str(path))
    assert rotated != token, "rotation means a NEW value"
    assert not is_valid_device_token(rotated, device), "and revokes every old device token"

    # A server started now demands exactly the rotated token — nothing else.
    with patch.dict(os.environ, {WS_TOKEN_FILE_ENV: str(path)}, clear=False):
        os.environ.pop(WS_TOKEN_ENV, None)
        assert make_server().ws_token == rotated


def test_write_is_refused_while_the_environment_pins_a_token(tmp_path):
    """The environment value beats the file, so --write would revoke nothing and any device
    token derived from the new file master would be rejected: it refuses and prints none."""
    path = tmp_path / "ws-token"
    pinned = gen_ws_token.generate_token()
    env = {**os.environ, WS_TOKEN_FILE_ENV: str(path), WS_TOKEN_ENV: pinned}
    proc = subprocess.run([sys.executable, str(REPO_ROOT / "gen_ws_token.py"), "--write"],
                          capture_output=True, text=True, timeout=30, env=env)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert not path.exists(), "the file must not be written"
    assert WS_TOKEN_ENV in proc.stdout and "overrides" in proc.stdout
    assert "still in force" in proc.stdout
    assert printed_tokens(proc.stdout) == [], "no device token the server would reject"
    assert pinned not in proc.stdout


# What the token-literal scan walks: every text file the repo versions. The skips mirror
# .gitignore (build output, environments, caches, logs, dated notes, local tool state)
# plus third-party vendor/ code, so a new directory is covered the day it appears
# instead of waiting to be added to a list.
_SCAN_SKIP_DIRS = {"vendor", "node_modules", "__pycache__", "dist", ".venv", ".tmp", ".temp",
                   ".git", ".pytest_cache", ".vscode", ".idea", "logs"}
_SCAN_SKIP_ROOT_DIRS = {"graft"}      # and feedback*: both are ignored at the root only
_SCAN_SKIP_FILES = {"package-lock.json", "kilo.json"}
_SCAN_SUFFIXES = {".py", ".js", ".mjs", ".cjs", ".html", ".css", ".json", ".md", ".txt",
                  ".toml", ".ini", ".sh", ".ps1", ".cmd", ".vbs", ".yml", ".yaml"}


def _repo_text_files():
    for dirpath, dirnames, filenames in os.walk(REPO_ROOT):
        here = Path(dirpath)
        at_root = here == REPO_ROOT
        dirnames[:] = [d for d in dirnames if d not in _SCAN_SKIP_DIRS and not (
            at_root and (d in _SCAN_SKIP_ROOT_DIRS or d.startswith("feedback")))]
        in_claude = here == REPO_ROOT / ".claude"
        if in_claude:
            dirnames[:] = []            # .claude/* is ignored except settings.json
        for name in filenames:
            if name in _SCAN_SKIP_FILES or (at_root and name.startswith("feedback")):
                continue
            if in_claude and name != "settings.json":
                continue
            if name == "Makefile" or Path(name).suffix.lower() in _SCAN_SUFFIXES:
                yield here / name


def test_the_token_scan_covers_the_whole_tree():
    """A scan that lists its directories misses whichever one nobody added. Pin the
    coverage both ways: every kind of versioned text is in, generated and third-party
    trees are out."""
    rel = {p.relative_to(REPO_ROOT).as_posix() for p in _repo_text_files()}
    for must in ("AGENTS.md", "README.md", "docs/predefined-scripts.md", "shared-py/browserplug_shared/ws_token.py",
                 "extension-gecko/background.js", "extension-chromium/background.js",
                 "tests/unit/test_ws_token.py", "Makefile", "package.json",
                 "launcher/launcher.py", "server/runtime/lifecycle.py",
                 "server/web/http_endpoints.py"):
        assert must in rel, f"the token scan no longer covers {must}"
    for never in ("node_modules/", ".venv/", ".tmp/", "dist/", "graft/", "feedback",
                  "vendor/"):
        assert not any(r.startswith(never) or f"/{never}" in r for r in rel), never


# One tokenizer for JS-family text: strings and comments are consumed and dropped so that
# only bare identifiers come out. A secret pasted inside a string therefore never lands in
# the identifier set that exempts it.
_JS_LEXEME = re.compile(
    r"""(?P<skip>'(?:\\.|[^'\\\n])*'|"(?:\\.|[^"\\\n])*"|`(?:\\.|[^`\\])*`"""
    r"""|//[^\n]*|/\*.*?\*/)|(?P<id>[A-Za-z_$][\w$]*)""", re.S)

_IDENTIFIER_DIRS = ("server", "launcher", "shared-py", "tests", "extension-gecko",
                    "extension-chromium")


_SCREAMING_NAME = re.compile(r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+")


def _screaming_names(quoted):
    """The content of a string literal (quotes included in `quoted`) when it is exactly a
    SCREAMING_SNAKE name. Hex and base64 secrets never fit: no underscore-joined caps."""
    inner = quoted.strip("'\"`")
    return [inner] if _SCREAMING_NAME.fullmatch(inner) else []


def _source_identifiers():
    """Every identifier the source tree spells outside a string or comment.

    A long name on a line about tokens (a constant, a test name, an env var) is a name,
    not a secret. Deriving the exemption from the code keeps it from growing by hand each
    time a doc mentions one. Python goes through `tokenize` and JS through _JS_LEXEME, so
    a value written inside a string never counts as an identifier. The one string that does
    count is a bare SCREAMING_SNAKE name (`WS_TOKEN_ENV = "BROWSERPLUG_WS_TOKEN"`): that is
    how the code defines an environment variable's name."""
    import io
    import tokenize
    names = set()
    for path in _repo_text_files():
        rel = path.relative_to(REPO_ROOT).parts
        if not rel or rel[0] not in _IDENTIFIER_DIRS:
            continue
        suffix = path.suffix.lower()
        if suffix not in {".py", ".js", ".mjs", ".cjs"}:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if suffix == ".py":
            try:
                for tok in tokenize.generate_tokens(io.StringIO(text).readline):
                    if tok.type == tokenize.NAME:
                        names.add(tok.string)
                    elif tok.type == tokenize.STRING:
                        names.update(_screaming_names(tok.string))
            except (tokenize.TokenError, IndentationError, SyntaxError):
                continue
        else:
            for m in _JS_LEXEME.finditer(text):
                names.add(m.group("id")) if m.group("id") else names.update(
                    _screaming_names(m.group("skip")))
    return names


# A quoted or backticked literal of >=20 URL-safe chars, on a line that mentions a token.
_QUOTED_LITERAL = re.compile(r"""['"`]([A-Za-z0-9_-]{20,})['"`]""")
# KEY=value with no spaces around "=": the shape of an env-file line, a shell export or a
# command-line flag, where the value is often NOT quoted and the rule above sees nothing.
# `.`, `(` and `$` stay out of the value class, so `token=secrets.token_urlsafe(32)` and
# `TOKEN=$(python gen_ws_token.py)` are not values.
_KEY_VALUE = re.compile(
    r"""\b[A-Za-z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY)[A-Za-z0-9_]*=['"`]?"""
    r"""([A-Za-z0-9_+/-]{16,})(?![\w.(])""", re.I)
# A credential riding in a URL query string.
_QUERY_VALUE = re.compile(
    r"""[?&][\w-]*(?:token|secret|password|passwd|api_?key)[\w-]*=([A-Za-z0-9_%~+/.-]{16,})""",
    re.I)
# A name, not a secret, that no identifier in the tree spells: HTTP header names carry
# hyphens, so background.js's ALWAYS_REDACTED_HEADERS floor entry never shows up as a
# bare identifier. It exists to keep credentials OUT of an agent's context.
_HYPHENATED_NAMES = {"x-amz-security-token"}


def _looks_random(value):
    """A secret mixes letters and digits; a variable name or a path segment rarely does."""
    return any(c.isdigit() for c in value) and any(c.isalpha() for c in value)


def _token_offenders(lines, identifiers):
    """The (lineno, line) pairs in `lines` that carry a hard-coded credential."""
    out = []
    for lineno, line in enumerate(lines, 1):
        hit = False
        if "token" in line.lower():
            hit = any(m.group(1) not in identifiers and m.group(1) not in _HYPHENATED_NAMES
                      for m in _QUOTED_LITERAL.finditer(line))
        if not hit:
            hit = any(_looks_random(m.group(1)) and m.group(1) not in identifiers
                      for rx in (_KEY_VALUE, _QUERY_VALUE) for m in rx.finditer(line))
        if hit:
            out.append((lineno, line.strip()))
    return out


def test_no_literal_token_in_the_repo():
    """PROJECT RULE: no token value is ever hand-written, not in code, docs, configs or
    tests. Everything mints one at runtime (`secrets`) or points at gen_ws_token.py.

    Flags a quoted token-shaped literal on a line about tokens, an unquoted
    KEY=value assignment (TOKEN, SECRET, PASSWORD, API_KEY) and a `?token=` query string.
    A name the source tree itself spells as an identifier is exempt: it is a name."""
    identifiers = _source_identifiers()
    offenders = []
    for path in _repo_text_files():
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for lineno, line in _token_offenders(text.splitlines(), identifiers):
            offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {line}")

    assert not offenders, ("a literal token value must never be written down:\n"
                           + "\n".join(offenders))


def test_the_identifier_set_is_built_from_code_not_strings():
    ids = _source_identifiers()
    assert "MAX_TOKEN_BOOTSTRAP_ATTEMPTS" in ids
    assert "test_no_literal_token_in_the_repo" in ids
    # A value that only ever appears inside a string must not be exempted by that fact.
    probe = "Zq9" + secrets.token_hex(12)
    assert probe not in ids
    lexed = _JS_LEXEME.finditer(f"const a = '{probe}'; // {probe}\nlet realName = 1;")
    assert {m.group("id") for m in lexed if m.group("id")} == {"const", "a", "let", "realName"}


def _fake_secret():
    return "a1" + secrets.token_hex(12)


@pytest.mark.parametrize("template", [
    "API_TOKEN={v}",
    "export BROWSERPLUG_SECRET={v}",
    "db_password={v}",
    'MY_API_KEY="{v}"',
    "curl http://127.0.0.1:2830/mcp?token={v}",
    "ws://host:8772/?a=1&auth_token={v}",
    'the token is "{v}"',
    "the token is `{v}`",
])
def test_the_scanner_flags_an_injected_credential(template):
    line = template.format(v=_fake_secret())
    assert _token_offenders([line], set()), line


@pytest.mark.parametrize("line", [
    "token=secrets.token_urlsafe(32)",
    "BROWSERPLUG_WS_TOKEN=$(python gen_ws_token.py)",
    "WS_TOKEN=short",
    "?token={token}",
    "url = base + '?token=' + encodeURIComponent(token)",
    "PASSWORD=some_long_variable_name_only",
    "the token file is named ws-token",
    "x=1&y=2",
])
def test_the_scanner_ignores_names_placeholders_and_code(line):
    assert not _token_offenders([line], set()), line


def test_an_identifier_the_tree_spells_is_exempt_only_when_it_is_one():
    name = "SOME_LONG_CONSTANT_NAME_9"
    line = f'the token limit is "{name}"'
    assert not _token_offenders([line], {name})
    assert _token_offenders([line], set())
    assert not _token_offenders([f"API_TOKEN={name}"], {name})


# --- S11: the first-run banner must not put a working token into a log ----------------

class _Tty(io.StringIO):
    def __init__(self, tty):
        super().__init__()
        self._tty = tty

    def isatty(self):
        return self._tty


def test_the_first_mint_never_writes_any_working_token_to_the_log(tmp_path, monkeypatch):
    """The log handler writes to a file that outlives the process and gets shared. The
    banner used to carry a device token there (a working credential)."""
    monkeypatch.delenv(WS_TOKEN_ENV, raising=False)
    logged = []
    out = _Tty(False)
    token, _ = resolve_token(path=str(tmp_path / "ws-token"), log=logged.append, out=out)
    text = "\n".join(logged)
    assert token not in text
    assert not [w for w in text.split() if device_tokens.parse(w)], "no device token in the log"
    assert out.getvalue() == "", "not a terminal: nothing is printed either"
    assert str(tmp_path / "ws-token") in text, "the log names where the token lives"
    assert "gen_ws_token.py" in text


def test_the_first_mint_prints_the_device_token_to_a_terminal_only(tmp_path, monkeypatch):
    monkeypatch.delenv(WS_TOKEN_ENV, raising=False)
    logged = []
    out = _Tty(True)
    token, _ = resolve_token(path=str(tmp_path / "ws-token"), log=logged.append, out=out)
    shown = [w for w in out.getvalue().split() if device_tokens.parse(w)]
    assert len(shown) == 1 and is_valid_device_token(token, shown[0])
    assert token not in out.getvalue()
    assert shown[0] not in "\n".join(logged)


def test_scrub_logs_redacts_only_the_banner_token_line():
    from server.runtime.scrub_logs import scrub_text
    fake = "A" * 43
    log = ("2026-01-01 WARNING:server.server:\r\n  ----\r\n"
           "  BrowserPlug generated a WebSocket token for this server (first run).\r\n"
           "  Stored in: /x/ws-token\r\n\r\n      " + fake + "\r\n\r\n"
           "2026-01-01 INFO:x:" + fake + " unrelated\r\n")
    new, n = scrub_text(log)
    assert n == 1 and "      [token redacted]\r\n" in new
    assert new.count(fake) == 1, "only the banner line changed"
    assert scrub_text(new)[1] == 0
