"""The stdio relay's carriage of MCP 2026-07-28.

A stdio client has no HTTP headers, and the server's streamable HTTP entry routes a
2026-07-28 request on them. This module is what the relay adds so that such a client is
served: it recognises a modern frame, gives it the routing headers the SDK requires, sends
it on a connection of its own, ends that connection when the client cancels, and answers
`subscriptions/listen` itself. stdio_relay.py holds the hooks, and everything here is a
function or a class it calls. Standard library and browserplug_shared only, as for the
rest of the launcher path: this process is one of several on the box per MCP client, and
tests/unit/test_relay_era.py (TestWeight) fails when the import pulls in anything heavy.

Which frames are modern. A request or notification whose `params._meta` holds
`io.modelcontextprotocol/protocolVersion` and whose method is not `initialize`, the SDK's
own rule (mcp.server.runner). The relay decides per frame and never locks an era: a client
that probes `server/discover`, is refused and falls back to `initialize` simply stops
sending modern frames. Anything else goes the way it always went.

What a modern request carries (rules of mcp.shared.inbound, which rejects a mismatch with
-32020 and which Claude Code treats as a hard error after connect):

  MCP-Protocol-Version   the envelope's version, byte for byte
  Mcp-Method             the method
  Mcp-Name               params.name for tools/call and prompts/get, params.uri for
                         resources/read; text that is not plain printable ASCII travels
                         as =?base64?...?= (encode_header_value). A name that cannot
                         travel (an unpaired surrogate, or longer than MAX_NAME_HEADER
                         once encoded) is refused with HeaderRefused, which the relay
                         answers under the request's id: sent without the header it would
                         be rejected for a mismatch the client did not cause, and an
                         oversized one makes the server reset the connection, which the
                         relay would take for a lost server.
  X-BrowserPlug-Client-Pid, X-BrowserPlug-Connection-Id
                         who this is: the pid of the client that spawned the launcher, and
                         one random id per relay, which is what the server files the
                         connection under now that no Mcp-Session-Id exists

No Mcp-Session-Id is ever sent with such a request, and no Mcp-Param-* header: those exist
only for a tool schema that carries `x-mcp-header`, and tests/unit/test_tools_cross_era.py
pins that none does. A 2026-07-28 request gets its own connection (http.client, not urllib)
because cancelling it means shutting that connection down, and the SDK defers its response
headers until the handler has something to say (up to 15 s), so the socket has to be known
before they arrive.

Cancel. The 2026-07-28 wire has no client-to-server notification over HTTP: ending the
request IS the cancellation. A `notifications/cancelled` for a request in flight shuts its
socket down and nothing is written back for that id. One the relay holds nothing for is
dropped when the client has no handshake-era session, which is also what the SDK's own
client does with a late cancel.

Listen. The relay answers `subscriptions/listen` itself: the acknowledgment at once, then
`notifications/tools/list_changed` stamped with the subscription id whenever the /health
watcher sees server_pid or tools_signature move. A stream forwarded to the server would pin
one of the control workers for as long as the client lives, run into the control deadline,
and end at every server /restart, after which Claude Code parks re-listening for hours.
The relay outlives the server, so its stream does too.
"""

import base64
import http.client
import json
import os
import re
import socket
import threading
import time

from browserplug_shared import mcp_era
from browserplug_shared.version import CLIENT_PID_HEADER

ENVELOPE_VERSION_KEY = "io.modelcontextprotocol/protocolVersion"
SUBSCRIPTION_ID_KEY = "io.modelcontextprotocol/subscriptionId"

PROTOCOL_HEADER = "MCP-Protocol-Version"
METHOD_HEADER = "Mcp-Method"
NAME_HEADER = "Mcp-Name"
CONNECTION_ID_HEADER = mcp_era.CONNECTION_ID_HEADER

# Method -> the params key whose value is mirrored as Mcp-Name (inbound.NAME_BEARING_METHODS).
NAME_PARAM = {"tools/call": "name", "prompts/get": "name", "resources/read": "uri"}

MCP_PATH = "/mcp"
UNSUPPORTED_PROTOCOL_VERSION = -32022
# The first revision that carries the per-request envelope. Revisions are ISO dates, so a
# string comparison orders them.
FIRST_MODERN_VERSION = "2026-07-28"
JSONRPC_INVALID_PARAMS = -32602
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_INTERNAL_ERROR = -32603

