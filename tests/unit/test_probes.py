"""GET /ready — the yes/no probe on the MCP HTTP port.

The point of it is the STATUS CODE: a supervisor reads it without parsing anything, so
these tests assert codes first and bodies second. What they exist to stop is the failure
/health has by design — answering 200 for a server that cannot do anything an agent asks,
because no browser is attached.

The "service" key is asserted everywhere on purpose: the MCP port is a searched range
(2830..2942) and a stranger's HTTP server squatting there can return 200 too. Identity is
half the verdict; mcp_port.browserplug_health keys on the same field.
"""

import json
import logging
import os

import pytest

from server.reporting import probes
from server.reporting.probes import READY_PATH, ready_payload
from server.browsers.browser_identity import ExtBrowser
from server.server import BrowserPlugServer


class FakeSocket:
    """A live extension socket: `connected` is `close_code is None` (see ExtBrowser)."""
    close_code = None


class ClosedSocket:
    close_code = 1001


def make_server(**kwargs):
    return BrowserPlugServer(host="localhost", port=8772, start_mcp=True, **kwargs)


def attach(server, instance_id="ff-a", slot=1, ws=None, pid=None):
    b = ExtBrowser(ws=FakeSocket() if ws is None else ws, instance_id=instance_id, slot=slot)
    b.provisional = False
    b.browser_pid = pid
    server.browsers[instance_id] = b
    return b


async def http_get(app, path, client=("127.0.0.1", 5555)):
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    await app({"type": "http", "method": "GET", "path": path,
               "headers": [], "client": client}, receive, send)
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    headers = dict(next(m["headers"] for m in sent if m["type"] == "http.response.start"))
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, headers, json.loads(body)


def probe_app(server):
    """The real ASGI wrapper, over an inner app that would EXPLODE if reached — /ready
    must never fall through to the MCP application."""

    async def unreachable(*_a):
        raise AssertionError("a probe path was passed through to the MCP app")

    return server._wrap_client_tracking(unreachable)


@pytest.mark.asyncio
async def test_ready_is_503_with_no_browser_attached():
    """The state /health calls 200 and an agent experiences as "No extension connection
    available" on every single call."""
    server = make_server()
    status, headers, body = await http_get(probe_app(server), READY_PATH)

    assert status == 503
    assert body["ready"] is False and body["browsers"] == 0
    assert body["service"] == "browserplug"
    assert "reason" in body, "a 503 must say what is missing"
    assert headers[b"cache-control"] == b"no-store", "a cached 503 outlives its condition"


@pytest.mark.asyncio
async def test_ready_is_200_once_a_browser_is_attached():
    server = make_server()
    attach(server, pid=4321)

    status, _, body = await http_get(probe_app(server), READY_PATH)

    assert status == 200
    assert body["ready"] is True and body["browsers"] == 1
    assert body["browser_names"] == [server.browsers["ff-a"].name], "name the browser"
    assert "reason" not in body


@pytest.mark.asyncio
async def test_ready_counts_every_attached_browser():
    """Firefox and Chromium coexist; a supervisor that wants both up reads the count."""
    server = make_server()
    attach(server, "ff-a", slot=1)
    attach(server, "cr-b", slot=2)

    status, _, body = await http_get(probe_app(server), READY_PATH)

    assert status == 200 and body["browsers"] == 2
    assert len(body["browser_names"]) == 2


@pytest.mark.asyncio
async def test_ready_follows_the_router_not_the_registry_size():
    """A departed browser lingers in the registry until its handler's finally runs, so
    len(server.browsers) is not the answer; live_browsers() is the same call routing uses."""
    server = make_server()
    attach(server, "ff-a", ws=ClosedSocket())

    status, _, body = await http_get(probe_app(server), READY_PATH)

    assert body["browsers"] == len(server.live_browsers())
    assert body["ready"] is bool(server.live_browsers())
    assert status == (200 if server.live_browsers() else 503)


def test_ready_payload_never_raises_when_the_registry_is_gone():
    """A probe that arrives mid-teardown answers 503, not 500: 'no browser to route to'
    is the correct verdict, and a 500 would send a supervisor hunting a fault."""

    class Torn:
        def live_browsers(self):
            raise RuntimeError("registry emptied while you were asking")

    status, body = ready_payload(Torn())
    assert status == 503 and body["ready"] is False


