"""
Unit tests for the multi-Firefox browser registry (no Firefox, no psutil).

The server used to keep ONE extension slot: a second Firefox connecting kicked the
first off, and both kept kicking each other (flip-flop). It now keeps a REGISTRY keyed
by the extension's stable instance_id:

  * same instance_id reconnecting  -> displaces that browser's socket (restart/reload)
  * a different instance_id        -> a new registry entry; nobody is kicked
  * a disconnect                   -> fails only THAT browser's in-flight requests

Single-browser behavior must be byte-for-byte what it was, which is what the compat
properties (extension_connection / _version / _file_access / _proxy_access) and the
legacy /health scalars are for: they all resolve to the DEFAULT browser (first
connected, still alive).
"""

import asyncio
import json
import time
from unittest.mock import patch

import pytest

from server.reporting import client_identity
from server.browsers.browser_identity import (ExtBrowser, _gecko_dir_cache,
                                              fork_from_browser_info, gecko_variant)
from brand_probe import gecko_brand
from server.server import BrowserPlugServer

_EOF = object()

# One browser process behind a reconnect: the proof a takeover of a live record needs.
FF_A = {"pid": 4101, "friendly": "Firefox"}


class FakeWS:
    """A WebSocket the connection handler can iterate, fed message by message.

    It can also be `recv()`d, one frame at a time: a REMOTE peer is always authenticated
    before it is registered (server.auth_mode), and that path reads the socket directly.
    """

    def __init__(self, peer=("127.0.0.1", 5555)):
        self.remote_address = peer
        self.close_code = None
        self.sent = []
        self._q = asyncio.Queue()

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    async def recv(self):
        item = await self._q.get()
        if item is _EOF:
            raise ConnectionError("connection closed")
        return item

    async def close(self, code=1000, reason=""):
        if self.close_code is None:
            self.close_code = code
        self._q.put_nowait(_EOF)

    def feed(self, message):
        self._q.put_nowait(json.dumps(message))

    def drop(self, code=1006):
        """The peer vanished (Firefox closed / crashed)."""
        self.close_code = code
        self._q.put_nowait(_EOF)

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self._q.get()
        if item is _EOF:
            raise StopAsyncIteration
        return item


@pytest.fixture
def server(monkeypatch):
    # An idempotent request whose browser drops waits out the reconnect grace before it
    # fails (8 s by default). No test here reconnects inside that window, so shrink it.
    monkeypatch.setenv("BROWSERPLUG_RECONNECT_GRACE", "0.05")
    return BrowserPlugServer(host="localhost", port=8772, start_mcp=False)


async def wait_until(predicate, what, timeout=2.0, interval=0.01):
    """Poll `predicate` until it holds — the bounded-poll idiom test_command_timings.py
    uses, factored out (test_browser_routing.py imports it too).

    Every use replaces a fixed sleep that was really waiting for an async step to LAND
    (a send reaching a socket, a close task running). A fixed sleep is a bet that the
    step finishes inside N ms: it wins on an idle box and silently asserts against
    half-finished state on a loaded one. This exits the moment the condition is true, and
    fails naming what never happened rather than letting the assertion below misreport it.
    """
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out after {timeout}s waiting until {what}")
        await asyncio.sleep(interval)


async def connect(server, ws, identity=None):
    """Run the real connection handler for `ws`, with the local Firefox probe stubbed
    (it is the only psutil dependency). Returns the handler task."""
    with patch.object(server, "_probe_browser_identity", return_value=identity or {}):
        task = asyncio.create_task(server.handle_extension_connection(ws))
        # Let it register the socket. The peer probe now runs on an executor (finding C7),
        # so registration takes a thread hop: wait for it instead of guessing a delay.
        deadline = asyncio.get_running_loop().time() + 2.0
        while (server.registry.browser_for_ws(ws) is None and not task.done()
               and asyncio.get_running_loop().time() < deadline):
            await asyncio.sleep(0.005)
        await asyncio.sleep(0.01)
    return task


async def hello(server, ws, **fields):
    """Deliver an extension_hello on `ws` (fields are the hello payload, all optional)."""
    ws.feed({"type": "extension_hello", "data": fields})
    await asyncio.sleep(0.01)