# A client opens one listen stream per connection. The SDK's server allows 1024; a relay
# serves one client, so the cap only stops a runaway loop.
MAX_SUBSCRIPTIONS = 16

# The longest Mcp-Name value (after encoding) the relay will send. The server resets a
# connection whose header section passes about 64 KB, with no response; the usual HTTP
# server limit is 8 KB per header, and no tool name or resource URI comes near it.
MAX_NAME_HEADER = 8192

# A socket timeout set to the time left and the clock that measures the deadline can disagree
# by a clock tick (15.6 ms on Windows), so a timeout this close to the deadline is the deadline.
_CLOCK_SLACK = 0.1

_HEADER_SAFE = re.compile(r"[\x20-\x7E]*")
_BASE64_SENTINEL = re.compile(r"=\?base64\?.*\?=")
_LIST_CHANGED_FLAGS = ("toolsListChanged", "promptsListChanged", "resourcesListChanged")


class Cancelled(Exception):
    """The client withdrew this request: nothing is sent back for its id."""


class DeadlineCut(Exception):
    """The request's overall deadline passed, and the socket was shut down to end it."""


class HeaderRefused(ValueError):
    """The request cannot be sent: a value it must mirror into a header cannot travel there.
    `str(e)` is the text for the client."""


# ---- the frames ------------------------------------------------------------------------

def encode_header_value(value):
    """`value` as an HTTP header value, the way mcp.shared.inbound.encode_header_value does.

    Plain printable ASCII with no edge whitespace goes as it is. Anything else (non-ASCII,
    control characters, edge whitespace, or text that already looks like the sentinel) is
    base64 of its UTF-8 inside =?base64?...?=, so the server can recover the exact value.
    """
    if (_HEADER_SAFE.fullmatch(value) and value == value.strip()
            and not _BASE64_SENTINEL.fullmatch(value)):
        return value
    return "=?base64?{}?=".format(base64.b64encode(value.encode("utf-8")).decode("ascii"))


def _header_text(value):
    """`value` when it can travel in a header unchanged: a string of printable ASCII with no
    edge whitespace, which is the only kind the server can compare for equality."""
    if (isinstance(value, str) and value and _HEADER_SAFE.fullmatch(value)
            and value == value.strip()):
        return value
    return None


def modern_version(message):
    """The protocol version a 2026-07-28 frame names, or None for any other frame.

    A frame that names a version it cannot carry as a header (not a string, or not plain
    ASCII) is not treated as modern: it goes out as a handshake-era frame would, and the
    server's gate answers it.
    """
    method = _header_text(message.get("method"))
    if method is None or method == "initialize":
        return None
    params = message.get("params")
    meta = params.get("_meta") if isinstance(params, dict) else None
    if not isinstance(meta, dict):
        return None
    return _header_text(meta.get(ENVELOPE_VERSION_KEY))


def request_id_key(value):
    """The key a JSON-RPC id is tracked under, or None when it cannot be tracked.

    A numeric string and the number are one id, as in the SDK (coerce_request_id), so a
    cancel that echoes the id as text still finds its request. Booleans and containers are
    not ids.
    """
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            pass
    return value


def new_connection_id():
    """One random id for one relay: 32 lowercase hex characters (mcp_era.is_connection_id).
    os.urandom, not uuid: importing uuid loads a DLL that costs a megabyte of commit."""
    return os.urandom(16).hex()


def request_headers(message, version, client_pid, connection_id):
    """The headers a 2026-07-28 request goes out with. No Mcp-Session-Id, by design."""
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        CLIENT_PID_HEADER: str(client_pid),
        CONNECTION_ID_HEADER: connection_id,
        PROTOCOL_HEADER: version,
        METHOD_HEADER: message["method"],
    }
    key = NAME_PARAM.get(message["method"])
    params = message.get("params")
    name = params.get(key) if key and isinstance(params, dict) else None
    if isinstance(name, str):
        try:
            value = encode_header_value(name)
        except UnicodeEncodeError:
            raise HeaderRefused(
                "{}: params.{} is not valid text (it holds an unpaired surrogate), so the "
                "request was not sent".format(message["method"], key)) from None
        if len(value) > MAX_NAME_HEADER:
            raise HeaderRefused(
                "{}: params.{} is too long to send ({} bytes once encoded for its Mcp-Name "
                "header; the limit is {}), so the request was not sent".format(
                    message["method"], key, len(value), MAX_NAME_HEADER))
        headers[NAME_HEADER] = value
    return headers