@pytest.mark.asyncio
async def test_a_trailing_slash_is_the_same_endpoint():
    """Every other endpoint here accepts both spellings; a supervisor's URL should not be
    the thing that fails."""
    server = make_server()
    for path in ("/ready", "/ready/"):
        status, _, body = await http_get(probe_app(server), path)
        assert status == 503 and body["service"] == "browserplug"


@pytest.mark.asyncio
async def test_a_post_is_not_a_probe():
    """GET-only, like /health. A POST falls through to the MCP app, which here explodes —
    proving /ready claims only what it should."""
    server = make_server()
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    with pytest.raises(AssertionError, match="passed through to the MCP app"):
        await probe_app(server)({"type": "http", "method": "POST", "path": READY_PATH,
                                 "headers": [], "client": ("127.0.0.1", 5555)},
                                receive, sent.append)


@pytest.mark.asyncio
async def test_health_still_answers_200_with_no_browser():
    """The contrast /ready exists for, pinned so nobody 'fixes' /health into a verdict:
    it reports, it does not judge, and every existing client depends on that."""
    server = make_server()
    status, _, body = await http_get(probe_app(server), "/health")
    assert status == 200 and body["service"] == "browserplug"


def _raise(exc):
    def broken():
        raise exc
    return broken


@pytest.mark.asyncio
async def test_health_answers_degraded_not_500_when_its_report_raises():
    """/health is how every launcher recognises this server. A 500 there reads as "no
    BrowserPlug on this port", and the launcher spawns a duplicate that dies on the busy
    WebSocket port, so a report that raises still answers 200 with who we are."""
    server = make_server()
    server.get_health_info = _raise(RuntimeError("psutil went away"))
    status, _, body = await http_get(probe_app(server), "/health")
    assert status == 200
    assert body["service"] == "browserplug" and body["status"] == "degraded"
    assert body["health_error"] == "RuntimeError: psutil went away"
    assert body["server_pid"] == os.getpid()
    # The launchers' refresh key survives, so a degraded answer is not a tool-list change.
    assert body["tools_signature"] == server.mcp_tools.tools_signature
    assert body["mcp_port"] == server.mcp_port and body["ws_port"] == server.port


@pytest.mark.asyncio
async def test_health_answers_degraded_when_its_report_is_not_json():
    server = make_server()
    server.get_health_info = lambda: {"unserialisable": object()}
    status, _, body = await http_get(probe_app(server), "/health")
    assert status == 200 and body["status"] == "degraded"
    assert body["health_error"].startswith("TypeError")


@pytest.mark.asyncio
async def test_health_is_never_cached_like_the_other_probes():
    server = make_server()
    _, headers, _ = await http_get(probe_app(server), "/health")
    assert headers[b"cache-control"] == b"no-store"
    server.get_health_info = _raise(RuntimeError("boom"))
    _, headers, _ = await http_get(probe_app(server), "/health")
    assert headers[b"cache-control"] == b"no-store"


def test_a_report_that_keeps_failing_is_logged_once_not_on_every_poll(caplog, monkeypatch):
    monkeypatch.setattr(probes, "_last_health_error", None)
    server = make_server()
    server.get_health_info = _raise(RuntimeError("boom"))

    def warnings_logged():
        return [r for r in caplog.records if "could not build its report" in r.getMessage()]

    with caplog.at_level(logging.WARNING, logger=probes.logger.name):
        for _ in range(3):
            probes.health_body(server)
        assert len(warnings_logged()) == 1
        server.get_health_info = lambda: {"status": "ok"}   # recovers ...
        probes.health_body(server)
        server.get_health_info = _raise(RuntimeError("boom"))   # ... and fails again
        probes.health_body(server)
    assert len(warnings_logged()) == 2


@pytest.mark.asyncio
async def test_ready_never_leaks_the_connection_token():
    """Unauthenticated and CORS-less. Whatever else it grows, not this."""
    server = make_server()
    attach(server)
    _, _, body = await http_get(probe_app(server), READY_PATH)
    assert server.ws_token not in json.dumps(body)
