"""GET /ready and GET /health: the probes on the MCP HTTP port.

`/health` answers 200 for anything a live server can say about itself, warnings
included, and it answers 200 when it cannot say it: every launcher recognises this server
by its /health (`service`), and launchers and the options page notice a restart through it
(`server_pid`, `tools_signature`). A 500 would read as "no BrowserPlug here", and each
launcher would spawn a duplicate that dies on the busy WebSocket port. So when the full
report or its JSON raises, the reply is the identity fields plus `health_error`, marked
`status: "degraded"`, which mcp_port.browserplug_health still accepts as ours.

Either way /health is a *report*, not a verdict, which makes it useless to a supervisor
that wants a yes/no: a server with no browser attached can do nothing an agent asks for,
and /health still says 200. So:

  GET /ready   200 while at least one browser is attached and routable, 503 when none is.
               Answers "would a tool call reach a browser right now".

Readiness is deliberately NOT "the WebSocket port is bound". It is `server.live_browsers()`
— exactly the list a tool call routes into — so /ready cannot say yes to a state where
every call would come back "No extension connection available".

It never returns 500, and it returns nothing at all when the process is down: an unbound
port refuses the connection (or, behind Windows Firewall's default drop, times out). A
supervisor must read "no answer" as down; there is no status code for a server that is not
there to send one. Identity is half the verdict, which is why the payload carries
`"service": "browserplug"`: the MCP port is a searched range (2830..2942) and a stranger's
HTTP server squatting there can return 200 too (mcp_port.browserplug_health keys on the
same field).

**There is no /startup, and the measurement is why.** A startup probe would have to
describe the window between "this port answers" and "initialization finished", and on this
server that window is 0-62 ms (five spawns, Windows, polling as fast as urllib allows; one
run measured exactly zero). The reason is the boot ORDER: start_server brings uvicorn up
first and only then binds the WebSocket, but uvicorn does not serve responses until its own
startup completes, which lands at or after the WebSocket bind. Everything genuinely slow is
BEFORE that — ~5.6 s of interpreter and package imports during which the port is not bound
and a probe gets connection-refused, which already tells a supervisor "not started". A
probe cannot be made to answer inside that window without binding a second, minimal listener
on the MCP port ahead of uvicorn and handing the port over, and paying that for 62 ms is not
a trade worth making. If the boot order is ever changed so HTTP comes up first in earnest,
re-measure before concluding it is still true.
"""

import json
import logging
import os

from browserplug_shared.mcp_port import HEALTH_SERVICE, HEALTH_STATUS_DEGRADED, HEALTH_STATUS_OK
from browserplug_shared.version import __version__ as BROWSERPLUG_VERSION

from server.reporting.reports import SnapshotCache

logger = logging.getLogger(__name__)

READY_PATH = "/ready"

# 503 rather than 500 for the negative: the request was understood and the server is
# healthy enough to answer it, it just cannot say yes yet. 500 would claim a fault.
STATUS_READY = 200
STATUS_NOT_READY = 503


def ready_payload(server):
    """(status, body-dict) for GET /ready — is at least one browser attached?

    `live_browsers()` is the same call the router uses to pick a target, so a 200 here
    means a tool call has somewhere to go. Names and count are included because "ready"
    is not actionable on its own when the one attached browser is the wrong browser.
    """
    try:
        live = list(server.live_browsers())
    except Exception:
        # A server torn down mid-probe answers 503, never 500: the verdict ("no browser
        # you can route to") is correct either way, and a 500 would send a supervisor
        # hunting a fault that isn't there.
        live = []
    body = {
        "service": HEALTH_SERVICE,
        "ready": bool(live),
        "browsers": len(live),
        "browser_names": [b.name for b in live],
    }
    if not live:
        body["reason"] = ("no browser extension is connected - the user must open the "
                          "browser, or click Reconnect in the BrowserPlug popup")
    return (STATUS_READY if live else STATUS_NOT_READY), body


