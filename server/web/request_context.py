"""The headers of the HTTP request an MCP message arrived on, for the tool layer.

The tool layer needs two request headers and nothing else: `Mcp-Session-Id` (the client's
session, the key its telemetry record is filed under) and `X-BrowserPlug-Client-Pid` (the
real client behind a stdio launcher, the key its browser selection is stored under).
`HttpEndpoints` binds the headers of every /mcp request here before it hands the request to
the SDK app. The SDK runs each request's handler inside a copy of the context of the POST
that delivered it (its transport streams snapshot the sender's context per message), so a
tools/call handler, and every tool it runs, reads the headers of its own POST, even with
several calls in flight on one session.

Nothing binds it on stdio or for an in-process client: `current_headers()` is None there,
and callers fall back to their no-HTTP behaviour.

Stdlib only; importing it has no side effects.
"""

from contextlib import contextmanager
from contextvars import ContextVar, Token
from types import MappingProxyType
from typing import Iterable, Iterator, Mapping, Optional, Tuple

_HEADERS: ContextVar[Optional[Mapping[str, str]]] = ContextVar(
    "browserplug_mcp_request_headers", default=None)


def headers_of(raw: Iterable[Tuple[bytes, bytes]]) -> Mapping[str, str]:
    """ASGI header pairs as a read-only {lowercase name: value} map. The first occurrence
    of a repeated header wins, as it does for Starlette's `Headers.get`."""
    headers = {}
    for name, value in raw or ():
        headers.setdefault(name.decode("latin-1").lower(), value.decode("latin-1"))
    return MappingProxyType(headers)


def bind(headers: Mapping[str, str]) -> Token:
    """Make `headers` the current request's; pass the token to `reset` afterwards."""
    return _HEADERS.set(headers)


def reset(token: Token) -> None:
    _HEADERS.reset(token)


@contextmanager
def bound(headers: Mapping[str, str]) -> Iterator[None]:
    """`bind` for the duration of a block (tests use it to act as an HTTP client)."""
    token = bind(MappingProxyType({k.lower(): v for k, v in headers.items()}))
    try:
        yield
    finally:
        reset(token)


def current_headers() -> Optional[Mapping[str, str]]:
    """The current request's headers, or None when the call did not arrive over HTTP."""
    return _HEADERS.get()


def header(name: str) -> Optional[str]:
    """One header of the current request (`name` in any case), or None."""
    headers = _HEADERS.get()
    return headers.get(name.lower()) if headers is not None else None