async def hello_processed(ws):
    """Let the server read a sealed hello and register the browser. A fixed 0.01 s was too
    short on 3.11: wait for the frame to be consumed, then give the handler a beat."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 5.0
    while not ws._q.empty() and loop.time() < deadline:
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.3)


async def remote_hello(server, ws, **fields):
    """How a REMOTE browser logs in: always authenticated (auth_mode), through the encrypted
    handshake with a device token of this server's, then the hello as a sealed record."""
    from secure_ext import secure_fake_hello
    await secure_fake_hello(ws, server, {"type": "extension_hello",
                                         "data": {"tokenBootstrap": True, **fields}})
    await hello_processed(ws)


async def disconnect(ws, task):
    ws.drop()
    await asyncio.wait_for(task, timeout=2.0)
    await asyncio.sleep(0)  # futures settle on their owning loop's next tick


def send(server, request_id, browser, timeout=5.0, action="tabs.list"):
    # action is overridable so the disconnect/displacement SCOPING tests below can use a
    # NON-idempotent action for the call that gets dropped: the server auto-retries an
    # idempotent read (tabs.list is one) once on a transient disconnect, which would make the
    # dropped call wait for a reconnect instead of failing immediately. The retry path (and
    # its own multi-browser scoping) is covered by tests/unit/test_disconnect_retry.py.
    return asyncio.create_task(server.send_request_and_wait(
        {"id": request_id, "type": "request", "action": action, "data": {}},
        timeout=timeout, browser=browser))


# --- registry: add / displace / coexist / remove ------------------------------------

@pytest.mark.asyncio
async def test_hello_registers_the_browser(server):
    """The hello the extension actually sends (background.js sendExtensionHello):
    snake_case instance_id, plus platform/browserInfo."""
    ws = FakeWS()
    task = await connect(server, ws)
    await hello(server, ws, instance_id="ff-a", version="1.4.0",
                fileAccess={"can_read_files": True}, proxyAccess={"has_proxy_permission": True},
                platform={"os": "win", "arch": "x86-64"},
                browserInfo={"name": "Firefox", "version": "152.0"})

    assert list(server.browsers) == ["ff-a"]
    browser = server.browsers["ff-a"]
    assert browser.slot == 1
    assert browser.provisional is False
    assert browser.version == "1.4.0"
    assert browser.file_access == {"can_read_files": True}
    assert browser.proxy_access == {"has_proxy_permission": True}
    assert browser.platform == {"os": "win", "arch": "x86-64"}
    assert browser.browser_info == {"name": "Firefox", "version": "152.0"}
    assert server.default_browser is browser

    await disconnect(ws, task)


@pytest.mark.asyncio
async def test_api_support_is_stored_and_absence_stays_unknown(server):
    """A probed capability must survive as three states, not two.

    None means the extension never reported it (a build predating the probe). Collapsing
    that to False would tell a caller the API is missing on a browser that has it — the
    same confidently-wrong shape the file-access hint was created to remove.
    """
    ws = FakeWS()
    task = await connect(server, ws)
    await hello(server, ws, instance_id="ff-a", apiSupport={"documentId": True})
    assert server.browsers["ff-a"].api_support == {"documentId": True}
    await disconnect(ws, task)

    ws2 = FakeWS()
    task2 = await connect(server, ws2)
    await hello(server, ws2, instance_id="ff-old", version="1.4.0")
    assert server.browsers["ff-old"].api_support is None, "absent must not become False"
    await disconnect(ws2, task2)


@pytest.mark.asyncio
async def test_hello_also_accepts_camelcase_instance_id(server):
    """Belt and braces: the identity key is read under either spelling."""
    ws = FakeWS()
    task = await connect(server, ws)
    await hello(server, ws, instanceId="ff-a", version="1.4.0")

    assert list(server.browsers) == ["ff-a"]

    await disconnect(ws, task)


@pytest.mark.asyncio
async def test_same_instance_id_displaces_that_browsers_socket(server):
    old = FakeWS(peer=("127.0.0.1", 1111))
    old_task = await connect(server, old, identity=FF_A)
    await hello(server, old, instanceId="ff-a", version="1.4.0")
    established = server.browsers["ff-a"]

    # That Firefox restarts / reloads the extension: same instance_id, new socket.
    new = FakeWS(peer=("127.0.0.1", 2222))
    new_task = await connect(server, new, identity=FF_A)
    await hello(server, new, instanceId="ff-a", version="1.4.1")

    assert list(server.browsers) == ["ff-a"], "the reconnect must not add a second entry"
    browser = server.browsers["ff-a"]
    assert browser is established, "the established record (and its history) is kept"
    assert browser.slot == 1, "the slot is stable across reconnects"
    assert browser.connect_count == 2
    assert browser.version == "1.4.1"
    assert browser.ws is new
    assert server.extension_connection is new
    assert old.close_code is not None, "the displaced socket must be closed"

    await asyncio.wait_for(old_task, timeout=2.0)
    assert server.browsers["ff-a"].ws is new, \
        "the displaced handler's cleanup must not tear down its successor"

    await disconnect(new, new_task)