def _error_of(raw):
    """The `error` object of a JSON-RPC reply frame, or None."""
    try:
        error = json.loads(raw).get("error")
    except Exception:
        return None
    return error if isinstance(error, dict) else None


def _short(value):
    text = repr(value)
    return text if len(text) <= 80 else text[:77] + "..."


def _lists_modern(data):
    """Whether a -32022 reply's `data.supported` names a 2026-07-28 or later revision."""
    supported = data.get("supported") if isinstance(data, dict) else None
    return isinstance(supported, list) and any(
        isinstance(v, str) and v >= FIRST_MODERN_VERSION for v in supported)


# ---- requests in flight ----------------------------------------------------------------

class Exchange:
    """One 2026-07-28 request between the client and the server: the socket it is using,
    and whether the client has withdrawn it."""

    def __init__(self):
        self._lock = threading.Lock()
        self._sock = None
        self.cancelled = False

    def attach(self, sock):
        with self._lock:
            if self.cancelled:
                raise Cancelled()
            self._sock = sock

    def detach(self):
        """Called before the socket is closed, so a late abort cannot reach a handle that
        has been reused."""
        with self._lock:
            self._sock = None

    def abort(self):
        with self._lock:
            self.cancelled = True
            sock = self._sock
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass


class Exchanges:
    """The modern requests the relay has taken from stdin and not yet answered, by id.

    A request is entered in stdin order, by the reader, before a worker has it, and the
    worker is handed the Exchange: a cancel that arrives while the request still waits for
    a worker finds it and marks it, and the worker then sends nothing."""

    def __init__(self):
        self._lock = threading.Lock()
        self._by_id = {}

    def begin(self, request_id):
        """A new Exchange for `request_id`. A client that reuses an id still in flight
        replaces the entry, so a cancel reaches the newest request. An id that cannot be
        tracked (a notification has none) gets an Exchange nothing can cancel."""
        exchange = Exchange()
        key = request_id_key(request_id)
        if key is not None:
            with self._lock:
                self._by_id[key] = exchange
        return exchange

    def end(self, request_id, exchange):
        key = request_id_key(request_id)
        if key is None:
            return
        with self._lock:
            if self._by_id.get(key) is exchange:
                del self._by_id[key]

    def cancel(self, request_id):
        """End the request named `request_id`. False when none is in flight."""
        key = request_id_key(request_id)
        with self._lock:
            exchange = self._by_id.get(key) if key is not None else None
        if exchange is None:
            return False
        exchange.abort()
        return True


class _DeadlineWatch:
    """Shuts a socket down when the request's overall deadline passes.

    A socket timeout restarts on every byte, and the server pings a quiet SSE stream every
    15 s, so only a timer that cuts the connection bounds a wedged call. Armed at connect,
    so it also covers the wait for response headers, which for a modern request lasts as
    long as the handler has nothing to say."""

    def __init__(self, sock, deadline):
        self.fired = threading.Event()
        self._lock = threading.Lock()
        self._sock = sock
        # The timer holds self._cut, so stop() must drop the timer: left in place the pair
        # is a reference cycle, and each request would keep a dead timer thread's kernel
        # handles until the next garbage collection.
        self._timer = threading.Timer(max(0.0, deadline - time.monotonic()), self._cut)
        self._timer.daemon = True
        self._timer.start()

    def _cut(self):
        with self._lock:
            if self._sock is None:
                return
            self.fired.set()
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def stop(self):
        timer, self._timer = self._timer, None
        if timer is not None:
            timer.cancel()
        with self._lock:
            self._sock = None


