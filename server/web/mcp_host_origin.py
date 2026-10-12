"""The second Host/Origin check on the MCP app.

It sits in front of everything the MCP app receives: every request HttpEndpoints falls
through with (/mcp, /mcp/ and any path without a route row), after HostGuard, the peer and
owner gates, the protocol-era gate and session tracking. The rules and the response bytes
are those of fastmcp 4.0.10's HostOriginGuardMiddleware in mode "auto" with no
allowed_hosts and no allowed_origins, which guarded this app before the SDK migration.
`tests/unit/test_host_guard.py` pins the verdicts (the `[with-origin-layer]` variant).

  Host    checked only when the socket the request arrived on (scope["server"]) is a
          loopback address. It must then name 127.0.0.1, localhost, ::1 or that socket
          address, after the port and IPv6 brackets are dropped and nothing else is
          normalised. Anything else, a missing Host included, gets 421 "Misdirected
          Request".
  Origin  checked when present and either the Host or the socket is loopback. It passes
          when its host and the Host are both loopback, or when it equals scheme://Host
          (default ports filled in); otherwise 403 "Forbidden Origin".

HostGuard (host_guard.py) is the first check and admits more: this machine's hostname and
interface addresses, BROWSERPLUG_ALLOWED_HOSTS, the WebSocket bind host, other spellings
of loopback, and requests with no Host. Those reach /health and the other routes; on a
loopback socket they do not reach the MCP app. Refusals here are not logged.

Stdlib only, no import-time work. BrowserPlugMCPServer.http_app installs HostOriginLayer
as the outermost layer of the MCP app.
"""

from fnmatch import fnmatchcase
from ipaddress import ip_address
from typing import Optional, Sequence, Tuple
from urllib.parse import urlsplit

DEFAULT_HOSTS = ("127.0.0.1", "localhost", "::1")

MISDIRECTED = (421, b"Misdirected Request")
FORBIDDEN_ORIGIN = (403, b"Forbidden Origin")

# The helpers below keep quirks that test_host_guard pins: `localhost.` and
# `[0:0:0:0:0:0:0:1]` are not canonicalised, and an unbracketed IPv6 Host keeps its port.
# Parity with the pre-SDK guard is the point; do not tidy them.


def _normalize_host(host: str) -> str:
    host = host.strip().lower()
    if not host:
        return ""
    if host.startswith("["):
        end = host.find("]")
        if end == -1:
            return host
        return host[1:end]
    if host.count(":") == 1:
        return host.rsplit(":", 1)[0]
    return host


def _is_loopback_host(host: str) -> bool:
    host = _normalize_host(host)
    if host == "localhost":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def _is_unspecified_host(host: str) -> bool:
    host = _normalize_host(host)
    if not host:
        return True
    try:
        return ip_address(host).is_unspecified
    except ValueError:
        return False


def _host_matches(host: str, allowed_hosts: Sequence[str]) -> bool:
    host = _normalize_host(host)
    for allowed_host in allowed_hosts:
        pattern = _normalize_host(allowed_host)
        if pattern == "*" or fnmatchcase(host, pattern):
            return True
    return False


def _origin_host(origin: str) -> str:
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return ""
    return parsed.hostname or ""


def _origin_port(scheme: str, port: Optional[int]) -> Optional[int]:
    if port is not None:
        return port
    if scheme == "http":
        return 80
    if scheme == "https":
        return 443
    return None


def _format_origin_host(host: str) -> str:
    if ":" in host and not host.startswith("["):
        return f"[{host}]"
    return host


def _normalize_origin(origin: str) -> str:
    origin = origin.strip().rstrip("/")
    try:
        parsed = urlsplit(origin)
        port = parsed.port
    except ValueError:
        return origin.lower()
    if not parsed.scheme or not parsed.hostname:
        return origin.lower()
    if parsed.path or parsed.query or parsed.fragment:
        return origin.lower()
    scheme = parsed.scheme.lower()
    host = _format_origin_host(_normalize_host(parsed.hostname))
    normalized_port = _origin_port(scheme, port)
    if normalized_port is None:
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{normalized_port}"


def _header(scope, name: bytes) -> Optional[str]:
    """Starlette's `Headers(scope=scope).get(name)`: the first header whose name is exactly
    `name` (ASGI servers lower-case header names)."""
    for key, value in scope.get("headers") or ():
        if key == name:
            return value.decode("latin-1")
    return None


def refusal(scope) -> Optional[Tuple[int, bytes]]:
    """None to admit an HTTP request, or (status, body) to answer it with instead."""
    host = _header(scope, b"host") or ""
    server = scope.get("server")
    on_loopback = bool(server and _is_loopback_host(server[0]))

    if on_loopback:
        allowed = list(DEFAULT_HOSTS)
        if not _is_unspecified_host(server[0]):
            allowed.append(server[0])
        if not _host_matches(host, allowed):
            return MISDIRECTED

    origin = _header(scope, b"origin")
    if origin and (_is_loopback_host(host) or on_loopback):
        if _is_loopback_host(_origin_host(origin)) and _is_loopback_host(host):
            return None
        request_origin = _normalize_origin(f"{scope.get('scheme', 'http')}://{host}")
        if _normalize_origin(origin) != request_origin:
            return FORBIDDEN_ORIGIN
    return None


class HostOriginLayer:
    """ASGI middleware applying `refusal` to every HTTP request (Starlette's
    `app.add_middleware(HostOriginLayer)`). Other scope types pass untouched."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        refused = refusal(scope) if scope["type"] == "http" else None
        if refused is None:
            await self.app(scope, receive, send)
            return
        status, body = refused
        # What starlette's Response(body, status_code=status) sends: a content-length and
        # no content-type (clients saw this shape before the SDK migration).
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-length", str(len(body)).encode("latin-1"))]})
        await send({"type": "http.response.body", "body": body})