@pytest.mark.asyncio
async def test_a_different_instance_id_coexists_and_kicks_nobody(server):
    a = FakeWS(peer=("127.0.0.1", 1111))
    a_task = await connect(server, a)
    await hello(server, a, instanceId="ff-a")

    b = FakeWS(peer=("127.0.0.1", 2222))
    b_task = await connect(server, b)
    await hello(server, b, instanceId="ff-b")

    assert [br.instance_id for br in server.registry.by_slot()] == ["ff-a", "ff-b"]
    assert [br.slot for br in server.registry.by_slot()] == [1, 2]
    assert a.close_code is None, "a second Firefox must NOT kick the first (no flip-flop)"
    assert b.close_code is None
    assert server.default_browser.instance_id == "ff-a"

    await disconnect(a, a_task)
    await disconnect(b, b_task)


@pytest.mark.asyncio
async def test_disconnect_removes_only_that_browser(server):
    a = FakeWS(peer=("127.0.0.1", 1111))
    a_task = await connect(server, a)
    await hello(server, a, instanceId="ff-a")
    b = FakeWS(peer=("127.0.0.1", 2222))
    b_task = await connect(server, b)
    await hello(server, b, instanceId="ff-b")

    await disconnect(a, a_task)

    assert list(server.browsers) == ["ff-b"]
    assert server.extension_connection is b

    await disconnect(b, b_task)
    assert server.browsers == {}
    assert server.extension_connection is None


# --- pending-request scoping ---------------------------------------------------------

@pytest.mark.asyncio
async def test_a_disconnect_fails_only_that_browsers_in_flight_requests(server):
    a = FakeWS(peer=("127.0.0.1", 1111))
    a_task = await connect(server, a)
    await hello(server, a, instanceId="ff-a")
    b = FakeWS(peer=("127.0.0.1", 2222))
    b_task = await connect(server, b)
    await hello(server, b, instanceId="ff-b")
    browser_a, browser_b = server.browsers["ff-a"], server.browsers["ff-b"]

    call_a = send(server, "req-a", browser_a, action="content.act")  # non-idempotent: no retry
    call_b = send(server, "req-b", browser_b)
    await wait_until(lambda: a.sent and b.sent, "both requests have reached their browser")
    assert a.sent[-1]["id"] == "req-a" and b.sent[-1]["id"] == "req-b"

    await disconnect(a, a_task)

    assert call_a.done()
    error = (await call_a)["error"]
    assert "disconnected" in error
    assert "[1]" in error, "the failure must name the browser that went away"
    assert not call_b.done(), "the other Firefox's call must be untouched"

    # ...and B still answers.
    assert server.rpc.pending_requests == {"req-b": server.rpc.pending_requests["req-b"]}
    assert server.rpc.pending_owners == {"req-b": "ff-b"}
    await server.rpc.handle_response(
        {"id": "req-b", "type": "response", "action": "tabs.list", "data": {"ok": True}})
    assert (await asyncio.wait_for(call_b, 2.0))["data"] == {"ok": True}
    assert server.rpc.pending_owners == {}

    await disconnect(b, b_task)


@pytest.mark.asyncio
async def test_displacement_fails_only_the_displaced_browsers_requests(server):
    a = FakeWS(peer=("127.0.0.1", 1111))
    a_task = await connect(server, a, identity=FF_A)
    await hello(server, a, instanceId="ff-a")
    b = FakeWS(peer=("127.0.0.1", 2222))
    b_task = await connect(server, b)
    await hello(server, b, instanceId="ff-b")

    call_a = send(server, "req-a", server.browsers["ff-a"], action="content.act")  # non-idempotent: no retry
    call_b = send(server, "req-b", server.browsers["ff-b"])
    await wait_until(lambda: a.sent and b.sent, "both requests are in flight")

    # A's Firefox restarts: same instance_id on a fresh socket.
    a2 = FakeWS(peer=("127.0.0.1", 3333))
    a2_task = await connect(server, a2, identity=FF_A)
    await hello(server, a2, instanceId="ff-a")

    assert "displaced" in (await call_a)["error"]
    assert not call_b.done()

    await asyncio.wait_for(a_task, timeout=2.0)
    await disconnect(a2, a2_task)
    await disconnect(b, b_task)


