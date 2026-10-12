"""The MCP protocol-era policy: what the HTTP gate admits, and how a duplex stream is served.

mcp 2.x serves two wire eras. The handshake era (`initialize` plus an Mcp-Session-Id)
is the one client identity is keyed on (track_http_client, record_command,
BrowserCalls.client_key). 2026-07-28 is stateless and issues no session: modern_identity.py
gives such a request a key, and browserplug_shared/mcp_era.py holds the setting that decides
whether it is served at all (`mcp.era`: handshake, direct or all).

HTTP. The SDK routes a request on its MCP-Protocol-Version header against the module
constant HANDSHAKE_PROTOCOL_VERSIONS (StreamableHTTPSessionManager._handle_request) and has
no switch for it, so this gate answers before the SDK sees the request:

  a handshake version in the header      passes
  2026-07-28 in the header               passes, unless the setting is `handshake`: then
                                         -32022 naming the handshake versions only
  any other header value                 -32022 naming every version the setting serves
  no header, a 2026-07-28 envelope in    -32022 naming the handshake versions only,
  the body, no session                   whatever the setting
  anything else                          passes

Clients in auto mode take a -32022 whose list holds no 2026-07-28 for a handshake-only
server and fall back to `initialize`: mcp.client._probe.negotiate_auto (Python;
tests/unit/test_protocol_era.py drives it) and the TypeScript SDK in Claude Code 2.1.286
(read from its bundle). The headerless row is what a stdio relay that sends no era headers
forwards for a client probing server/discover. The SDK would answer it 400 "Missing session
ID", because it routes on the header alone and marks body-based routing as a follow-up (TODO
L49). Claude Code reads that as legacy too, but a -32022 with a handshake-only list is the
answer defined to mean "fall back", so the gate answers first.

Duplex streams (`--stdio`, the proxy front). `Server.run` serves both eras on one
connection and lets the client's first request choose. serve_duplex puts the setting in front
of it: under `handshake` only the handshake loop runs, where a server/discover probe gets
-32601 and an auto client falls back to `initialize`.
"""

import json
import logging
from typing import NamedTuple, Optional, Tuple

from mcp.server.lowlevel import Server
from mcp.server.runner import serve_loop
from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS, MODERN_PROTOCOL_VERSIONS

from browserplug_shared import mcp_era

UNSUPPORTED_PROTOCOL_VERSION = -32022
PROTOCOL_VERSION_HEADER = b"mcp-protocol-version"
SESSION_HEADER = b"mcp-session-id"
ENVELOPE_VERSION_KEY = "io.modelcontextprotocol/protocolVersion"
MCP_PATH = "/mcp"
# A body that can decide the era is one small JSON-RPC message (a discover probe is under
# 1 KiB). Past this the gate stops buffering and leaves the body to the SDK's own limiter.
MAX_PROBE_BODY = 64 * 1024

logger = logging.getLogger(__name__)


class Refusal(NamedTuple):
    requested: str
    via: str                    # "header" or "envelope"
    supported: Tuple[str, ...]  # what the -32022 names


def is_mcp_path(path):
    return path == MCP_PATH or path.startswith(MCP_PATH + "/")


def configured_era() -> str:
    """The era setting, logged with where it came from and with any value it skipped.

    The server reads it once per process: HttpEndpoints when it is built, and serve_duplex
    for each duplex connection."""
    setting = mcp_era.resolve()
    logger.info("MCP protocol era: %s (%s)", setting.era, setting.source)
    for skipped in setting.ignored:
        logger.warning("Ignored %s: not one of %s", skipped, ", ".join(mcp_era.ERA_VALUES))
    return setting.era


def is_modern_version(value) -> bool:
    """Whether a MCP-Protocol-Version value names a version outside the handshake era,
    which is how the SDK routes a request to its stateless entry."""
    version = (value or "").strip()
    return bool(version) and version not in HANDSHAKE_PROTOCOL_VERSIONS


def supported_versions(era: str) -> Tuple[str, ...]:
    """The protocol versions the server serves over HTTP under `era`."""
    if mcp_era.serves_modern(era):
        return tuple(HANDSHAKE_PROTOCOL_VERSIONS) + tuple(MODERN_PROTOCOL_VERSIONS)
    return tuple(HANDSHAKE_PROTOCOL_VERSIONS)


def _single_message(body):
    """The one JSON-RPC object in `body`, or None (batch, empty, unparseable)."""
    if not body:
        return None
    try:
        msg = json.loads(body)
    except (ValueError, RecursionError):
        return None
    return msg if isinstance(msg, dict) else None


def header_version(headers) -> Optional[str]:
    """The MCP-Protocol-Version header's value, or None when the request has none.
    headers: {lowercase bytes: bytes}."""
    raw = headers.get(PROTOCOL_VERSION_HEADER)
    return None if raw is None else raw.decode("latin-1").strip()


