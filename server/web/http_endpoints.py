"""The HTTP endpoints on the MCP port: a route table with one policy column.

server.py wraps the MCP SDK's ASGI app in `HttpEndpoints`; the request-tracking logic
(formerly the `_wrap_client_tracking` closure) lives here. Order of business for one request:

  1. the Host-check hook (host_guard.HostGuard in production: Host allowlist for every
     path, Origin check on /mcp; DNS-rebinding defence, review finding S1)
  2. `http_auth.authorize`, the peer gate: local peers pass, remote peers need a signature
  3. the route table below: the first matching (method, path) row runs
  4. no row matched: the MCP protocol-era gate, then MCP session tracking, then the MCP app
     (with the request's headers bound in request_context for the tool layer). A
     2026-07-28 request names no session, so it is tracked under a connection key
     (modern_identity.py) that stands in for the session id.

Every row carries one policy. The column records what is enforced today, including the
weak spots:

  LOCAL     only the peer gate: a local peer is free, a remote peer needs a signed request.
  CONTROL   a signature demanded even from a local peer, plus a refusal of anything a web
            page sent (Sec-Fetch-Site: cross-site, or an Origin that is not an extension's).
            /shutdown, /restart, /timing and /diagnose-wan: a plain form post can reach
            loopback, but it cannot carry the custom header (review finding S2).
  TOKEN     a valid signature is demanded even from a local peer. The dispatcher drains the
            request body first, then refuses, so the caller reads the 403 cleanly.
            /ws-bind and /reset-config.

The cross-site refusal is a column of its own, `Route.refuse_cross_site`. Every CONTROL and
TOKEN row sets it, and a unit test holds the table to that.
  LOOPBACK  a local peer only. The dispatcher answers 403 "localhost only" to anyone else.

The MCP protocol path itself (the fall-through) is loopback-only through the peer gate.

Dependency direction: this module never imports server.py. What it needs from there
(`is_localhost_bind`) is passed in. Import has no side effects.
"""

import asyncio
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from types import MappingProxyType
from typing import Callable, Optional, Tuple

from browserplug_shared import request_signing
from browserplug_shared.config_store import WS_MODE_VALUES
from browserplug_shared.version import (CLIENT_PID_HEADER, HEALTH_IDENTITY_HEADER, HEALTH_IDENTITY_VALUE,
                                        LAUNCHER_PID_HEADER, LAUNCHER_VERSION_HEADER)

from server.browsers import routing
from server.runtime import graceful_restart
from server.ws import http_auth, ws_host_policy
from server.web import peer_owner
from server import preflight
from server.reporting import probes, wan_diagnosis
from server.rpc import protocol_era
from server.web import modern_identity, request_context

logger = logging.getLogger(__name__)

LOCAL = "local"
TOKEN = "token"
CONTROL = "control"
LOOPBACK = "loopback-only"

# The policy of the fall-through to the MCP app, enforced by http_auth's gate.
MCP_POLICY = LOOPBACK


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

# Body caps (review finding S8). Any web page can POST to loopback, and an uncapped drain
# holds the one event loop every agent shares. The control endpoints take a few hundred
# bytes of JSON. The /mcp cap sits far above the biggest legitimate tools/call (a page_eval
# script or a page_act text fill is kilobytes to a few MiB) and only bounds what the
# recorder buffers in memory.
MAX_CONTROL_BODY = 64 * 1024
MAX_MCP_BODY = 16 * 1024 * 1024

# Who is behind an MCP connection is found by walking the TCP table and the process tree,
# which takes milliseconds and, before the masking regex was made linear, took minutes. It
# never runs on the loop. These workers are the only place it runs; a flood of new
# connections queues here, and past MAX_IDENTITY_PENDING lookups in flight further ones are
# dropped (the row stays unnamed and a later request tries again).
IDENTITY_WORKERS = 2
MAX_IDENTITY_PENDING = 64
# A tools/call waits at most this long for its session's lookup to finish, so the call is
# filed under the client's real process (routing key, launcher pid) when the lookup is quick,
# and goes ahead regardless when it is not.
IDENTITY_WAIT_SECONDS = 1.0

_identity_pool = None
_identity_pool_lock = threading.Lock()