@pytest.mark.asyncio
async def test_requests_sent_before_the_hello_follow_the_browser(server):
    """A tool can route to a socket that hasn't said hello yet (it is the default
    browser the moment it connects). Promotion re-keys the record — its in-flight
    requests must be re-owned too, or that browser's disconnect would orphan them."""
    ws = FakeWS()
    task = await connect(server, ws)
    provisional = server.default_browser
    assert provisional.provisional is True

    call = send(server, "req-1", provisional)
    await wait_until(lambda: server.rpc.pending_owners,
                     "the request is registered against a browser")
    assert set(server.rpc.pending_owners.values()) == {provisional.instance_id}

    await hello(server, ws, instanceId="ff-a")
    assert server.rpc.pending_owners == {"req-1": "ff-a"}

    await disconnect(ws, task)
    assert "disconnected" in (await call)["error"]


# --- version skew: an extension with no instance_id ----------------------------------

@pytest.mark.asyncio
async def test_old_extension_without_instance_id_falls_back_to_the_browser_pid(server):
    """An XPI too old to mint an instance_id still gets a stable identity from the local
    pid probe — so its reconnect displaces its own record instead of piling up."""
    ws = FakeWS(peer=("127.0.0.1", 1111))
    task = await connect(server, ws, identity={"pid": 4242, "friendly": "Firefox"})
    await hello(server, ws, version="1.0.0")  # no instanceId at all

    assert list(server.browsers) == ["browser-pid:4242"]
    browser = server.browsers["browser-pid:4242"]
    assert browser.provisional is False
    assert browser.version == "1.0.0"

    # Same Firefox reconnects (extension reload): same pid -> same record, no second entry.
    ws2 = FakeWS(peer=("127.0.0.1", 2222))
    task2 = await connect(server, ws2, identity={"pid": 4242, "friendly": "Firefox"})
    await hello(server, ws2, version="1.0.0")

    assert list(server.browsers) == ["browser-pid:4242"]
    assert server.browsers["browser-pid:4242"].slot == 1
    assert server.extension_connection is ws2

    await asyncio.wait_for(task, timeout=2.0)
    await disconnect(ws2, task2)


@pytest.mark.asyncio
async def test_no_instance_id_and_no_pid_stays_provisional_but_usable(server):
    """Fully degraded remote extension: authenticated (a remote peer always must be), but
    with no identity at all — no instance_id, and no local process to probe. It must still
    register and route."""
    ws = FakeWS(peer=("10.0.0.9", 5555))
    task = await connect(server, ws)
    await remote_hello(server, ws, version="1.0.0")

    browser = server.default_browser
    assert browser is not None
    assert browser.provisional is True
    assert browser.version == "1.0.0"
    assert await server.rpc.send_to_extension({"id": "x", "type": "request", "action": "ping"})

    await disconnect(ws, task)


@pytest.mark.asyncio
async def test_hello_with_missing_or_junk_fields_degrades(server):
    """The extension's hello contract is all-optional: an older build sends fewer fields,
    and nothing here may crash the connection."""
    ws = FakeWS()
    task = await connect(server, ws)

    ws.feed({"type": "extension_hello"})                      # no data at all
    ws.feed({"type": "extension_hello", "data": "nonsense"})  # data of the wrong type
    ws.feed({"type": "extension_hello", "data": {}})          # empty payload
    await asyncio.sleep(0.02)

    browser = server.default_browser
    assert browser is not None and browser.provisional is True
    assert browser.version is None

    # A late hello with a non-string instance_id is coerced, not rejected.
    await hello(server, ws, instanceId=12345, version="1.4.0")
    assert list(server.browsers) == ["12345"]
    assert server.extension_version == "1.4.0"

    await disconnect(ws, task)