def _header_refused(version: str, era: str) -> bool:
    if version in HANDSHAKE_PROTOCOL_VERSIONS:
        return False
    return not (version in MODERN_PROTOCOL_VERSIONS and mcp_era.serves_modern(era))


def refusal(headers, body, era: str = mcp_era.ERA_DEFAULT) -> Optional[Refusal]:
    """Why this request is refused, or None to let it through.

    headers: {lowercase bytes: bytes}; body: the POST body, or None when not read."""
    version = header_version(headers)
    if version is not None:
        if _header_refused(version, era):
            return Refusal(version, "header", supported_versions(era))
        return None
    if body is None or headers.get(SESSION_HEADER):
        return None
    msg = _single_message(body)
    if msg is None or msg.get("method") == "initialize":
        return None
    params = msg.get("params")
    meta = params.get("_meta") if isinstance(params, dict) else None
    if isinstance(meta, dict) and ENVELOPE_VERSION_KEY in meta:
        requested = str(meta[ENVELOPE_VERSION_KEY])
        if requested not in HANDSHAKE_PROTOCOL_VERSIONS:
            return Refusal(requested, "envelope", tuple(HANDSHAKE_PROTOCOL_VERSIONS))
    return None


def rejection_body(request_id, refused: Refusal):
    return json.dumps({
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {
            "code": UNSUPPORTED_PROTOCOL_VERSION,
            "message": "Unsupported protocol version",
            "data": {"supported": list(refused.supported), "requested": refused.requested},
        },
    }).encode("utf-8")


async def gate(scope, receive, send, era: str = mcp_era.ERA_DEFAULT):
    """(handled, receive). handled=True: the request was answered here. Otherwise the
    returned receive replays whatever the gate consumed, so pass it downstream."""
    if scope.get("type") != "http" or not is_mcp_path(scope.get("path", "")):
        return False, receive
    # The first of a repeated header wins, as the SDK routes on it (the session manager
    # reads the first MCP-Protocol-Version) and as request_context binds it. dict() would
    # keep the last, and `2026-07-28, 2025-11-25` would pass the gate and be routed modern.
    headers = {}
    for name, value in scope.get("headers") or ():
        headers.setdefault(name, value)
    version = header_version(headers)
    # Read the body only where a verdict can depend on it or an id is owed: a refused
    # header owes the request's id, and a request with no header at all and no session may
    # carry the envelope. A request with a header the policy admits streams through
    # untouched, and so does a body too large to be a probe.
    try:
        declared = int(headers.get(b"content-length", b"0"))
    except ValueError:
        declared = 0
    wants_body = (scope.get("method") == "POST" and declared <= MAX_PROBE_BODY
                  and (_header_refused(version, era) if version is not None
                       else not headers.get(SESSION_HEADER)))
    buffered, body = [], None
    if wants_body:
        chunks, size, complete = [], 0, False
        while size <= MAX_PROBE_BODY:
            message = await receive()
            buffered.append(message)
            if message.get("type") != "http.request":
                break
            chunk = message.get("body", b"") or b""
            chunks.append(chunk)
            size += len(chunk)
            if not message.get("more_body", False):
                complete = True
                break
        # An oversized or cut-off body cannot be a probe: judge on the header alone and
        # replay what was read, then the rest straight from the client.
        body = b"".join(chunks) if complete else None

    refused = refusal(headers, body, era)
    if refused is None:
        if not buffered:
            return False, receive
        pending = list(buffered)

        async def replay():
            if pending:
                return pending.pop(0)
            return await receive()
        return False, replay

    msg = _single_message(body)
    request_id = msg.get("id") if msg is not None else None
    client = scope.get("client") or ("?", "?")
    logger.info("Refused MCP protocol %s (%s) on %s %s from %s:%s", refused.requested,
                refused.via, scope.get("method"), scope.get("path"), client[0], client[1])
    await send({"type": "http.response.start", "status": 400,
                "headers": [(b"content-type", b"application/json")]})
    await send({"type": "http.response.body", "body": rejection_body(request_id, refused)})
    return True, receive


async def serve_duplex(server, read_stream, write_stream, init_options,
                       raise_exceptions: bool = False, era: Optional[str] = None):
    """Serve one duplex connection under the era setting (`era`, else configured_era()).

    Where the setting serves 2026-07-28 this is the SDK's own dual-era loop. Under
    `handshake` it is the handshake-only loop inside the server's lifespan, which is what
    Server.run wraps. Callers are run_stdio and ProxyServer.run, not the in-process test
    transport: that one calls Server.run directly and keeps the dual-era loop.

    It calls Server.run itself rather than `server.run`, because ProxyServer.run calls
    this and would otherwise recurse into itself."""
    if era is None:
        era = configured_era()
    if mcp_era.serves_modern(era):
        await Server.run(server, read_stream, write_stream, init_options, raise_exceptions)
        return
    async with server.lifespan(server) as lifespan_state:
        await serve_loop(server, read_stream, write_stream, lifespan_state=lifespan_state,
                         init_options=init_options, raise_exceptions=raise_exceptions)