def identity_pool():
    """The shared worker pool for client identity lookups, created on first use."""
    global _identity_pool
    with _identity_pool_lock:
        if _identity_pool is None:
            _identity_pool = ThreadPoolExecutor(max_workers=IDENTITY_WORKERS,
                                                thread_name_prefix="identity")
        return _identity_pool


class BodyTooLarge(Exception):
    """A request body passed its cap."""


def declared_length(scope) -> Optional[int]:
    """The Content-Length header as an int, or None when absent or malformed."""
    for k, v in scope.get("headers") or ():
        if k.lower() == b"content-length":
            try:
                return int(v)
            except ValueError:
                return None
    return None


async def read_body(receive, limit: int = MAX_CONTROL_BODY) -> bytes:
    """Drain the request body, raising BodyTooLarge as soon as it passes `limit`."""
    chunks, size = [], 0
    while True:
        msg = await receive()
        chunk = msg.get("body", b"") or b""
        size += len(chunk)
        if size > limit:
            raise BodyTooLarge(limit)
        chunks.append(chunk)
        if msg.get("type") != "http.request" or not msg.get("more_body", False):
            break
    return b"".join(chunks)


async def json_reply(send, payload, status=200, no_store=True) -> None:
    """One JSON response. `no_store` adds the Cache-Control header the sensitive replies use."""
    headers = [(b"content-type", b"application/json")]
    if no_store:
        headers.append((b"cache-control", b"no-store"))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": json.dumps(payload).encode("utf-8")})


async def require_token(scope, send, message) -> bool:
    """True when the request carried a valid signature; otherwise answer 403 and return False.

    The signature was verified once, at the top of the request (a valid one spends its
    nonce), and stored in the scope.
    """
    if scope.get("browserplug.signed"):
        return True
    await http_auth.refuse(send, 403, message)
    return False


# Origin schemes of the extension's own pages. Their fetches carry an Origin and, in some
# engines, Sec-Fetch-Site: cross-site, and they are the callers that sign.
EXTENSION_ORIGIN_SCHEMES = ("moz-extension://", "chrome-extension://",
                            "safari-web-extension://")


def cross_site_refusal(scope) -> Optional[str]:
    """Why a request looks like a web page's, or None.

    A page can be made to send a request to loopback, but the browser stamps it: `Origin`
    on any POST and `Sec-Fetch-Site: cross-site` on a cross-site one. The stdio launcher,
    the control script and curl send neither. An extension page sends Origin too, so its
    own scheme is let through; the signature still has to be valid.
    """
    headers = {}
    for k, v in scope.get("headers") or ():
        headers.setdefault(k.decode("latin-1").lower(), v.decode("latin-1"))
    origin = headers.get("origin")
    if origin is not None:
        if origin.strip().lower().startswith(EXTENSION_ORIGIN_SCHEMES):
            return None
        return "requests from a web page are refused"
    if headers.get("sec-fetch-site", "").strip().lower() == "cross-site":
        return "cross-site requests are refused"
    return None


def permissive_host_check(scope) -> Optional[Tuple[int, str]]:
    """The Host-check hook. Return (status, message) to refuse a request, None to admit it.

    The default for tests that build an endpoint without a guard; server.py always injects
    host_guard.HostGuard.check.
    """
    return None


class Request:
    """One HTTP request as the handlers see it."""

    __slots__ = ("scope", "receive", "send", "server", "body", "_body_read", "max_body")

    def __init__(self, scope, receive, send, server, max_body=MAX_CONTROL_BODY):
        self.scope = scope
        self.receive = receive
        self.send = send
        self.server = server
        self.body = b""
        self._body_read = False
        self.max_body = max_body

    @property
    def peer_host(self):
        return (self.scope.get("client") or ("", 0))[0]

    async def read_body(self) -> bytes:
        # Once only: the dispatcher reads the body after the token check, and a second
        # receive() on a finished request waits for a disconnect that never comes.
        if not self._body_read:
            declared = declared_length(self.scope)
            if declared is not None and declared > self.max_body:
                raise BodyTooLarge(self.max_body)
            self.body = await read_body(self.receive, self.max_body)
            self._body_read = True
        return self.body

    async def json_reply(self, payload, status=200, no_store=True) -> None:
        await json_reply(self.send, payload, status, no_store)


