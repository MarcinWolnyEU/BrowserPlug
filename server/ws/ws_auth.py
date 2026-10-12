"""The extension WebSocket's front door: bind policy, who must authenticate, and how.

Owns the bind-address helpers (`ws_bind_addresses`, `ws_bind_refusal`), the hello window,
the per-peer authentication mode, the rejection of a socket that fails it, and the upgrade
to the encrypted channel (ws_crypto). Registering the socket as a browser afterwards is
browser_registry.py's job; the connection loop that calls both stays in server.py.

`WsAuth` reads the token, the "explicitly configured" bit, the hello window and the HTTP
port through callables, resolved on every use: tests and `set_ws_host` change those on the
server after construction. Nothing here imports server.py, and importing this module has
no side effects.
"""

import asyncio
import json
import logging
import os
from typing import Callable, Optional

from browserplug_shared import device_tokens
from browserplug_shared.ws_token import WS_HOST_ENV, WS_TOKEN_ENV

from server.ws import connection_log, http_auth, ws_crypto

logger = logging.getLogger(__name__)

# --- WebSocket bind address + token auth ------------------------------------
# WS_HOST_ENV / WS_TOKEN_ENV / WS_TOKEN_FILE_ENV and the token's resolution +
# persistence live in ws_token.py.
#
# A browser WebSocket cannot set request headers, so the token rides in the first
# extension_hello. This is how long a connection has to produce one.
#
# It was 5 s until 1.8.8. A Firefox whose host had run out of RAM opened a socket twice
# and missed that window twice, eight minutes apart, while a cold start passed the same
# handshake in 3 s: the background page was too starved to read storage, fetch the token
# and answer inside five seconds. Five seconds buys nothing - an extension that is going
# to answer answers in milliseconds - and it costs the one case where the browser is in
# trouble and reconnecting is the remedy.
WS_AUTH_TIMEOUT_SECONDS = 15.0
WS_AUTH_TIMEOUT_ENV = "BROWSERPLUG_WS_AUTH_TIMEOUT"
# Clamped rather than trusted: below ~2 s the window is shorter than a loaded browser's
# storage read, and above a minute a socket that will never identify itself sits in the
# pre-auth path holding frames.
WS_AUTH_TIMEOUT_MIN = 2.0
WS_AUTH_TIMEOUT_MAX = 60.0
# Frames a not-yet-authenticated peer may send before its hello (see
# WsAuth.authenticate): held, never processed. A cap keeps a hostile peer from
# growing that buffer without ever authenticating.
WS_PREAUTH_FRAME_LIMIT = 16
# A hand-set token shorter than this (128 bits of URL-safe base64) draws a startup warning.
WEAK_TOKEN_CHARS = 22
# Why an extension older than 1.9 is turned away. Deliberately names no token endpoint:
# the old extension would fetch a token only to send it in the clear again.
PLAINTEXT_REFUSED_REASON = ("encryption required: update the BrowserPlug extension to 1.9 "
                            "or later")
# Bind addresses that stay on this machine. Everything else (a LAN IP, a hostname,
# and 0.0.0.0/:: (all interfaces)) is reachable from the network.
LOCALHOST_BIND_HOSTS = {"localhost", "127.0.0.1", "::1", "localhost6"}

# Close code for an unauthenticated extension socket (private-use range). The close
# REASON tells a local extension where to fetch the token; a WebSocket close reason is
# capped at 123 BYTES by the protocol, and websockets raises if it is longer.
WS_CLOSE_UNAUTHORIZED = 4401
WS_CLOSE_REASON_MAX_BYTES = 123
# What the server's _close_extension_connection sends (websockets' default): the server
# hung up on purpose. Recorded on the browser's record so a later reader can tell "we closed this"
# from "the browser vanished", which is the whole distinction a close code exists for.
WS_CLOSE_NORMAL = 1000
# Path of the localhost-only bootstrap endpoint (served on the MCP HTTP app).
WS_TOKEN_ENDPOINT_PATH = http_auth.WS_TOKEN_ENDPOINT_PATH

# How a given peer is authenticated (see WsAuth.auth_mode).
AUTH_STRICT = "strict"        # a valid token or the socket is closed. No exceptions.
AUTH_BOOTSTRAP = "bootstrap"  # enforced, but the peer can fetch the token from /ws-token
AUTH_OFF = "off"              # no check at all: the pre-token behavior


def is_localhost_bind(host):
    """True if binding `host` keeps the WebSocket unreachable from other machines."""
    return (host or "").strip().lower() in LOCALHOST_BIND_HOSTS