@pytest.mark.asyncio
async def test_a_remote_peer_is_not_probed_for_a_local_pid(server):
    ws = FakeWS(peer=("192.168.1.50", 5555))
    with patch.object(server, "_probe_browser_identity",
                      side_effect=AssertionError("must not probe a remote peer")):
        task = asyncio.create_task(server.handle_extension_connection(ws))
        await hello_processed(ws)
    await remote_hello(server, ws, instanceId="ff-remote")

    browser = server.browsers["ff-remote"]
    assert browser.is_remote is True
    assert browser.remote_addr == "192.168.1.50"
    assert browser.browser_pid is None
    assert browser.name == "remote-192.168.1.50"

    await disconnect(ws, task)


# --- default browser + compat surface ------------------------------------------------

@pytest.mark.asyncio
async def test_default_browser_is_the_first_connected_still_alive(server):
    a = FakeWS(peer=("127.0.0.1", 1111))
    a_task = await connect(server, a)
    await hello(server, a, instanceId="ff-a", version="1.4.0")
    b = FakeWS(peer=("127.0.0.1", 2222))
    b_task = await connect(server, b)
    await hello(server, b, instanceId="ff-b", version="1.4.1")

    assert server.default_browser.instance_id == "ff-a"

    # The first one leaves: the next-oldest survivor takes over as the default.
    await disconnect(a, a_task)
    assert server.default_browser.instance_id == "ff-b"
    assert server.extension_connection is b
    assert server.extension_version == "1.4.1"

    await disconnect(b, b_task)
    assert server.default_browser is None


@pytest.mark.asyncio
async def test_compat_properties_track_the_default_browser(server):
    a = FakeWS(peer=("127.0.0.1", 1111))
    a_task = await connect(server, a)
    await hello(server, a, instanceId="ff-a", version="1.4.0",
                fileAccess={"can_read_files": True},
                proxyAccess={"has_proxy_permission": True})
    b = FakeWS(peer=("127.0.0.1", 2222))
    b_task = await connect(server, b)
    await hello(server, b, instanceId="ff-b", version="9.9.9",
                fileAccess={"can_read_files": False},
                proxyAccess={"has_proxy_permission": False})

    # Every legacy consumer (popup, /health, mcp_tools, dozens of tests) reads these:
    # they must resolve to the DEFAULT browser, never to the newcomer.
    assert server.extension_connection is a
    assert server.extension_version == "1.4.0"
    assert server.extension_file_access == {"can_read_files": True}
    assert server.extension_proxy_access == {"has_proxy_permission": True}

    # Unrouted sends still go to the default browser.
    await server.rpc.send_to_extension({"id": "x", "type": "request", "action": "ping"})
    assert a.sent[-1]["id"] == "x"
    assert not b.sent

    await disconnect(a, a_task)
    await disconnect(b, b_task)


def test_compat_setters_drive_the_registry(server):
    """Legacy call sites assign the socket directly; that still binds/unbinds the slot."""
    ws = object()
    server.extension_connection = ws
    server.extension_version = "1.2.9"

    assert server.extension_connection is ws
    assert server.extension_version == "1.2.9"
    assert len(server.browsers) == 1

    server.extension_connection = None
    assert server.extension_connection is None
    assert server.browsers == {}


@pytest.mark.asyncio
async def test_shutdown_closes_every_browser_socket(server):
    a = FakeWS(peer=("127.0.0.1", 1111))
    a_task = await connect(server, a)
    await hello(server, a, instanceId="ff-a")
    b = FakeWS(peer=("127.0.0.1", 2222))
    b_task = await connect(server, b)
    await hello(server, b, instanceId="ff-b")

    call_a = send(server, "req-a", server.browsers["ff-a"])
    call_b = send(server, "req-b", server.browsers["ff-b"])
    await wait_until(lambda: a.sent and b.sent, "both requests are in flight")

    server.lifecycle.websocket_server = type("S", (), {"close": lambda self: None})()
    server.lifecycle.stop_websocket_server()
    # The registry is emptied synchronously, but each socket is closed by a task.
    await wait_until(lambda: a.close_code is not None and b.close_code is not None,
                     "the shutdown's close tasks have closed both sockets")

    assert server.browsers == {}
    assert "shutting down" in (await call_a)["error"]
    assert "shutting down" in (await call_b)["error"]
    assert a.close_code is not None and b.close_code is not None

    await asyncio.wait_for(a_task, timeout=2.0)
    await asyncio.wait_for(b_task, timeout=2.0)