def post(host, port, body, headers, exchange, timeout, deadline):
    """POST one 2026-07-28 request on a connection of its own: (status, headers, text).

    An HTTP error status is a result, as in StdioRelay.post. Raises Cancelled when the
    client withdrew the request, DeadlineCut when `deadline` (a time.monotonic() value)
    passed, and the OSError or http.client error of a connection that failed otherwise,
    which the caller treats as a lost server.
    """
    if exchange.cancelled:
        raise Cancelled()
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    watch = response = None
    try:
        # A connection that cannot be made, even by timing out, is a lost server and the
        # caller recovers and replays, as it does for urllib in StdioRelay.post. Only a
        # request the server accepted can run out of its deadline.
        try:
            conn.connect()
        except Exception:
            if exchange.cancelled:
                raise Cancelled() from None
            raise
        exchange.attach(conn.sock)
        try:
            watch = _DeadlineWatch(conn.sock, deadline)
            conn.request("POST", MCP_PATH, body=body, headers=headers)
            response = conn.getresponse()
            text = response.read().decode("utf-8", "replace")
        except Exception as exc:
            if exchange.cancelled:
                raise Cancelled() from None
            if watch is not None and watch.fired.is_set():
                raise DeadlineCut() from None
            if isinstance(exc, TimeoutError) and time.monotonic() >= deadline - _CLOCK_SLACK:
                raise DeadlineCut() from None
            raise
        if exchange.cancelled:
            raise Cancelled()
        if watch.fired.is_set():
            raise DeadlineCut()     # cut mid-body: the read returned what it had
        return response.status, response.headers, text
    finally:
        if watch is not None:
            watch.stop()
        exchange.detach()
        # A response that will close takes the socket over from the connection, so closing
        # the connection alone leaves it open when the read was cut.
        if response is not None:
            response.close()
        conn.close()


# ---- subscriptions/listen --------------------------------------------------------------

class Subscriptions:
    """The listen streams the relay holds open for its client."""

    def __init__(self, emit, emit_error):
        self._emit = emit
        self._emit_error = emit_error
        # Held while a stream is entered and acknowledged, and while a notice goes out, so
        # nothing can precede the acknowledgment on a stream.
        self._lock = threading.Lock()
        self._open = {}

    def __len__(self):
        with self._lock:
            return len(self._open)

    def listen(self, message):
        """Open the stream a `subscriptions/listen` request asks for, and acknowledge it."""
        listen_id = message.get("id")
        key = request_id_key(listen_id)
        if listen_id is None:
            return      # a notification cannot be answered
        if key is None:
            self._emit_error(listen_id, "subscriptions/listen requires a string or number id",
                             JSONRPC_INVALID_REQUEST)
            return
        params = message.get("params")
        wanted = params.get("notifications") if isinstance(params, dict) else None
        if not isinstance(wanted, dict):
            self._emit_error(listen_id, "subscriptions/listen requires params.notifications",
                             JSONRPC_INVALID_PARAMS)
            return
        # What the SDK's ListenHandler honors: every requested kind, as true flags and a
        # non-empty URI list. Whether a kind ever fires is up to the server's surface.
        honored = {name: True for name in _LIST_CHANGED_FLAGS if wanted.get(name) is True}
        uris = wanted.get("resourceSubscriptions")
        if isinstance(uris, list) and uris and all(isinstance(u, str) for u in uris):
            honored["resourceSubscriptions"] = list(uris)
        with self._lock:
            if key not in self._open and len(self._open) >= MAX_SUBSCRIPTIONS:
                self._emit_error(listen_id, "Subscription limit reached",
                                 JSONRPC_INTERNAL_ERROR)
                return
            self._open[key] = (listen_id, honored)
            self._emit(json.dumps({
                "jsonrpc": "2.0",
                "method": "notifications/subscriptions/acknowledged",
                "params": {"_meta": {SUBSCRIPTION_ID_KEY: listen_id}, "notifications": honored},
            }))

    def close(self, request_id):
        """End the stream opened under `request_id`, with no reply. False when none is."""
        key = request_id_key(request_id)
        with self._lock:
            return key is not None and self._open.pop(key, None) is not None

    def tools_changed(self):
        """Tell every stream that asked for it that the tool list changed. Returns how many."""
        told = 0
        with self._lock:
            for listen_id, honored in self._open.values():
                if honored.get("toolsListChanged"):
                    self._emit(json.dumps({
                        "jsonrpc": "2.0",
                        "method": "notifications/tools/list_changed",
                        "params": {"_meta": {SUBSCRIPTION_ID_KEY: listen_id}},
                    }))
                    told += 1
        return told


