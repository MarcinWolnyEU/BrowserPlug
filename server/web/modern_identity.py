"""The key a 2026-07-28 request is filed under, since it carries no Mcp-Session-Id.

Everything that follows a client through the tool layer is keyed on the session id: its
Connections row (`track_http_client`), the record its tool calls open and receive their
verdict on (`CallTelemetry`, `[:8]` of the key plus the request id), and its browser
selection (`routing.client_key`). A 2026-07-28 request has none, so without a key it gets
no row, no `last_command`, no verdict, and every such client shares the "local" selection.

`HttpEndpoints` computes the key once per modern request and binds it into the request
context as `mcp-session-id` (the SDK never sees it), which hands the three consumers a
session id with no change on their side. Where it comes from:

  1. X-BrowserPlug-Connection-Id, when it is a well-formed connection id (32 lowercase
     hex). The stdio relay mints one per process, so the key names one stdio connection
     and survives a server restart.
  2. Otherwise a hash of the TCP peer, `ip:port`: one key per connection. An HTTP client
     that reuses its connection keeps one key. The Connections report folds rows by pid,
     so several keys of one process show as one client with several sessions.

Both are 32 hex characters, the shape of a real session id. Telemetry compares only the
first 8 characters of a session id, so a key must not begin with a fixed prefix. Both
sources are random or hashed, so those 8 characters tell connections apart.

A caller that can already send a connection id could send a forged one, but it is a
same-user loopback process past the owner gate, and it can already claim any pid with
X-BrowserPlug-Client-Pid: no new trust boundary.

Stdlib only; no event loop, no I/O.
"""

import hashlib
from typing import Mapping, Optional, Sequence

from browserplug_shared import mcp_era

# The request-context name the key is bound under: the one the tool layer reads.
SESSION_HEADER = "mcp-session-id"


def peer_key(peer: Optional[Sequence]) -> Optional[str]:
    """The key for a TCP peer `(host, port)`, or None when the transport named none."""
    if not peer or len(peer) < 2 or peer[0] is None or peer[1] is None:
        return None
    digest = hashlib.sha1(f"{peer[0]}:{peer[1]}".encode("utf-8"), usedforsecurity=False)
    return digest.hexdigest()[:32]


def connection_key(headers: Mapping[str, str], peer: Optional[Sequence]) -> Optional[str]:
    """The key for a 2026-07-28 request, or None to leave it unkeyed.

    `headers` is {lowercase name: value}. A request that names a session already has its
    identity, and a request with no connection id and no peer has nothing to derive one
    from. The caller decides that the request is modern; this does not look at the
    protocol version."""
    if headers.get(SESSION_HEADER):
        return None
    announced = (headers.get(mcp_era.CONNECTION_ID_HEADER.lower()) or "").strip()
    if mcp_era.is_connection_id(announced):
        return announced
    return peer_key(peer)