@pytest.mark.asyncio
async def test_wait_for_extension_connection_means_any_browser(server):
    waiter = asyncio.create_task(server.wait_for_extension_connection(timeout=2.0))
    await asyncio.sleep(0.01)
    assert not waiter.done()

    ws = FakeWS()
    task = await connect(server, ws)
    assert await asyncio.wait_for(waiter, 1.0) is True
    # Already connected -> returns immediately.
    assert await server.wait_for_extension_connection(timeout=0.1) is True

    await disconnect(ws, task)


# --- /health --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_health_exposes_the_browsers_array_and_the_legacy_scalars(server):
    a = FakeWS(peer=("127.0.0.1", 1111))
    a_task = await connect(server, a, identity={"pid": 111, "friendly": "Firefox"})
    await hello(server, a, instanceId="ff-a", version="1.4.0",
                fileAccess={"can_read_files": True, "browser_major": 152},
                proxyAccess={"has_proxy_permission": True})
    b = FakeWS(peer=("10.0.0.9", 2222))
    b_task = await connect(server, b)
    await remote_hello(server, b, instanceId="ff-b", version="1.4.1")

    with patch.object(client_identity, "connected_browser_pids", return_value=[111]):
        health = server.get_health_info()

    assert [x["slot"] for x in health["browsers"]] == [1, 2]
    assert health["browsers_count"] == 2
    first, second = health["browsers"]
    assert first["instance_id"] == "ff-a"
    assert first["name"] == "Firefox pid 111"
    assert first["version"] == "1.4.0"
    assert first["browser_pid"] == 111
    assert first["is_default"] is True
    assert first["connected_at"] and first["last_activity"]
    assert second["name"] == "remote-10.0.0.9"
    assert second["remote_addr"] == "10.0.0.9"
    assert second["is_remote"] is True
    assert second["is_default"] is False

    # Legacy scalars stay, as aliases for the DEFAULT browser.
    assert health["extension_connected"] is True
    assert health["extension_version"] == "1.4.0"
    assert health["extension_file_access"]["can_read_opened_file_tab"] is True
    assert health["extension_proxy_access"]["has_proxy_permission"] is True
    assert health["browser_major"] == 152
    # Multi-instance is a supported state; the retired warning keys are not in /health.
    assert "multiple_extensions" not in health
    assert "extension_warning" not in health

    await disconnect(a, a_task)
    await disconnect(b, b_task)


@pytest.mark.asyncio
async def test_health_with_no_browser_is_unchanged(server):
    with patch.object(client_identity, "connected_browser_pids", return_value=[]):
        health = server.get_health_info()

    assert health["browsers"] == []
    assert health["extension_connected"] is False
    assert health["extension_version"] == "unknown"
    assert health["extension_file_access"] is None
    assert health["extension_proxy_access"] is None


def test_browser_name_uses_edition_then_pid(server):
    b = ExtBrowser(ws=object(), instance_id="ff-a", slot=1)
    assert b.name == "Firefox"
    b.browser_pid = 4242
    b.friendly = "Firefox Developer Edition"
    assert b.name == "Firefox Developer Edition pid 4242"
    assert b.describe() == "[1] Firefox Developer Edition pid 4242"


# --- Firefox FORKS (Zen, LibreWolf, …) ---------------------------------------
# A fork runs the extension unchanged and — this is the trap — still answers
# runtime.getBrowserInfo() with name "Firefox" (Zen does). The local PROCESS is the
# only thing that knows which browser this really is.

def test_a_fork_is_named_from_its_process_not_from_what_it_claims():
    assert gecko_brand("zen.exe", r"C:\Program Files\Zen Browser\zen.exe") == "Zen Browser"
    assert gecko_brand("librewolf", "/usr/lib/librewolf/librewolf") == "LibreWolf"
    assert gecko_brand("firefox.exe", r"C:\Program Files\Mozilla Firefox\firefox.exe") == "Firefox"
    # The edition lives in the install path, and only Firefox ships several.
    assert gecko_variant("Firefox", r"C:\Program Files\Firefox Developer Edition\firefox.exe") \
        == "Firefox Developer Edition"
    assert gecko_variant("Zen Browser", r"C:\Program Files\Zen Browser\zen.exe") == "Zen Browser"