# The last /health failure logged. Launchers poll every few seconds, so a report that
# keeps failing the same way is logged once, not on every poll.
_last_health_error = None


def health_body(server):
    """The JSON bytes for GET /health. Never raises: a full report when one can be built,
    otherwise `degraded_health()` naming what failed."""
    global _last_health_error
    try:
        body = json.dumps(server.get_health_info()).encode("utf-8")
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        if error != _last_health_error:
            logger.warning("/health could not build its report, answering degraded: %s",
                           error, exc_info=True)
            _last_health_error = error
        return json.dumps(degraded_health(server, error)).encode("utf-8")
    _last_health_error = None
    return body


def degraded_health(server, error):
    """What /health can still say when the full report raised: identity, plus the fields
    launchers and the options page key on. Each is read on its own, so one broken
    attribute costs that field rather than the reply."""
    body = {"status": HEALTH_STATUS_DEGRADED, "health_error": error}
    body.update(_identity_fields(server))
    return body


def identity_health(server):
    """The identity-only /health: what a launcher's poll reads and nothing else.

    Launchers, the relay and the tool-surface watch key on `service`, `server_pid`, the
    version, the ports and `tools_signature`. Reading those costs no psutil scan, so the
    poll every launcher makes every few seconds stays off the expensive report. The keys
    and values are the ones the full reply carries; only the roster is left out."""
    body = {"status": HEALTH_STATUS_OK}
    body.update(_identity_fields(server))
    return body


def _identity_fields(server):
    body = {"service": HEALTH_SERVICE, "server_pid": os.getpid(),
            "server_version": BROWSERPLUG_VERSION}
    optional = (
        ("started", lambda: server.server_start_time.strftime("%Y-%m-%d %H:%M:%S")),
        ("ws_port", lambda: server.port),
        ("mcp_port", lambda: server.mcp_port),
        # Present even when None, as in the full report: the launchers' refresh key is
        # (server_pid, tools_signature), and a key that vanished would read as a change.
        ("tools_signature",
         lambda: getattr(getattr(server, "mcp_tools", None), "tools_signature", None)),
    )
    for key, read in optional:
        try:
            body[key] = read()
        except Exception:
            pass
    return body


def _snapshots(server):
    """The server's health cache, created on first use so a bare server object works."""
    cache = getattr(server, "_health_snapshots", None)
    if not isinstance(cache, SnapshotCache):
        cache = SnapshotCache()
        try:
            server._health_snapshots = cache
        except Exception:
            pass
    return cache


async def handle_health_request(send, server, identity=False):
    """Serve GET /health: always 200, and never cached by the client (`no-store`), since a
    cached report outlives the server it described.

    The full report is built on the report pool, never on this loop, and the server keeps
    it for SNAPSHOT_TTL_SECONDS so a burst of pollers costs one build (reports.py).
    `identity=True` answers with the identity fields only and touches nothing slow."""
    if identity:
        body = json.dumps(identity_health(server)).encode("utf-8")
    else:
        body = await _snapshots(server).get("health", lambda: health_body(server))
    await send({"type": "http.response.start", "status": 200,
                "headers": [(b"content-type", b"application/json"),
                            (b"cache-control", b"no-store")]})
    await send({"type": "http.response.body", "body": body})


async def handle_probe_request(path, scope, send, server):
    """Serve /ready. Returns True if it handled `path`, else False.

    GET-only, unauthenticated and CORS-less, exactly like /health: it runs on the
    localhost-bound MCP port and says nothing a bystander could not learn by trying to use
    the server. Never add a token-carrying field to it - a custom header is what keeps the
    authenticated endpoints away from web pages, and this one has none.
    """
    if path != READY_PATH:
        return False

    status, body = ready_payload(server)
    payload = json.dumps(body).encode("utf-8")
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"),
                            # A cached 503 outlives the condition it described.
                            (b"cache-control", b"no-store")]})
    await send({"type": "http.response.body", "body": payload})
    return True