# ---- what the relay holds --------------------------------------------------------------

class Carriage:
    """Everything the relay keeps for carrying 2026-07-28: the requests in flight, the listen
    streams, and this relay's identity on the wire.

    `enabled` is False for any era but `all`. A disabled carriage recognises nothing and
    intercepts nothing, so every frame takes the path it took before this module existed.
    """

    def __init__(self, era, emit, emit_error, log, client_pid, connection_id=None,
                 legacy_session=None):
        self.enabled = era is not None and mcp_era.relay_carries_modern(era)
        self.client_pid = client_pid
        self.connection_id = connection_id or new_connection_id()
        self.exchanges = Exchanges()
        self.subscriptions = Subscriptions(emit, emit_error)
        self._log = log
        # Whether the client has opened a handshake-era session on this relay.
        self._legacy_session = legacy_session or (lambda: False)
        self._served = False
        self._last_refusal = None

    def version_of(self, message):
        """The 2026-07-28 version `message` carries, or None (always None when disabled)."""
        return modern_version(message) if self.enabled else None

    def intercept(self, message):
        """Settle inline, on the reader's thread, what must not wait for a worker.

        True means the frame is consumed and goes no further."""
        if not self.enabled:
            return False
        method = message.get("method")
        if method == "subscriptions/listen":
            if modern_version(message) is None:
                return False
            self.subscriptions.listen(message)
            return True
        if method == "notifications/cancelled":
            return self._cancelled(message)
        return False

    def enter(self, message):
        """Enter a modern request in the table, on the reader's thread and in stdin order, so
        that a cancel read after it finds it. Returns its Exchange for the worker, or None for
        a frame that is not a modern request."""
        if not self.enabled or "id" not in message or modern_version(message) is None:
            return None
        return self.exchanges.begin(message["id"])

    def _cancelled(self, message):
        params = message.get("params")
        request_id = params.get("requestId") if isinstance(params, dict) else None
        if self.exchanges.cancel(request_id):
            self._log("cancelled request {!r}: ended its connection to the server".format(
                request_id))
            return True
        if self.subscriptions.close(request_id):
            self._log("closed listen stream {!r}".format(request_id))
            return True
        # A cancel the relay holds nothing for: a request that already answered, or one the
        # handshake-era session is still serving. The second goes on to the server as it
        # always did; the first would only draw an error from a server that has no session
        # to look it up in.
        return not self._legacy_session()

    def headers(self, message, version):
        return request_headers(message, version, self.client_pid, self.connection_id)

    def note_reply(self, status, frames):
        """Keep what a reply says about the server's era, and say so when it is a surprise."""
        if status < 400:
            self._served = True
            # A later refusal is news again, even one of the kind logged before.
            self._last_refusal = None
            return
        errors = (_error_of(raw) for raw in frames)
        refusal = next((e for e in errors
                        if e and e.get("code") == UNSUPPORTED_PROTOCOL_VERSION), None)
        if refusal is None:
            return
        data = refusal.get("data")
        # A -32022 that still lists 2026-07-28 is about the version this one request named
        # (a client newer than the server); only a list without it says the era moved.
        if _lists_modern(data):
            kind = "version"
            text = ("the server does not support the protocol version this client named "
                    "({}); it serves {}").format(
                        _short(data.get("requested")), _short(data.get("supported")))
        elif self._served:
            kind = "changed"
            text = ("the server stopped serving 2026-07-28 (-32022) after it had served "
                    "it: its mcp.era changed, and this client has to reconnect")
        else:
            kind = "fallback"
            text = ("the server does not serve 2026-07-28 (-32022); a client that "
                    "negotiates falls back to initialize")
        # Once per kind in a row: a client that repeats a request must not fill the log.
        if kind != self._last_refusal:
            self._last_refusal = kind
            self._log(text)

    def tools_changed(self):
        """Stamp a tools/list_changed onto every listen stream. Returns how many."""
        return self.subscriptions.tools_changed() if self.enabled else 0