def ws_bind_addresses(ws_host):
    """The address(es) to actually bind for `ws_host`: a wildcard covers BOTH families.

    `0.0.0.0` is the IPv4 wildcard only, so a LAN/WAN bind would leave `::1` with
    nothing listening. Every modern browser resolves `localhost` to `::1` FIRST, tries
    it, and logs `ERR_CONNECTION_REFUSED` before Happy Eyeballs falls back to
    `127.0.0.1`; the extension connects, but the reload is never error-free, and the
    error names a failure that is not real. A named host (`localhost`) never had the
    problem: getaddrinfo returns both families and asyncio binds each. So expand a
    wildcard to its sibling rather than making everyone read past a phantom error.

    Returns a single host or a list; both are accepted by loop.create_server. Callers
    keep reporting `ws_host` verbatim (/health, the LAN warning, --ws-host on restart);
    this widens the LISTENER, not the configuration.
    """
    host = (ws_host or "").strip()
    if host == "0.0.0.0":
        return ["0.0.0.0", "::"]
    if host == "::":
        return ["::", "0.0.0.0"]
    return ws_host


def ws_bind_refusal(ws_host, token):
    """Why binding the extension WebSocket to `ws_host` must be refused, or None.

    Fail-safe. That socket is *full control of a logged-in browser*: a non-localhost
    bind hands it to every machine that can reach the port, so without a shared token
    the server refuses to start rather than come up wide open.

    Since the server now MINTS a token when none is configured (ws_token.resolve_token),
    a tokenless server should be unreachable; this is the invariant that says so out
    loud, and the last line of defense if a future change ever breaks it.
    """
    if token or is_localhost_bind(ws_host):
        return None
    return (
        f"Refusing to bind the extension WebSocket to {ws_host!r}: that address is "
        f"reachable from other machines and no {WS_TOKEN_ENV} is set, so anyone who can "
        f"reach the port could drive your logged-in browser. Generate a token with "
        f"`python gen_ws_token.py`, set {WS_TOKEN_ENV} to it for the server, and paste "
        f"the same value into the extension's options page (Server connection → "
        f"Connection token). To keep the safe default instead, leave {WS_HOST_ENV} unset "
        f"(localhost-only) and tunnel (SSH/Tailscale) if you need a remote browser."
    )


def ws_auth_timeout_seconds(environ=None) -> float:
    """The hello window, from WS_AUTH_TIMEOUT_ENV, clamped to the sane range.

    Never raises and never refuses to start: a typo in an env var must not be the reason
    a browser cannot connect, so garbage reads as the default and an out-of-range value
    reads as the nearest bound, both with a warning.
    """
    raw = (os.environ if environ is None else environ).get(WS_AUTH_TIMEOUT_ENV)
    if raw is None or str(raw).strip() == "":
        return WS_AUTH_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logger.warning("%s=%r is not a number - using the default %.0fs",
                       WS_AUTH_TIMEOUT_ENV, raw, WS_AUTH_TIMEOUT_SECONDS)
        return WS_AUTH_TIMEOUT_SECONDS
    clamped = min(max(value, WS_AUTH_TIMEOUT_MIN), WS_AUTH_TIMEOUT_MAX)
    if clamped != value:
        logger.warning("%s=%r is outside %.0f-%.0fs - using %.0fs", WS_AUTH_TIMEOUT_ENV,
                       raw, WS_AUTH_TIMEOUT_MIN, WS_AUTH_TIMEOUT_MAX, clamped)
    return clamped


def peer_of(websocket):
    """(host, is_remote) of a WebSocket peer; (None, False) when it can't be read."""
    try:
        host = websocket.remote_address[0]
    except Exception:
        return None, False
    if not isinstance(host, str):
        return None, False
    return host, not http_auth.is_local_peer(host)


