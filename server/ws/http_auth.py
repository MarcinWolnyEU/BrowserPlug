"""Who may call what on the MCP HTTP port, isolated from server.py on purpose.

Until now that port answered only 127.0.0.1, and the bind WAS the security model: every
endpoint that mattered re-checked the peer and 403'd, `/health` did not bother to check at
all because nothing else could reach it, and a browser on another machine simply got a
connection timeout. That is why the LAN switch moved only the WebSocket: exposing this port
would have handed `/health` — connected clients' pids, working directories, command lines,
the last command run, browser executable paths — to anyone on the network.

The port now follows the LAN/WAN setting like the WebSocket does, and the token takes over
the job the bind was doing. server.py's involvement is a call into http_endpoints.py, whose gate runs
`authorize` before any route; the policy lives here.

    local peer                  allowed, no token (the launcher and the token bootstrap
                                both predate any token and must keep working)
    remote peer, loopback-only  refused, whatever it presents
    remote peer, anything else  a valid X-BrowserPlug-Auth signature, else 403

Two paths stay loopback-only no matter what the setting says:

`/ws-token` hands out the credential, so it cannot be protected by the credential. Serving
it to unauthenticated peers would mean anyone who can reach the port can ask for the key,
which is the assumption `.tmp/dev-docs/ws-token-auth.md` is built on. A remote browser is set up by
pasting the token in by hand; that asymmetry is the design, not a gap.

`/mcp` is the MCP protocol endpoint: the full tool surface against a logged-in browser.
A token that leaks off a phone screen should cost the reader a look at the server's state,
not the ability to read every page the user is signed in to. Decided 2026-09-02 with the
user, who chose this over token-gating it.
"""

import json

# Peers that count as "this machine": the ONE definition. server.py, preflight.py and
# http_endpoints.py all ask `is_local_peer`, so the copies cannot drift apart again (the
# copy in preflight had lost the IPv4-mapped form). Kept here because this module is
# stdlib-only and importable without dragging server.py in.
LOCAL_PEER_HOSTS = frozenset({"127.0.0.1", "::1", "::ffff:127.0.0.1", "localhost", ""})

# The localhost-only token bootstrap endpoint on the MCP HTTP port.
WS_TOKEN_ENDPOINT_PATH = "/ws-token"

# Never reachable from another machine, whatever the bind and whatever token is offered.
LOOPBACK_ONLY_PATHS = frozenset({WS_TOKEN_ENDPOINT_PATH})
LOOPBACK_ONLY_PREFIXES = ("/mcp",)

ALLOW = "allow"
DENY_LOOPBACK_ONLY = "loopback_only"
DENY_TOKEN = "token_required"


def _normalize(path):
    """'/health/' and '/health' are the same endpoint; '' is '/'."""
    p = (path or "").strip()
    if len(p) > 1:
        p = p.rstrip("/")
    return p or "/"


def is_local_peer(peer_host):
    return (peer_host or "") in LOCAL_PEER_HOSTS


def is_loopback_only(path):
    """Does this path stay on 127.0.0.1 regardless of the LAN setting?

    Prefix-matched for `/mcp` so `/mcp`, `/mcp/`, and any session sub-path are all covered:
    a gate that only knew the exact string would leave the sub-paths open, and those are the
    ones carrying the actual tool calls.
    """
    p = _normalize(path)
    if p in LOOPBACK_ONLY_PATHS:
        return True
    return any(p == pre or p.startswith(pre + "/") for pre in LOOPBACK_ONLY_PREFIXES)


def authorize(path, peer_host, signed):
    """Decide before any endpoint runs. Returns (decision, status, message).

    `signed` is True when the request carried a valid `X-BrowserPlug-Auth` signature
    (shared-py/browserplug_shared/request_signing.py), which proves a device token without sending it. The raw
    `X-BrowserPlug-Token` header that extensions before 1.9 sent is no longer honoured: it
    put the token on the network in the clear.

    A local peer is allowed unconditionally: the launcher polls /health with no token, and
    an extension bootstrapping its very first token has none to offer. Endpoints that want
    a token from local callers too (/ws-bind, /reset-config, whose risk is a web page
    POSTing to localhost) still enforce that themselves — this gate is about the peer, not
    about what an individual endpoint additionally demands.
    """
    if is_local_peer(peer_host):
        return ALLOW, 200, None

    if is_loopback_only(path):
        return (DENY_LOOPBACK_ONLY, 403,
                "this endpoint is only reachable from the machine running the server")

    if not signed:
        return (DENY_TOKEN, 403,
                "a valid connection token is required from another machine "
                "(sign the request with it in the X-BrowserPlug-Auth header)")

    return ALLOW, 200, None




async def refuse(send, status, message):
    """The one refusal shape every denied request gets."""
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"),
                            (b"cache-control", b"no-store")]})
    await send({"type": "http.response.body",
                "body": json.dumps({"error": message}).encode("utf-8")})


def mcp_bind_host(ws_host, default_host):
    """Where uvicorn should listen.

    The MCP HTTP port follows the WebSocket's bind so one setting governs both, which is
    what "Configure local server for incoming connections" claims to do. A localhost WS
    leaves this port exactly where it was.
    """
    if (ws_host or "").strip() in ("0.0.0.0", "::"):
        return ws_host.strip()
    return default_host