# ---------------------------------------------------------------------------
# Handlers. Each takes (endpoints, request); `endpoints.server` is the BrowserPlugServer.
# ---------------------------------------------------------------------------

async def _health(ep, req):
    # A launcher stamps its pid + version on this poll (every few seconds, for its whole
    # life): the only channel that reports a launcher's build BEFORE it has an MCP session,
    # and one that does not depend on how its MCP transport is built.
    try:
        hdrs = dict(req.scope.get("headers") or [])
        lv = hdrs.get(LAUNCHER_VERSION_HEADER.lower().encode("latin-1"))
        lp = hdrs.get(LAUNCHER_PID_HEADER.lower().encode("latin-1"))
        if lv and lp:
            req.server.record_launcher_version(int(lp.decode("latin-1")),
                                               lv.decode("latin-1"))
    except Exception as e:
        logger.debug(f"launcher version header ignored: {e}")
    # A launcher asks for the identity fields only (header, or ?identity=1 by hand); the
    # popup and the options page send neither and get the full report, unchanged.
    identity = False
    try:
        wanted = dict(req.scope.get("headers") or []).get(
            HEALTH_IDENTITY_HEADER.lower().encode("latin-1"), b"")
        query = (req.scope.get("query_string") or b"").decode("latin-1")
        identity = (wanted.decode("latin-1").strip().lower() == HEALTH_IDENTITY_VALUE
                    or "identity=1" in query.split("&"))
    except Exception:
        pass
    # Always 200, degraded rather than failed; the reply lives in probes.py.
    if identity:
        await probes.handle_health_request(req.send, req.server, identity=True)
    else:
        await probes.handle_health_request(req.send, req.server)


async def _ready(ep, req):
    await probes.handle_probe_request(probes.READY_PATH, req.scope, req.send, req.server)


async def _diagnose_wan(ep, req):
    await wan_diagnosis.handle_diagnose_wan_request(req.scope, req.send, req.server)


async def _doctor(ep, req):
    await preflight.handle_doctor_request(req.scope, req.send, req.server)


async def _ws_token(ep, req):
    # The token bootstrap channel: a browser on THIS machine reads the WebSocket token here
    # and configures itself with zero user action. A remote browser is given the token by
    # hand; a token served to any unauthenticated peer would not be a secret.
    #
    # No CORS headers, ever. A malicious web page CAN issue this request, but without
    # Access-Control-Allow-Origin it can never read the response, which keeps the token away
    # from the one attacker who can reach 127.0.0.1 from inside the user's own browser.
    server = req.server
    await req.json_reply({
        # A fresh device token per call: every browser gets its own, and the master never
        # leaves the server (shared-py/browserplug_shared/device_tokens.py).
        "token": server.ws_auth.issue_device_token(),
        "ws_host": server.ws_host,
        "ws_port": server.port,
    })


async def _ws_bind(ep, req):
    # Change the extension WebSocket's bind address, then restart onto it (the options
    # page's "allow browsers on other machines" button). The TOKEN policy is what keeps a
    # web page out: a custom header cannot cross origins without a CORS preflight, which
    # this app never answers.
    server = req.server
    try:
        parsed = json.loads(req.body or b"{}") or {}
        requested = parsed.get("host")
        requested_mode = parsed.get("mode")
    except Exception:
        await http_auth.refuse(req.send, 400, "invalid JSON body")
        return
    if requested_mode is not None and requested_mode not in WS_MODE_VALUES:
        await http_auth.refuse(req.send, 400, "mode must be 'lan' or 'wan'")
        return
    if requested is not None and str(requested).strip():
        # Persisted as given, so an address no interface owns would fail every later start.
        # An interface scan plus a hostname lookup: off the loop like every other one.
        assigned = [row.get("ip") for row in await asyncio.to_thread(server.ws_lan_addresses)]
        invalid = ws_host_policy.validate_ws_host(requested, assigned)
        if invalid:
            await http_auth.refuse(req.send, 400, invalid)
            return

    host, error = server.set_ws_host(requested, requested_mode)
    if error:
        await http_auth.refuse(req.send, 409, error)
        return

    await req.json_reply({"status": "restarting", "ws_host": host,
                          "ws_accepts_remote": not ep.is_localhost_bind(host),
                          "ws_mode": server.current_ws_mode(),
                          "pid": os.getpid()})
    server._restart_onto(host)