def test_a_process_that_is_not_a_browser_is_not_one(tmp_path):
    """The guard that keeps two sockets from one fake-extension harness (a python
    process) from merging into a single browser: a pid is only a browser identity
    when the process really is a browser."""
    py = tmp_path / "python.exe"
    py.write_text("")
    assert gecko_brand("python.exe", str(py)) is None
    assert gecko_brand("node.exe", str(tmp_path / "node.exe")) is None


def test_an_unknown_fork_is_still_recognized_by_its_gecko_runtime(tmp_path):
    """A fork nobody has heard of yet: the Gecko library next to the exe is the tell,
    so it gets a pid and a name instead of dropping off the roster entirely."""
    exe = tmp_path / "moonwolf.exe"
    exe.write_text("")
    assert gecko_brand("moonwolf.exe", str(exe)) is None    # nothing says it's a browser
    (tmp_path / "xul.dll").write_text("")                   # …now it does
    _gecko_dir_cache.clear()
    assert gecko_brand("moonwolf.exe", str(exe)) == "Moonwolf"


def test_a_fork_reads_cleanly_in_the_roster(server):
    b = ExtBrowser(ws=object(), instance_id="zen-1", slot=2)
    b.brand = b.friendly = "Zen Browser"
    b.browser_pid = 38288
    b.browser_info = {"name": "Firefox", "version": "152.0.5"}   # what Zen claims
    assert b.family == "Zen Browser"                             # what it actually is
    assert b.name == "Zen Browser pid 38288"
    assert b.describe() == "[2] Zen Browser pid 38288"           # no "Zen Browser [2] Zen Browser"


# --- The one self-report a fork makes: its own key in getBrowserInfo() --------
# It lies on `name` AND `vendor` — but adds a key of its own, and that key reaches us
# from a REMOTE browser, which no process probe ever can. (chrome://branding, which
# would say "Zen Browser" outright, is forbidden to extensions — verified.)

# Verbatim from the user's Zen 152 and Firefox Dev Edition 153.
ZEN_BROWSER_INFO = {"name": "Firefox", "vendor": "Mozilla", "version": "152.0.5",
                    "buildID": "20260708113753", "zen": {"version": "1.21.6b"}}
FIREFOX_BROWSER_INFO = {"name": "Firefox", "vendor": "Mozilla", "version": "153.0",
                        "buildID": "20260710112311"}


def test_a_fork_is_outed_by_its_own_key_and_nothing_else():
    assert fork_from_browser_info(ZEN_BROWSER_INFO) == ("Zen Browser", "1.21.6b")
    assert fork_from_browser_info(FIREFOX_BROWSER_INFO) == (None, None)
    assert fork_from_browser_info(None) == (None, None)
    # A fork nobody has heard of yet is still a fork, named after its key.
    assert fork_from_browser_info({"name": "Firefox", "moonwolf": {"version": "3.1"}}) \
        == ("Moonwolf", "3.1")
    # But a future Firefox growing an ordinary field is NOT a new browser.
    assert fork_from_browser_info({"name": "Firefox", "policiesEnabled": True}) == (None, None)


def test_the_fork_key_identifies_a_REMOTE_browser_no_process_probe_can_reach():
    """The whole point: another machine, nothing to probe — and it still says Zen."""
    b = ExtBrowser(ws=object(), instance_id="r-zen", slot=1,
                   remote_addr="192.168.1.50", is_remote=True)
    b.browser_info = ZEN_BROWSER_INFO

    assert b.family == "Zen Browser"          # …while it claims to be Firefox
    assert b.browser_info["name"] == "Firefox"
    assert b.fork_version == "1.21.6b"        # its own version, which `version` is not
    assert b.describe() == "Zen Browser [1] remote-192.168.1.50"


def test_a_remote_firefox_stays_firefox():
    """No process, no fork key: the claim is all there is, and it is true."""
    b = ExtBrowser(ws=object(), instance_id="r-1", slot=1,
                   remote_addr="192.168.1.50", is_remote=True)
    b.browser_info = FIREFOX_BROWSER_INFO
    assert b.family == "Firefox"
    assert b.fork_version is None
    assert b.describe() == "Firefox [1] remote-192.168.1.50"