class WsAuth:
    def __init__(self, *, token: Callable[[], str], token_explicit: Callable[[], bool],
                 auth_timeout: Callable[[], float],
                 http_port: Callable[[], Optional[int]]):
        """`http_port` answers the MCP HTTP port that serves /ws-token, or None when this
        server runs without an HTTP app (`--no-mcp`, `--stdio`)."""
        self._token = token
        self._token_explicit = token_explicit
        self._auth_timeout = auth_timeout
        self._http_port = http_port

    def token_endpoint_url(self):
        """URL of the localhost-only token endpoint, or None when there is no HTTP app.

        This is the BOOTSTRAP CHANNEL. It exists only when the MCP HTTP listener runs
        (`--no-mcp` and `--stdio` have none), and only a process on this machine can
        reach it, which is precisely the security limit: a token handed to any
        unauthenticated peer is not a secret, so only same-machine browsers may
        self-configure. A remote one must be told the token out of band.
        """
        port = self._http_port()
        if port is None:
            return None
        return f"http://127.0.0.1:{port}{WS_TOKEN_ENDPOINT_PATH}"

    def auth_mode(self, peer_local: bool) -> str:
        """How this peer must authenticate. The whole enforcement rule, in one place.

        A token now always exists (auto-minted if not configured), so "is auth on?" is no
        longer the same question as "is a token set?". What decides it is whether the peer
        can possibly OBTAIN the token:

        * REMOTE peer -> AUTH_STRICT, always, no exceptions. It reached a LAN-bound socket
          that is full control of a logged-in browser; the token is the only thing between
          it and that browser, and it cannot read our /ws-token (localhost-only) either.
        * LOCAL peer, token explicitly configured (the BROWSERPLUG_WS_TOKEN variable or the
          `ws_token` constructor argument; there is no command-line flag) -> AUTH_STRICT. The
          user asked for auth; honor it for everyone, and the local extension self-heals by
          pulling the same token from /ws-token.
        * LOCAL peer, auto-minted token, bootstrap channel up -> AUTH_BOOTSTRAP: enforced,
          but a rejection names /ws-token so the extension configures itself and retries.
          No peer is let in without a token here: the grace for an extension too old to
          bootstrap ended in 1.9 (see authenticate).
        * LOCAL peer, auto-minted token, NO bootstrap channel (--no-mcp / --stdio) ->
          AUTH_OFF: nothing on this machine could learn the token, so demanding it would
          lock the user out of their own browser. Degrade to the pre-token behavior.
        """
        if not peer_local:
            return AUTH_STRICT
        if self._token_explicit():
            return AUTH_STRICT
        if self.token_endpoint_url():
            return AUTH_BOOTSTRAP
        return AUTH_OFF

    def auth_close_reason(self, peer_local: bool) -> str:
        """What a rejected peer is told: the ONLY hint it gets about how to recover.

        For a local peer that is the token endpoint's URL, carrying the server's REAL MCP
        port: the extension cannot guess it (tests and multi-server setups use dynamic
        ports), and this is the one place it is on the wire before authentication. Capped
        to the protocol's 123-byte close-reason limit.
        """
        url = self.token_endpoint_url() if peer_local else None
        reason = (f"token required: GET {url}" if url
                  else "token required: paste a device token made for this browser into the extension options")
        raw = reason.encode("utf-8")[:WS_CLOSE_REASON_MAX_BYTES]
        return raw.decode("utf-8", errors="ignore")

    async def reject(self, websocket, peer, why: str, peer_local: bool = True,
                                close_reason: Optional[str] = None, hint: str = ""):
        """Close an unauthenticated socket. It never entered the registry, so there is
        nothing to tear down; just say who was turned away (never WHAT they sent: that
        would put a guessed token in the log).

        `close_reason` overrides the token text for a rejection that is not about the
        token. The extension reads the reason, so a hello timeout answered with "token
        required" sends a struggling browser off to refetch a token it already has.
        """
        if not hint:
            hint = (f" (the extension WebSocket requires a token; a local browser can "
                    f"fetch it from {self.token_endpoint_url() or 'the server'})")
        logger.warning(f"Rejected extension connection from {peer}: {why}{hint}")
        try:
            await websocket.close(code=WS_CLOSE_UNAUTHORIZED,
                                  reason=close_reason or self.auth_close_reason(peer_local))
        except Exception as e:
            logger.debug(f"Error closing an unauthorized extension socket: {e}")

    async def reject_on_hello_timeout(self, websocket, peer, peer_local: bool):
        """The socket opened and then said nothing. Distinct from a token rejection in
        both channels: the log names the pressure that causes it and the knob that
        answers it, and the close reason carries the `hello timeout:` prefix the
        extension keys on."""
        await self.reject(
            websocket, peer,
            f"no extension_hello within {self._auth_timeout():g}s",
            peer_local,
            close_reason=connection_log.hello_timeout_close_reason(
                self._auth_timeout(), WS_CLOSE_REASON_MAX_BYTES),
            hint=(f" — the socket opened but the extension never identified itself. A "
                  f"browser under memory or CPU pressure can miss this window; raise it "
                  f"with {WS_AUTH_TIMEOUT_ENV} (seconds, "
                  f"{WS_AUTH_TIMEOUT_MIN:g}-{WS_AUTH_TIMEOUT_MAX:g})"))

    def device_secret(self, label):
        """The device secret for a device token's label (shared-py/browserplug_shared/device_tokens.py), or
        None. Every browser proves its own; the master token never leaves the server."""
        return device_tokens.secret_for(self._token(), label)

    def issue_device_token(self) -> str:
        return device_tokens.issue(self._token())

    async def upgrade_socket(self, websocket, request, peer, peer_local: bool,
                              deadline: float):
        """Run the encrypted handshake after the extension's request frame.

        Returns the SecureSocket, or None when the socket has been closed: 4401 with the
        usual `token required: ...` reason when the extension's proof does not match a
        device token of ours (the extension handles that exactly like a wrong token in a
        hello), the `hello timeout:` reason when it stalls, 4400 for anything malformed.
        """
        loop = asyncio.get_event_loop()
        try:
            return await ws_crypto.accept(websocket, request, self.device_secret,
                                          timeout=max(0.0, deadline - loop.time()))
        except asyncio.TimeoutError:
            await self.reject_on_hello_timeout(websocket, peer, peer_local)
        except ws_crypto.HandshakeError as e:
            if e.token_mismatch:
                await self.reject(
                    websocket, peer, "the encrypted handshake proved a different token",
                    peer_local)
            else:
                logger.warning(f"Rejected extension connection from {peer}: {e.reason}")
                try:
                    await websocket.close(code=ws_crypto.CLOSE_PROTOCOL, reason=e.reason)
                except Exception:
                    pass
        except Exception as e:
            logger.info(f"Extension at {peer} disconnected during the handshake: {e}")
        return None

    async def authenticate(self, websocket, mode: str, peer_local: bool):
        """Gate a new socket on the shared token. Never called in AUTH_OFF mode, where the
        path below is exactly what it was before tokens existed.

        An extension that holds the token opens with a binary request frame and runs the
        encrypted handshake (ws_crypto), which proves the token without sending it; its
        hello then arrives as the first sealed record and needs no token of its own. One
        with no token yet sends a plaintext `extension_hello` with `tokenBootstrap: true`
        and is turned away towards /ws-token. An extension from before 1.9 still puts the
        token in the plaintext hello's `data.token`; since 1.9.1 that is refused too, for
        every peer (see the branch below), so no cleartext door remains. Nothing from this
        peer is acted on, and it never enters the registry, until the token has been
        proved.

        The hello is not necessarily the first frame: background.js fires its health probe
        synchronously in onopen while sendExtensionHello() is still awaiting storage, so a
        `server.health` request usually overtakes it. Pre-auth frames are therefore HELD
        (bounded, never answered) and replayed once the peer authenticates, so a genuine
        extension doesn't lose its first probe just because a token is configured.

        Returns (ok, frames_to_replay, socket). ok=False means the socket is already
        closed. `socket` is what the connection must use from here on: the SecureSocket
        after a handshake, else the raw one.
        """
        peer = getattr(websocket, "remote_address", None)
        loop = asyncio.get_event_loop()
        deadline = loop.time() + self._auth_timeout()
        held = []
        encrypted = False
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                await self.reject_on_hello_timeout(websocket, peer, peer_local)
                return False, [], websocket
            try:
                raw = await asyncio.wait_for(websocket.recv(), timeout=remaining)
            except asyncio.TimeoutError:
                await self.reject_on_hello_timeout(websocket, peer, peer_local)
                return False, [], websocket
            except Exception as e:
                logger.info(f"Extension at {peer} disconnected before authenticating: {e}")
                return False, [], websocket

            if not encrypted and not held and ws_crypto.is_request(raw):
                secure = await self.upgrade_socket(websocket, raw, peer, peer_local,
                                                    deadline)
                if secure is None:
                    return False, [], websocket
                websocket, encrypted = secure, True
                continue

            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, TypeError, ValueError):
                data = None

            if isinstance(data, dict) and data.get('type') == 'extension_hello':
                if encrypted:
                    logger.info(f"Extension at {peer} authenticated (encrypted, device "
                                f"{websocket.device_label})")
                    # The hello goes FIRST on replay: it is what names the browser.
                    return True, [raw] + held, websocket

                hello = data.get('data') if isinstance(data.get('data'), dict) else {}
                if 'token' in hello:
                    # An extension older than 1.9 puts its token in a plaintext hello.
                    # Refused whatever the token: it has crossed the network in the clear
                    # already, and so would every page after it. The reason names no
                    # endpoint on purpose, so the old extension backs off instead of
                    # fetching a token it would only send in the clear again.
                    await self.reject(
                        websocket, peer, "plaintext token (an extension older than 1.9)",
                        peer_local, close_reason=PLAINTEXT_REFUSED_REASON,
                        hint=" — update the extension; 1.9 and later encrypt the connection")
                    return False, [], websocket

                # An extension too old to bootstrap a token was once let in with none at
                # all. That grace ended in 1.9: next to the encrypted handshake it would
                # be the one door that skips both the token and the encryption.
                await self.reject(
                    websocket, peer, "extension_hello carried no token", peer_local)
                return False, [], websocket

            if len(held) >= WS_PREAUTH_FRAME_LIMIT:
                await self.reject(
                    websocket, peer,
                    f"more than {WS_PREAUTH_FRAME_LIMIT} frames before authenticating",
                    peer_local)
                return False, [], websocket
            held.append(raw)