async def _reset_config(ep, req):
    # Delete config.json: the server half of the options page's "Reset all settings to
    # defaults". Guarded like /ws-bind, for the same reason.
    server = req.server
    rebind_host, error = server.reset_config()
    if error:
        await http_auth.refuse(req.send, 500, error)
        return

    # Answer BEFORE restarting, or the caller reads a dropped connection as a failed reset
    # (the same ordering /ws-bind depends on).
    await req.json_reply({"status": "restarting" if rebind_host else "reset",
                          "config_deleted": True,
                          "ws_host": server.ws_host,
                          "ws_mode": server.current_ws_mode(),
                          "restarting": bool(rebind_host),
                          "pid": os.getpid()})
    if rebind_host:
        server._restart_onto(rebind_host)


async def _timing(ep, req):
    # The stdio launcher POSTs how long each tools/call took as the CLIENT experienced it;
    # merged into last_command's timing breakdown. Diagnostics only; nothing depends on it.
    body = await req.read_body()
    matched = False
    try:
        matched = req.server.record_launcher_timing(json.loads(body or b"{}"))
    except Exception as e:
        logger.debug(f"launcher timing report rejected: {e}")
    await req.json_reply({"matched": matched}, no_store=False)


async def _restart_verdict(verdict):
    """The replacement's verdict if it arrives within VERDICT_WAIT_SECONDS, else None.

    A verdict that is late is not an error: the drain may be waiting on in-flight calls,
    and the options page gives up on this request after 6 s. The bound keeps the answer
    "restarting", as it always was, for those; a replacement that dies on import is
    normally reported inside it.
    """
    try:
        return await asyncio.wait_for(asyncio.wrap_future(verdict),
                                      timeout=graceful_restart.VERDICT_WAIT_SECONDS)
    except asyncio.TimeoutError:
        return None


async def _stop(req, action):
    # Consume the request body so the response flushes cleanly.
    await req.read_body()
    server = req.server

    if action == "restart":
        logger.info("Restart requested via /restart")
        try:
            verdict = server._begin_restart("POST /restart")
        except graceful_restart.RestartInProgress as e:
            # A second POST would otherwise spawn a second replacement pair.
            await http_auth.refuse(req.send, 409, str(e))
            return
        if verdict is not None:
            error = await _restart_verdict(verdict)
            if error:
                await http_auth.refuse(req.send, 500, f"restart abandoned, server still "
                                                      f"running: {error}")
                return
        await req.json_reply({"status": "restarting", "pid": os.getpid()}, no_store=False)
        return

    await req.json_reply({"status": action + "ing", "pid": os.getpid()}, no_store=False)

    def _do_shutdown():
        time.sleep(0.3)  # let the HTTP response flush first
        server.lifecycle.stopping = True  # intentional exit, not an MCP crash
        logger.info("Shutdown requested via /shutdown — exiting")
        os._exit(0)

    threading.Thread(target=_do_shutdown, daemon=True).start()


async def _shutdown(ep, req):
    await _stop(req, "shutdown")


async def _restart(ep, req):
    await _stop(req, "restart")


# ---------------------------------------------------------------------------
# The route table
# ---------------------------------------------------------------------------

def _exact(path):
    return path


def _rstrip(path):
    return path.rstrip("/")


def _strip(path):
    return "/" + path.strip("/")


@dataclass(frozen=True)
class Route:
    name: str
    method: str
    paths: tuple
    policy: str
    handler: Callable
    # How the request path is normalized before it is compared with `paths`. The rows keep
    # the matching each endpoint had before the table existed.
    normalize: Callable = _rstrip
    # TOKEN and CONTROL routes: the 403 message when the signature is missing.
    deny_message: Optional[str] = None
    # Refuse anything a web page sent (cross_site_refusal) before the signature is looked
    # at. CONTROL rows always get it; /ws-bind and /reset-config set it too, so they are
    # no longer protected by the custom header alone.
    refuse_cross_site: bool = False

    def matches(self, method, path):
        return method == self.method and self.normalize(path) in self.paths