def test_the_process_still_names_a_local_browser():
    """Locally the probe is ground truth and stays in charge — it knows the EDITION,
    which no self-report carries (Zen and Firefox both just say "Firefox")."""
    b = ExtBrowser(ws=object(), instance_id="dev-1", slot=1)
    b.brand, b.friendly = "Firefox", "Firefox Developer Edition"
    b.browser_info = FIREFOX_BROWSER_INFO
    assert b.edition == "Firefox Developer Edition"   # not flattened to bare "Firefox"
    assert b.family == "Firefox"


# --- a hello may not take a live browser's record without proving it is that browser ------

@pytest.mark.asyncio
async def test_a_second_local_process_cannot_take_over_a_live_instance_id(server):
    """/health publishes every instance_id, so a copied one must not move the record onto
    the copier's socket: agents' typed text and eval code would follow it."""
    victim = FakeWS(peer=("127.0.0.1", 1111))
    victim_task = await connect(server, victim, identity={"pid": 4101, "friendly": "Firefox"})
    await hello(server, victim, instanceId="ff-a", version="1.4.0")
    record = server.browsers["ff-a"]

    thief = FakeWS(peer=("127.0.0.1", 2222))
    thief_task = await connect(server, thief, identity={"pid": 9999, "friendly": "Firefox"})
    await hello(server, thief, instanceId="ff-a", version="6.6.6")

    assert server.browsers["ff-a"] is record
    assert record.ws is victim, "the record moved onto the copier's socket"
    assert record.version == "1.4.0", "the refused hello's fields landed on the victim"
    assert victim.close_code is None, "the live browser's socket was closed"
    assert thief.close_code is not None, "the refused socket must be closed"

    await asyncio.wait_for(thief_task, timeout=2.0)
    assert server.browsers["ff-a"].ws is victim
    await disconnect(victim, victim_task)


@pytest.mark.asyncio
async def test_a_remote_peer_cannot_take_over_a_local_browsers_instance_id(server):
    victim = FakeWS(peer=("127.0.0.1", 1111))
    victim_task = await connect(server, victim, identity={"pid": 4101, "friendly": "Firefox"})
    await hello(server, victim, instanceId="ff-a")
    record = server.browsers["ff-a"]

    phone = FakeWS(peer=("10.0.0.9", 2222))
    phone_task = await connect(server, phone)
    await remote_hello(server, phone, instanceId="ff-a")

    assert server.browsers["ff-a"] is record
    assert record.ws is victim
    assert record.is_remote is False
    await asyncio.wait_for(phone_task, timeout=2.0)
    await disconnect(victim, victim_task)


@pytest.mark.asyncio
async def test_a_remote_device_cannot_take_over_another_devices_live_record(server):
    from secure_ext import secure_fake_hello
    from browserplug_shared import device_tokens

    owner = FakeWS(peer=("10.0.0.9", 1111))
    owner_task = await connect(server, owner)
    await remote_hello(server, owner, instanceId="ff-r")
    record = server.browsers["ff-r"]

    other = FakeWS(peer=("10.0.0.9", 2222))
    other_task = await connect(server, other)
    await secure_fake_hello(other, server, {"type": "extension_hello",
                                            "data": {"instance_id": "ff-r"}},
                            token=device_tokens.issue(server.ws_token, "another-device"))
    await hello_processed(other)

    assert record.ws is not None and record.ws.close_code is None
    assert server.browsers["ff-r"] is record
    assert record.device_label != "another-device"
    await asyncio.wait_for(other_task, timeout=2.0)
    await disconnect(owner, owner_task)


@pytest.mark.asyncio
async def test_the_same_device_may_replace_its_own_live_remote_connection(server):
    from secure_ext import secure_fake_hello
    from browserplug_shared import device_tokens
    token = device_tokens.issue(server.ws_token, "phone-1")

    old = FakeWS(peer=("10.0.0.9", 1111))
    old_task = await connect(server, old)
    await secure_fake_hello(old, server, {"type": "extension_hello",
                                          "data": {"instance_id": "ff-r"}}, token=token)
    await hello_processed(old)
    record = server.browsers["ff-r"]

    new = FakeWS(peer=("10.0.0.9", 2222))
    new_task = await connect(server, new)
    await secure_fake_hello(new, server, {"type": "extension_hello",
                                          "data": {"instance_id": "ff-r"}}, token=token)
    await hello_processed(new)

    assert server.browsers["ff-r"] is record
    assert record.ws is not old
    assert record.connect_count == 2
    await asyncio.wait_for(old_task, timeout=2.0)
    await disconnect(new, new_task)
