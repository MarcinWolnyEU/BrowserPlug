"""Frame-size limits and the unauthenticated-socket cap for the extension WebSocket.

Stdlib only. See .tmp/dev-docs/ws-token-auth.md (pre-auth limits) and .tmp/dev-docs/tool-surface.md (the
largest legitimate reply).

Two limits, because a socket's needs change at the hello:

* Before it has proved a token a peer may send only the handshake (a 35-byte request, a
  1,138-byte proof, a health probe, a hello). PRE_AUTH_MAX_MESSAGE_BYTES is far above
  that and small enough that the frames held and queued cost ~1 MiB per socket.
* After it has, the largest legitimate reply is a base64 screenshot or a captured body.
  websockets' default of 1 MiB sat under both and closed the socket with 1009 for every
  client, so POST_AUTH_MAX_MESSAGE_BYTES covers them.

websockets reads `protocol.max_message_size` per frame, so raising it on an established
connection takes effect on the next frame; no reconnect.
"""

import logging
from collections import defaultdict

logger = logging.getLogger(__name__)

PRE_AUTH_MAX_MESSAGE_BYTES = 64 * 1024
# What page_screenshot and requests can return, sealed:
#   * full-page capture is capped at 20,000 CSS px tall (background.js MAX_HEIGHT); a
#     1920 px wide PNG at dpr 1 is up to ~150 MB raw and typically 5-25 MB, base64 on top;
#   * a captured body is clamped to 1,000,000 bytes, 1.33 MB as base64.
# 32 MiB carries the first with headroom and still bounds what one authenticated peer can
# make the server hold. A reply above it closes the socket with 1009; the server then
# tells the waiting caller so (oversize_reason) instead of "the browser disconnected".
POST_AUTH_MAX_MESSAGE_BYTES = 32 * 1024 * 1024
# Frames queued unread per socket before the library stops reading the TCP stream. The
# default is 16, which times the post-auth limit is a lot of memory for backpressure that
# four frames give just as well.
MAX_QUEUED_FRAMES = 4

# Sockets that have connected and not yet authenticated. Per peer address, because every
# local web page shares 127.0.0.1; in total, because a LAN bind has many peers.
MAX_PREAUTH_PER_PEER = 8
MAX_PREAUTH_TOTAL = 32
# "Try again later": the extension backs off and retries, unlike 4401 which tells it to
# go and fetch a token.
CLOSE_TRY_LATER = 1013
CLOSE_TOO_BIG = 1009
BUSY_REASON = "too many unauthenticated connections; retry shortly"


def serve_kwargs() -> dict:
    """The websockets.serve arguments that carry the pre-auth limits."""
    return {"max_size": PRE_AUTH_MAX_MESSAGE_BYTES, "max_queue": MAX_QUEUED_FRAMES}


def raise_limit(websocket) -> None:
    """Lift the inbound limit once the peer has authenticated. Never raises: a test
    double or a wrapper without a protocol simply keeps whatever limit it has."""
    target = getattr(websocket, "raw", websocket)
    protocol = getattr(target, "protocol", None)
    if protocol is not None and hasattr(protocol, "max_message_size"):
        protocol.max_message_size = POST_AUTH_MAX_MESSAGE_BYTES


class PreauthGate:
    """Counts sockets that have not authenticated yet."""

    def __init__(self, per_peer: int = MAX_PREAUTH_PER_PEER,
                 total: int = MAX_PREAUTH_TOTAL):
        self.per_peer, self.total = per_peer, total
        self._by_peer = defaultdict(int)

    def try_acquire(self, peer) -> bool:
        if sum(self._by_peer.values()) >= self.total or self._by_peer[peer] >= self.per_peer:
            return False
        self._by_peer[peer] += 1
        return True

    def release(self, peer) -> None:
        if self._by_peer.get(peer, 0) <= 1:
            self._by_peer.pop(peer, None)
        else:
            self._by_peer[peer] -= 1

    def held(self) -> int:
        return sum(self._by_peer.values())


def oversize_reason(browser_describe: str) -> str:
    """The agent-facing failure for a request whose reply closed the socket with 1009."""
    limit_mib = POST_AUTH_MAX_MESSAGE_BYTES // (1024 * 1024)
    return (f"{browser_describe} sent a reply that was too large for the server's "
            f"WebSocket limit ({limit_mib} MiB), so the connection was closed. The "
            f"extension reconnects within seconds. Ask for less: a selector or clip for "
            f"page_screenshot instead of a very tall full page, or page_read with a "
            f"smaller max_chars.")