ROUTES = (
    Route("health", "GET", ("/health", "/health/"), LOCAL, _health, normalize=_exact),
    Route("ready", "GET", (probes.READY_PATH,), LOCAL, _ready, normalize=_strip),
    Route("diagnose-wan", "GET", ("/diagnose-wan",), CONTROL, _diagnose_wan,
          deny_message="the connection token is required to run the network diagnosis",
          refuse_cross_site=True),
    Route("doctor", "GET", ("/doctor",), LOOPBACK, _doctor),
    Route("ws-token", "GET", (http_auth.WS_TOKEN_ENDPOINT_PATH,), LOOPBACK, _ws_token),
    Route("ws-bind", "POST", ("/ws-bind",), TOKEN, _ws_bind,
          deny_message="the connection token is required to change the bind address",
          refuse_cross_site=True),
    Route("reset-config", "POST", ("/reset-config",), TOKEN, _reset_config,
          deny_message="the connection token is required to reset the server settings",
          refuse_cross_site=True),
    Route("timing", "POST", ("/timing",), CONTROL, _timing,
          deny_message="the connection token is required to report timings",
          refuse_cross_site=True),
    Route("shutdown", "POST", ("/shutdown",), CONTROL, _shutdown,
          deny_message="the connection token is required to stop the server "
                       "(python server/control.py shutdown)",
          refuse_cross_site=True),
    Route("restart", "POST", ("/restart",), CONTROL, _restart,
          deny_message="the connection token is required to restart the server "
                       "(python server/control.py restart)",
          refuse_cross_site=True),
)


def find_route(method, path) -> Optional[Route]:
    for route in ROUTES:
        if route.matches(method, path):
            return route
    return None


# ---------------------------------------------------------------------------
# The ASGI entry point
# ---------------------------------------------------------------------------

class HttpEndpoints:
    """Wraps the MCP ASGI app. Call `handle(scope, receive, send)` per request.

    `era` is the mcp.era setting the protocol gate applies. Left out it is read once,
    here, so a change takes the restart the setting documents."""

    def __init__(self, server, asgi_app, *, is_localhost_bind, host_check=None, era=None,
                 host_check_async=None, on_loop=None):
        self.server = server
        self.asgi_app = asgi_app
        self.is_localhost_bind = is_localhost_bind
        self.host_check = host_check or permissive_host_check
        # The same check for the loop (host_guard.HostGuard.check_async), which keeps its
        # interface lookups off it. Without one the sync hook is called inline.
        self.host_check_async = host_check_async
        # Called with the serving loop when the ASGI lifespan starts: how the watchdog
        # learns which loop uvicorn runs on (it does not expose it).
        self.on_loop = on_loop
        self.owner_gate = peer_owner.PeerOwnerGate()
        self.era = era if era is not None else protocol_era.configured_era()
        # session id -> the identity lookup in flight for it (a concurrent Future).
        self._identity_pending = {}

    async def handle(self, scope, receive, send):
        if scope.get("type") != "http":
            if scope.get("type") == "lifespan" and self.on_loop is not None:
                try:
                    self.on_loop(asyncio.get_running_loop())
                except Exception:
                    logger.debug("on_loop hook failed", exc_info=True)
            await self.asgi_app(scope, receive, send)
            return

        server = self.server
        if self.host_check_async is not None:
            refusal = await self.host_check_async(scope)
        else:
            refusal = self.host_check(scope)
        if refusal is not None:
            await http_auth.refuse(send, refusal[0], refusal[1])
            return

        # ONE gate, before any endpoint runs: this port follows the LAN/WAN bind, so the
        # token does the job of the loopback bind. Local peers are unaffected. Endpoints may
        # still demand a token from local callers too (TOKEN routes); this settles only
        # whether the PEER may proceed.
        peer_host = (scope.get("client") or ("", 0))[0]
        # Verified ONCE per request: a valid signature spends its nonce, so the routes
        # below read this answer instead of checking again.
        signed = request_signing.verify_scope(scope, server.ws_auth.device_secret)
        scope["browserplug.signed"] = signed
        decision, status, message = http_auth.authorize(
            scope.get("path", ""), peer_host, signed)
        if decision != http_auth.ALLOW:
            logger.warning(
                f"Refused {scope.get('method')} {scope.get('path')} from {peer_host}: "
                f"{decision}")
            await http_auth.refuse(send, status, message)
            return

        # Loopback reaches every OS user's processes; a peer owned by another account
        # gets nothing (server/web/peer_owner.py; fails open when the owner is unresolvable).
        if http_auth.is_local_peer(peer_host) and await self.owner_gate.is_foreign(
                (scope.get("client") or ("", 0))[1]):
            logger.warning(f"Refused {scope.get('method')} {scope.get('path')} from "
                           f"{peer_host}: peer process belongs to another OS user")
            await http_auth.refuse(send, 403, "this server only serves processes of the "
                                              "OS user that runs it")
            return

        route = find_route(scope.get("method"), scope.get("path", ""))
        if route is not None:
            req = Request(scope, receive, send, server)
            if route.policy == LOOPBACK and not http_auth.is_local_peer(peer_host):
                await json_reply(send, {"error": "localhost only"}, status=403,
                                 no_store=False)
                return
            if route.refuse_cross_site:
                # Before the body is read: a page's request gets no more of our time.
                why = cross_site_refusal(scope)
                if why:
                    logger.warning(f"Refused {scope.get('method')} {scope.get('path')}: {why}")
                    await http_auth.refuse(send, 403, why)
                    return
            if route.policy in (TOKEN, CONTROL):
                # The decision comes from the headers alone; a caller without a signature
                # gets its 403 and none of the body is read.
                if not await require_token(scope, send, route.deny_message):
                    return
                try:
                    await req.read_body()
                except BodyTooLarge:
                    await http_auth.refuse(send, 413, "request body too large")
                    return
                # The signature was checked before the body existed; now the bytes that
                # arrived must be the ones it covers (S7).
                why = request_signing.body_problem(
                    scope.get(request_signing.SCOPE_KEY), req.body)
                if why:
                    logger.warning(f"Refused {scope.get('method')} {scope.get('path')} "
                                   f"from {peer_host}: {why}")
                    await http_auth.refuse(send, 403, why)
                    return
            await route.handler(self, req)
            return

        await self._forward_to_mcp(scope, receive, send)

    async def _forward_to_mcp(self, scope, receive, send):
        handled, receive = await protocol_era.gate(scope, receive, send, self.era)
        if handled:
            return
        # What the tool layer reads off this request (the session id, the client pid the
        # launcher forwards), bound for the SDK app's handlers, which run in a copy of this
        # request's context (request_context.py).
        headers = request_context.headers_of(scope.get("headers"))
        session_key = None
        if protocol_era.is_modern_version(headers.get("mcp-protocol-version")):
            session_key = modern_identity.connection_key(headers, scope.get("client"))
        if session_key is not None:
            # A 2026-07-28 request names no session, yet the tool layer files the client's
            # row, its calls' verdicts and its browser selection under `mcp-session-id`.
            # The connection key goes there, SYNTHETIC. Only this bound copy carries it:
            # the scope the SDK app sees is untouched.
            headers = MappingProxyType({**headers, modern_identity.SESSION_HEADER: session_key})
        token = request_context.bind(headers)
        try:
            await self._track_and_forward(scope, receive, send, session_key)
        finally:
            request_context.reset(token)

    def _note_client(self, sid, client, client_pid):
        """File the session's row (cheap, on the loop) and start its identity lookup on a
        worker. Returns the lookup in flight for the session, or None."""
        server = self.server
        entry = server.note_http_client(sid, client, client_pid=client_pid)
        if entry is None:
            return self._identity_pending.get(sid)
        if len(self._identity_pending) >= MAX_IDENTITY_PENDING:
            logger.debug("identity lookup skipped: %d already in flight",
                         len(self._identity_pending))
            entry["identity_tried"] = None   # a later request may try again
            return None
        future = identity_pool().submit(server.identify_http_client, entry, client, client_pid)
        self._identity_pending[sid] = future
        entry["identity_pending"] = True      # routing.client_key: no final key yet
        loop = asyncio.get_running_loop()

        def done(finished, sid=sid):
            self._identity_pending.pop(sid, None)
            entry["identity_pending"] = False
            if finished.cancelled():
                return
            error = finished.exception()
            if error is not None:
                logger.debug(f"client identity lookup failed: {error!r}")
                return
            try:
                loop.call_soon_threadsafe(self._rekey_selection, sid)
            except RuntimeError:
                pass    # the loop is gone: the server is stopping
        future.add_done_callback(done)
        return future

    def _rekey_selection(self, sid):
        """The lookup landed: a browser the client chose while it was still anonymous (filed
        under its "session:" key) follows it to the key its identity gives it."""
        selection = getattr(self.server, "browser_selection", None)
        if selection is None:
            return
        try:
            routing.rekey_selection(selection, sid, self.server.http_clients)
        except Exception as e:
            logger.debug(f"could not re-key the selection of session {sid}: {e!r}")

    @staticmethod
    async def _identity_settled(future):
        """Wait for an identity lookup, but only IDENTITY_WAIT_SECONDS and without ever
        raising: the call goes ahead whether or not the client could be named. Shielded, so
        giving up on the wait does not cancel a lookup that has not started yet."""
        try:
            await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(future)),
                                   IDENTITY_WAIT_SECONDS)
        except Exception:
            pass

    async def _track_and_forward(self, scope, receive, send, session_key=None):
        server = self.server
        asgi_app = self.asgi_app
        sid = None
        identity = None    # the session's identity lookup, when one is in flight
        client = scope.get("client")  # (host, port) or None
        try:
            headers = dict(scope.get("headers") or [])
            sid_b = headers.get(b"mcp-session-id")
            # A request without a session header and with a connection key is 2026-07-28.
            sid = session_key or (sid_b.decode("latin-1") if sid_b else None)
            # The stdio launcher forwards the real Claude Code PID here so the connection
            # survives the proxy hop (else the peer is the launcher).
            pid_b = headers.get(CLIENT_PID_HEADER.lower().encode("latin-1"))
            client_pid = None
            if pid_b:
                try:
                    client_pid = int(pid_b.decode("latin-1"))
                except (ValueError, AttributeError):
                    client_pid = None
            if sid:
                identity = self._note_client(sid, client, client_pid)
        except Exception as e:
            logger.debug(f"client tracking error: {e}")

        # Buffer POST bodies so we can record tools/call invocations, then replay them
        # transparently to the downstream MCP app.
        if scope.get("method") == "POST" and sid:
            buffered = []
            chunks, size = [], 0
            recorded_call = False
            declared = declared_length(scope)
            if declared is not None and declared > MAX_MCP_BODY:
                await http_auth.refuse(send, 413, "request body too large")
                return
            try:
                while True:
                    msg = await receive()
                    buffered.append(msg)
                    if msg.get("type") == "http.request":
                        chunk = msg.get("body", b"") or b""
                        size += len(chunk)
                        if size > MAX_MCP_BODY:
                            await http_auth.refuse(send, 413, "request body too large")
                            return
                        chunks.append(chunk)
                        if not msg.get("more_body", False):
                            break
                    else:
                        break
                if buffered[-1].get("type") != "http.request":
                    # The client went away before its body arrived (the relay cancels by
                    # shutting the socket). No reply can be delivered, and replaying the
                    # disconnect makes the SDK raise ClientDisconnect out of the app, which
                    # uvicorn logs as an ERROR traceback.
                    logger.debug("client disconnected before its request body was read")
                    return
                body = b"".join(chunks)
                if identity is not None and b"tools/call" in body:
                    await self._identity_settled(identity)
                recorded_call = server._record_from_body(sid, body)
            except Exception as e:
                logger.debug(f"command capture error: {e}")

            idx = {"i": 0}

            async def replay():
                if idx["i"] < len(buffered):
                    m = buffered[idx["i"]]
                    idx["i"] += 1
                    return m
                return await receive()

            out_send = send
            if recorded_call:
                # Stamp when the response for THIS tools/call finishes sending: the end of
                # the server-side leg in last_command.timings. Only a 2xx response counts:
                # a POST to /mcp/ is 307-redirected to /mcp by the app's router before the
                # re-POST does the real work; stamping the redirect would end the call
                # before it began (seen live: server_ms 0.0 with a negative finalize).
                resp_status = {"code": None}

                async def timed_send(message):
                    await send(message)
                    if message.get("type") == "http.response.start":
                        resp_status["code"] = message.get("status")
                    elif (message.get("type") == "http.response.body"
                            and not message.get("more_body", False)
                            and resp_status["code"] is not None
                            and 200 <= resp_status["code"] < 300):
                        server._note_mcp_http_done(sid)
                out_send = timed_send

            await asgi_app(scope, replay, out_send)
            return

        await asgi_app(scope, receive, send)
