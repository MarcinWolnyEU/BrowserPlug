#!/usr/bin/env python3
"""The launcher's MCP transport, as a byte relay instead of an MCP-library proxy.

WHY THIS EXISTS: memory. Every MCP client on the box spawns its own launcher, and a
launcher that serves stdio through an MCP library pays for that library's import graph.
The proxy this relay replaced measured ~90 MB RSS per launcher: the interpreter floor is
18 MB and the rest was import graph, httpx +16 MB and `mcp` +23 MB (641 modules, including
uvicorn, watchfiles, starlette, jsonschema and python-multipart) among it, the whole
HTTP-SERVER stack paid for in order to serve stdio, which touches none of it. This module
speaks the same protocol over urllib and json and settles at ~28 MB, of which 18 MB is
CPython itself. The proxy survives as the opt-in transport (BROWSERPLUG_LAUNCHER_RELAY=0,
server/proxy/), in a child process of its own, so this module never imports it.

WHAT IT IS: a JSON-RPC frame forwarder. stdin carries newline-delimited JSON one way,
`POST /mcp` carries it the other, and the relay reads only the few fields it needs to run
the session (the method name, the id, whether an id is present at all). It does not model
tools, validate names against a cached tools/list, or parse arguments. Response frames go
out as the bytes the server sent, never re-serialized, so nothing this process does can
alter a payload.

Three things fall out of being dumb, and they are the point rather than a side effect:

  * No renamed-tool hook. The proxy transport needs one because it resolves a call against
    its own cached tool list and rejects an unknown name before it reaches the server. A
    relay forwards the name, so the server's own tools/call dispatch answers, with the
    identical renamed-tool text (server/tools/tool_renames.py).
  * No tool-list cache, so no TTL to shorten: a re-shaped tool surface is visible on the
    client's next tools/list.
  * Proxy-blindness is one ProxyHandler({}) opener, the same guard
    mcp_port.browserplug_health already uses: httpx reads HTTP_PROXY from the environment,
    and a corporate proxy with no NO_PROXY for localhost swallowed every loopback
    tools/call of the proxy until its client was built with trust_env=False.

WHAT IT IS NOT: a general MCP client. It carries the request/response direction only,
which is sound HERE because this server never initiates. server/ contains no
create_message, elicit, report_progress or list_roots, and the capabilities it advertises
on initialize are the three listChanged flags plus logging. The one server-to-client
notification the relay ever writes is tools/list_changed, and that one is synthesized
locally from the /health poll rather than carried up from the backend, exactly as the
proxy transport does. A handshake-era client gets it as an unsolicited notification. A
client on 2026-07-28 may not be sent one it did not ask for, so it gets it on the
`subscriptions/listen` stream the relay answers itself. A server that grows sampling or
elicitation needs the GET SSE stream opened here first; until then opening it would be
dead code holding a socket.

2026-07-28 (launcher/relay_era.py, only under mcp.era = all): the relay gives each modern
frame the routing headers the SDK requires, sends it on a connection of its own, ends that
connection to cancel it, and answers subscriptions/listen. Every other frame, and every
frame under any other era, goes out as it always did.
"""

import http.client
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request

from browserplug_shared.version import CLIENT_PID_HEADER
from launcher import relay_era
from launcher.client_pid import client_pid as spawning_client_pid

SESSION_HEADER = "Mcp-Session-Id"
PROTOCOL_HEADER = "MCP-Protocol-Version"

# Mirrors mcp.shared._httpx_utils's defaults by VALUE, not by import: reading them from
# `mcp` would pull the 23 MB this module exists to avoid. MCP_DEFAULT_TIMEOUT is 30.0 and
# MCP_DEFAULT_SSE_READ_TIMEOUT is 300.0; urllib has one socket timeout rather than two,
# and a tools/call arrives as a single SSE response, so the read budget is what governs.
REQUEST_TIMEOUT_SECONDS = 300.0
HEALTH_TIMEOUT_SECONDS = 2.0

# Same cadence the proxy path polled at, for the same two keys.
TOOLS_POLL_SECONDS = 3

JSONRPC_INTERNAL_ERROR = -32603
JSONRPC_PARSE_ERROR = -32700
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_REQUEST_TIMEOUT = -32001   # MCP's RequestTimeout

# Overall (monotonic) budget per request. REQUEST_TIMEOUT_SECONDS above is a per-READ
# timeout and sse-starlette pings every 15 s, so on a wedged call it never fires; these
# are what bound a request. Everything except tools/call (initialize, ping, tools/list)
# answers in milliseconds on a healthy server, and a client gives up on `initialize`
# after about 30 s and kills the launcher without any JSON-RPC error, so the error has to
# arrive first. A tools/call is bounded by the server's own per-action budgets (60 s read,
# 60 s screenshot, 45 s eval, page_act 20 s plus its wait steps, navigate's timeout_ms)
# plus queue wait and the one retry, so it gets a generous ceiling: this is a backstop for
# a wedged call, not a tighter timeout than the server's.
CONTROL_DEADLINE_SECONDS = 20.0
TOOL_CALL_DEADLINE_SECONDS = 900.0

# A tool call drives a browser, so several can legitimately be in flight while one waits
# on a slow page. Threads rather than asyncio: urllib blocks, and asyncio measured
# +3.2 MB for a loop whose only job would be to wrap blocking calls in an executor.
MAX_WORKERS = 32
# A second, small lane for everything that is not a tools/call (ping, initialize,
# tools/list, notifications/cancelled). Wedged tool calls can hold all of MAX_WORKERS, and
# a cancel or ping queued behind them is exactly the traffic that must not wait.
CONTROL_WORKERS = 4


class DeadlineExceeded(Exception):
    """The request's overall deadline passed before the server finished answering."""


class BackendLost(Exception):
    """The shared server could not be reached and retrying cannot answer the request.
    `str(e)` is the text for the client."""


# Recovery (L2). When the shared server dies or moves, the relay runs the launcher's
# seek-then-spawn again instead of failing every call until the client is reconnected by
# hand. One thread does it at a time, a failed attempt blocks the next one for a backoff
# that doubles up to the cap (a server that cannot start must not be respawned by every
# call of a busy client), and a request is re-sent at most once.
RECOVERY_BACKOFF_SECONDS = 2.0
RECOVERY_BACKOFF_MAX_SECONDS = 60.0
# Consecutive failed /health polls before the watcher recovers on its own. One is a slow
# poll; two in a row, 3 s apart, is a server that is not there.
WATCH_FAILURES_BEFORE_RECOVERY = 2

# Requests that can be sent again after a recovery: they only read, or (initialize) they
# start a session. A tools/call is never in this set, because it may already have run.
# server/discover is the 2026-07-28 opening exchange and, like initialize, changes nothing.
REPLAYABLE_METHODS = frozenset({"initialize", "ping", "tools/list", "resources/list",
                                "prompts/list", "resources/templates/list", "server/discover"})

# Tools whose every action only reads, so calling one again after a lost connection cannot
# double-apply anything. The tool-level view of the wire allowlist in request_retry.py;
# `tabs` and `requests` read or write depending on `action`.
READ_ONLY_TOOLS = frozenset({"page_read", "page_snapshot", "page_screenshot", "server_info"})
READ_ONLY_ACTIONS = {"tabs": frozenset({"list", "active", "find"}),
                     "requests": frozenset({"recent", "list", "get"})}


def _call_only_reads(params):
    """True when this tools/call's tool (and action) cannot change anything."""
    params = params if isinstance(params, dict) else {}
    name = params.get("name")
    if name in READ_ONLY_TOOLS:
        return True
    actions = READ_ONLY_ACTIONS.get(name)
    arguments = params.get("arguments")
    return bool(actions) and isinstance(arguments, dict) and arguments.get("action") in actions


def _is_connection_failure(exc):
    """A refused, reset or timed-out connection, as opposed to a deadline or a bug."""
    return isinstance(exc, (OSError, http.client.HTTPException))


def _was_refused(exc):
    """The TCP connect itself was refused: the request never reached any server."""
    return isinstance(getattr(exc, "reason", exc), ConnectionRefusedError)


_SSE_LINE_END = re.compile(r"\r\n|\r|\n")


def messages_from(headers, body_text):
    """The JSON frames in one backend response, as the raw strings the server sent.

    Streamable HTTP answers a POST either as application/json (one frame) or as
    text/event-stream (frames on `data:` lines). Returning strings rather than parsed
    objects is what lets the caller forward bytes it never re-encodes.
    """
    ctype = (headers.get("Content-Type") or "").lower()
    if "text/event-stream" in ctype:
        # SSE ends a line at CRLF, CR or LF only. str.splitlines() also cuts at U+2028,
        # U+2029, NEL and the C0 separators, which JSON leaves unescaped, and a cut
        # frame is a request the client never sees answered.
        return [line[5:].strip() for line in _SSE_LINE_END.split(body_text)
                if line.startswith("data:") and line[5:].strip()]
    text = body_text.strip()
    return [text] if text else []


_UNPARSED = object()


def _frame_id(raw):
    """A frame's JSON-RPC id (None when absent or null), or _UNPARSED."""
    try:
        frame = json.loads(raw)
    except Exception:
        return _UNPARSED
    return frame.get("id") if isinstance(frame, dict) else _UNPARSED


def _orphan(raw):
    """A frame the client cannot use once its request went unanswered: text that is not
    JSON-RPC (a plain-text 413 or 500), or a response whose id is missing or null. A
    server notification (it has a method) is not an orphan."""
    frame_id = _frame_id(raw)
    if frame_id is _UNPARSED:
        return True
    if frame_id is not None:
        return False
    return "method" not in json.loads(raw)


# What mcp's streamable HTTP transport says when the SESSION is the problem: 400 "Missing
# session ID", 404 "Invalid or expired session ID", 404 "Session not found". A 400 for a
# malformed frame ("Parse error: ...", a validation error) names none of these.
_SESSION_LOST = re.compile(r"missing session|invalid or expired session|session not found",
                           re.IGNORECASE)


def _session_lost(status, frames):
    """True when an error reply means the backend no longer knows this session.

    A 404 always does. A 400 does only when its JSON-RPC error text says so: mcp answers
    400 to one malformed frame as well, and rebuilding the session for that leaks the
    live session on the server and replays `initialize` for nothing.
    """
    if status == 404:
        return True
    if status != 400:
        return False
    for raw in frames:
        try:
            error = json.loads(raw).get("error")
        except Exception:
            continue
        if isinstance(error, dict) and _SESSION_LOST.search(str(error.get("message"))):
            return True
    return False


def _first_error(frames):
    """(code, message) of the first JSON-RPC error among `frames`, else the defaults."""
    for raw in frames:
        try:
            error = json.loads(raw).get("error")
        except Exception:
            continue
        if isinstance(error, dict) and isinstance(error.get("code"), int):
            return error["code"], error.get("message")
    return JSONRPC_INTERNAL_ERROR, None


class StdioRelay:
    """One client's session: stdin frames in, POST /mcp out, backend frames to stdout."""

    def __init__(self, port, log, identity_headers=None, host="127.0.0.1",
                 out=None, poll_seconds=TOOLS_POLL_SECONDS, client_pid=None,
                 recover=None, recover_detail=None, era=None, connection_id=None):
        self.host = host
        self.log = log
        # launcher.ensure_server: seek the server, spawn one if none answers, return its
        # port or None. Injected because the launcher imports this module, not the reverse.
        self.recover = recover
        self.recover_detail = recover_detail
        self.recovery_backoff_base = RECOVERY_BACKOFF_SECONDS
        self._recovery_lock = threading.Lock()
        self._recovery_epoch = 0
        self._recovery_failures = 0
        self._recovery_blocked_until = 0.0
        self.identity_headers = dict(identity_headers or {})
        # The client process that spawned this launcher. The server's TCP peer is this
        # launcher, so without the header every client is keyed by session id and a
        # reconnect loses its browser selection. Sent on every request, /health included.
        self.client_pid = client_pid if client_pid is not None else spawning_client_pid()
        self.poll_seconds = poll_seconds
        # Never route a loopback call through HTTP_PROXY; see the module docstring.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self._out = out if out is not None else sys.stdout.buffer
        self._out_lock = threading.Lock()
        # Guards _session_id and _session_gen, and serializes rebuilds. Re-entrant because
        # a rebuild holds it across its own initialize POST, which reads the session id.
        self._session_lock = threading.RLock()
        self.set_port(port)
        self._session_id = None
        # Bumped whenever the session is dropped or replaced. A call remembers the value
        # it went out under, so a late 404 for an OLD session can tell that someone
        # already rebuilt and must not wipe the new one.
        self._session_gen = 0
        self._protocol_version = None
        # The client's own initialize frame, kept so the session can be rebuilt without
        # it. The shared server outlives this launcher and /restart replaces it on the
        # SAME port, so a restart shows up here as one dead session id and nothing else.
        self._initialize_frame = None
        self._stopping = threading.Event()
        self.control_deadline_seconds = CONTROL_DEADLINE_SECONDS
        self.tool_deadline_seconds = TOOL_CALL_DEADLINE_SECONDS
        # The deadline of the request the current worker thread is serving. Thread-local
        # so post() and the session rebuild share one budget without a signature change.
        self._local = threading.local()
        # 2026-07-28 (relay_era.py), carried only when `era` is `all`: the requests in
        # flight, the listen streams the relay answers itself, and the id that names this
        # relay to the server. With any other era the carriage recognises no frame.
        self.era = era
        self.carriage = relay_era.Carriage(
            era, self.emit, self.emit_error, self.log, self.client_pid,
            connection_id=connection_id,
            legacy_session=lambda: self._initialize_frame is not None)
        self.connection_id = self.carriage.connection_id

    def set_port(self, port):
        """Point every URL at `port` (the server moved or was respawned)."""
        with self._session_lock:
            self.port = port
            self.mcp_url = "http://{}:{}/mcp".format(self.host, port)
            self.health_url = "http://{}:{}/health".format(self.host, port)
            self.timing_url = "http://{}:{}/timing".format(self.host, port)

    def recover_backend(self, epoch):
        """Find or respawn the shared server. True when a server answers afterwards.

        `epoch` is the recovery count the caller saw before its request failed: if it has
        moved, another thread recovered while this one waited for the lock, and the caller
        only needs to retry. A failure blocks further attempts for a backoff that doubles
        (recovery_backoff_base, 2x, 4x ... capped), so a server that cannot start is not
        respawned by every call.
        """
        if self.recover is None:
            return False
        with self._recovery_lock:
            if self._recovery_epoch != epoch:
                return True
            if time.monotonic() < self._recovery_blocked_until:
                return False
            try:
                port = self.recover()
            except Exception as e:
                self.log("recovery raised:", e)
                port = None
            if port is None:
                self._recovery_failures += 1
                backoff = min(self.recovery_backoff_base * 2 ** (self._recovery_failures - 1),
                              RECOVERY_BACKOFF_MAX_SECONDS)
                self._recovery_blocked_until = time.monotonic() + backoff
                self.log("could not find or start the shared server; next attempt in "
                         "{:g} s".format(backoff))
                return False
            self._recovery_failures = 0
            self._recovery_blocked_until = 0.0
            if port != self.port:
                self.log("shared server is now on :{} (was :{})".format(port, self.port))
            self.set_port(port)
            self._recovery_epoch += 1
            return True

    def _remaining(self, timeout):
        """`timeout` clipped to what is left of this thread's request deadline."""
        deadline = getattr(self._local, "deadline", None)
        if deadline is None:
            return timeout
        left = deadline - time.monotonic()
        if left <= 0:
            raise DeadlineExceeded()
        return min(timeout, left)

    # ---- stdout ------------------------------------------------------------------

    def emit(self, frame):
        """Write one JSON frame to stdout. Serialized: worker threads all land here."""
        data = frame if isinstance(frame, bytes) else frame.encode("utf-8")
        with self._out_lock:
            self._out.write(data + b"\n")
            self._out.flush()

    def emit_error(self, msg_id, message, code=JSONRPC_INTERNAL_ERROR):
        self.emit(json.dumps({"jsonrpc": "2.0", "id": msg_id,
                              "error": {"code": code, "message": message}}))

    # ---- backend HTTP ------------------------------------------------------------

    def post(self, body, timeout=REQUEST_TIMEOUT_SECONDS):
        """POST one frame. Returns (status, headers, text); an HTTP error is a RESULT.

        urllib raises on 4xx/5xx, but a 404 carrying "session not found" is the normal way
        a restarted server reports a dead session, and it has to reach the retry below as
        data rather than as an exception.
        """
        with self._session_lock:
            url = self.mcp_url
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json, text/event-stream")
        req.add_header(CLIENT_PID_HEADER, str(self.client_pid))
        # Blocks while a rebuild holds the lock, so a call never leaves with the id
        # cleared mid-rebuild (the server answers that -32600 "Missing session ID").
        with self._session_lock:
            session_id, version = self._session_id, self._protocol_version
        if session_id:
            req.add_header(SESSION_HEADER, session_id)
        if version:
            req.add_header(PROTOCOL_HEADER, version)
        timeout = self._remaining(timeout)
        deadline = getattr(self._local, "deadline", None) or time.monotonic() + timeout
        try:
            with self._opener.open(req, timeout=timeout) as r:
                return r.status, r.headers, self._read_bounded(r, deadline)
        except urllib.error.HTTPError as e:
            return e.code, e.headers, self._read_bounded(e, deadline)

    @staticmethod
    def _read_bounded(response, deadline):
        """Read a body, but give up at `deadline` however often the server pings.

        The socket timeout restarts on every byte, and a wedged call's SSE stream keeps
        sending pings, so only a watchdog that shuts the socket down bounds the read.
        """
        sock = None
        try:
            sock = response.fp.raw._sock
        except Exception:
            pass  # unreachable socket: fall back to the per-read timeout alone
        timer = None
        # Set BEFORE the shutdown: the read wakes on the shutdown while the timer thread
        # is still alive, so `timer.is_alive()` cannot tell "cut" from "finished".
        cut_done = threading.Event()
        if sock is not None:
            import socket

            def cut():
                cut_done.set()
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except Exception:
                    pass

            timer = threading.Timer(max(0.0, deadline - time.monotonic()), cut)
            timer.daemon = True
            timer.start()
        try:
            try:
                data = response.read()
            except Exception:
                if time.monotonic() >= deadline:
                    raise DeadlineExceeded()
                raise
            if cut_done.is_set():
                raise DeadlineExceeded()  # cut mid-body: the read returned what it had
        finally:
            if timer is not None:
                timer.cancel()
        return data.decode("utf-8", "replace")

    def remember_session(self, headers, frames):
        """Take the session id and negotiated protocol version off an initialize reply."""
        sid = headers.get(SESSION_HEADER)
        with self._session_lock:
            if sid:
                self._session_id = sid
                self._session_gen += 1
            for raw in frames:
                try:
                    version = json.loads(raw).get("result", {}).get("protocolVersion")
                except Exception:
                    continue
                if version:
                    self._protocol_version = version
                    break

    def drop_session(self):
        """Forget the session (the client is initializing afresh)."""
        with self._session_lock:
            self._session_id = None
            self._session_gen += 1
            self._protocol_version = None

    def post_modern(self, message, version, body, exchange):
        """POST one 2026-07-28 request. Returns (status, headers, text), like post().

        It carries no session: the request names its protocol version and capabilities
        itself, so there is nothing to rebuild when the server restarts, and a 404 here is
        METHOD_NOT_FOUND, never a lost session. Raises relay_era.Cancelled when the client
        withdrew the request.
        """
        with self._session_lock:
            host, port = self.host, self.port
        timeout = self._remaining(REQUEST_TIMEOUT_SECONDS)
        deadline = getattr(self._local, "deadline", None) or time.monotonic() + timeout
        try:
            return relay_era.post(host, port, body, self.carriage.headers(message, version),
                                  exchange, timeout, deadline)
        except relay_era.DeadlineCut:
            raise DeadlineExceeded() from None

    def reestablish(self, seen_gen=None):
        """Re-run the client's initialize against a server that lost our session.

        `seen_gen` is the session generation the failed call went out under. Under one
        lock, a call whose generation is stale (another thread already rebuilt, or the
        client re-initialized) just reports success so it retries on the new session:
        one lost session means one initialize replay. Returns True when there is a
        usable session afterwards.
        """
        with self._session_lock:
            if seen_gen is not None and self._session_gen != seen_gen:
                return self._session_id is not None
            if seen_gen is None and self._session_id is not None:
                return True  # another thread rebuilt it while this one waited
            if self._initialize_frame is None:
                return False  # nothing to replay: the client never initialized
            self._session_id = None
            self._session_gen += 1
            self._protocol_version = None
            status, headers, text = self.post(self._initialize_frame)
            if status >= 400:
                return False
            self.remember_session(headers, messages_from(headers, text))
            if self._session_id is None:
                return False
            # The server treats a session as unusable until this lands.
            self.post(json.dumps(
                {"jsonrpc": "2.0", "method": "notifications/initialized"}).encode())
            self.log("backend session was gone; re-initialized as "
                     "{}... (the shared server restarted)".format(self._session_id[:8]))
            return True

    def post_with_session(self, body):
        """post(), plus one transparent rebuild if the backend says the session is dead.

        This is what keeps a /restart invisible to the client, the property the proxy path
        got from its per-request backend sessions.
        """
        with self._session_lock:
            gen, had_session = self._session_gen, self._session_id is not None
        status, headers, text = self.post(body)
        if status in (400, 404) and _session_lost(status, messages_from(headers, text)):
            with self._session_lock:
                moved = self._session_gen != gen
            # `moved` covers a call that left while a rebuild was under way: it retries on
            # the new session instead of tearing that one down.
            if (had_session or moved) and self.reestablish(gen):
                status, headers, text = self.post(body)
        return status, headers, text

    def attempt(self, message, body):
        """One try at delivering `message`: a 2026-07-28 request goes out on a connection of
        its own, every other frame through the session."""
        modern = getattr(self._local, "modern", None)
        if modern is None:
            return self.post_with_session(body)
        version, exchange = modern
        return self.post_modern(message, version, body, exchange)

    def send(self, message, body, budget):
        """attempt(), plus one recovery if the connection to the server broke.

        Replayable requests are sent again once on the recovered server. A tools/call is
        not: it may already have run in the browser, so it gets an error that says what is
        known. Raises BackendLost when the server cannot answer the request.
        """
        method = message.get("method")
        epoch = self._recovery_epoch
        try:
            return self.attempt(message, body)
        except Exception as e:
            if self.recover is None or not _is_connection_failure(e):
                raise
            failure = e
        self.log("connection to the shared server failed ({}): {}".format(method, failure))
        recovered = self.recover_backend(epoch)
        if method == "tools/call":
            raise BackendLost(self._lost_call_message(message, failure, recovered))
        if not recovered or method not in REPLAYABLE_METHODS:
            raise BackendLost(self._lost_message(failure))
        # The recovery may have used most of the budget; the retry gets a fresh one.
        self._local.deadline = time.monotonic() + budget
        try:
            return self.attempt(message, body)
        except Exception as e:
            if not _is_connection_failure(e):
                raise
            raise BackendLost(self._lost_message(e)) from e

    def _lost_message(self, exc):
        detail = ""
        if self.recover_detail is not None:
            try:
                detail = self.recover_detail() or ""
            except Exception:
                detail = ""
        return ("BrowserPlug launcher could not reach the shared server on :{}: {}{}".format(
            self.port, exc, " ({})".format(detail) if detail else ""))

    def _lost_call_message(self, message, exc, recovered):
        """What a tools/call that lost its connection is told, by what is known about it."""
        if recovered:
            head = ("The connection to the BrowserPlug server broke during this call; the "
                    "server is reachable again on :{}, but the call was not re-sent. "
                    .format(self.port))
        else:
            head = ("The BrowserPlug server went away during this call and the launcher "
                    "could not restart it. {}. ".format(self._lost_message(exc)))
        if _call_only_reads(message.get("params")):
            tail = "This call only reads, so it is safe to call again."
        elif _was_refused(exc):
            tail = "The connection was refused, so the call did not run; call it again."
        else:
            tail = ("It may have already run in the browser: check the page or tab before "
                    "repeating an action that changes anything.")
        return head + tail

    # ---- timing ------------------------------------------------------------------

    def report_timing(self, tool, started_epoch, total_ms):
        """One tools/call's stdio-side timing, for last_command.timings.

        The launcher is the only process that sees the cycle as the CLIENT does, stdio in
        to stdio out. Best-effort: a lost report costs a diagnostics line.
        """
        try:
            payload = json.dumps({
                "source": "launcher",
                "launcher_pid": os.getpid(),
                "client_pid": self.client_pid,   # the client that spawned this launcher
                "tool": tool,
                "started_epoch": started_epoch,
                "total_ms": round(total_ms, 1),
            }).encode()
            req = urllib.request.Request(self.timing_url, data=payload, method="POST")
            req.add_header("Content-Type", "application/json")
            req.add_header(CLIENT_PID_HEADER, str(self.client_pid))
            # /timing demands a signed request (review finding S2). Imported here: the
            # scrypt behind the signature costs 32 MB once, and a launcher that never
            # finishes a tools/call should not pay it.
            from browserplug_shared.local_auth import signed_headers
            for k, v in signed_headers("POST", "/timing", payload).items():
                req.add_header(k, v)
            self._opener.open(req, timeout=HEALTH_TIMEOUT_SECONDS).close()
        except Exception:
            pass  # never let timing hurt, or even log-spam, the call path

    # ---- one client frame --------------------------------------------------------

    def handle_frame(self, raw, exchange=None):
        """Forward one client frame and emit whatever the backend answered.

        A frame with no `id` is a notification: the server accepts it with 202 and an
        empty body, and nothing goes back to the client. `exchange` is the request's entry
        in the table of requests in flight when the reader made one (dispatch); a frame
        handled on its own makes its own.
        """
        try:
            message = json.loads(raw)
        except Exception as e:
            self.log("dropping unparseable frame from the client:", e)
            return
        if not isinstance(message, dict):
            self.log("dropping non-object frame from the client")
            return

        method = message.get("method")
        msg_id = message.get("id")
        is_request = "id" in message and msg_id is not None
        body = raw.encode("utf-8") if isinstance(raw, str) else raw

        if method == "initialize":
            # Keep it verbatim: a rebuild has to send what the client actually sent,
            # including its clientInfo and capabilities.
            self._initialize_frame = body
            self.drop_session()

        # A 2026-07-28 frame (always None unless era is `all`) takes the modern path in
        # attempt(): its own connection, the routing headers, no session. `exchange` is how
        # a cancel from the client reaches it.
        version = self.carriage.version_of(message)
        if version is None:
            exchange = None
        elif exchange is None:
            exchange = self.carriage.exchanges.begin(msg_id)

        started = time.time()
        budget = (self.tool_deadline_seconds if method == "tools/call"
                  else self.control_deadline_seconds)
        self._local.deadline = time.monotonic() + budget
        self._local.modern = (version, exchange) if version is not None else None
        try:
            status, headers, text = self.send(message, body, budget)
        except relay_era.Cancelled:
            self.log("request {!r} was cancelled by the client; sending no reply".format(msg_id))
            return
        except relay_era.HeaderRefused as e:
            # Nothing was sent: the request names something no header can carry.
            self.log(str(e))
            if is_request:
                self.emit_error(msg_id, str(e), relay_era.JSONRPC_INVALID_PARAMS)
            return
        except BackendLost as e:
            self.log("backend call failed ({}):".format(method), e)
            if is_request:
                self.emit_error(msg_id, str(e))
            return
        except DeadlineExceeded:
            self.log("{} exceeded its {:g} s deadline".format(method, budget))
            if is_request:
                self.emit_error(msg_id, "BrowserPlug launcher gave up on {} after {:g} s: "
                                        "the shared server on :{} did not answer (it may be "
                                        "wedged; check server_info or restart it)".format(
                                            method, budget, self.port),
                                JSONRPC_REQUEST_TIMEOUT)
            return
        except Exception as e:
            self.log("backend call failed ({}):".format(method), e)
            if is_request:
                self.emit_error(msg_id, "BrowserPlug launcher could not reach the shared "
                                        "server on :{}: {}".format(self.port, e))
            return
        finally:
            self._local.deadline = None
            self._local.modern = None
            if exchange is not None:
                self.carriage.exchanges.end(msg_id, exchange)
        if exchange is not None and exchange.cancelled:
            return      # the client withdrew it while the answer was on its way

        if method == "initialize":
            self.remember_session(headers, messages_from(headers, text))

        frames = messages_from(headers, text)
        if version is not None:
            self.carriage.note_reply(status, frames)
        if status >= 400 and not is_request:
            # JSON-RPC forbids answering a notification, and mcp's reply here has id null.
            if frames:
                self.log("backend refused {} with HTTP {}: {}".format(
                    method, status, frames[0][:200]))
            frames = []
        # A 4xx/5xx the server could not tie to the request ("Missing session ID" comes
        # with id null, a 413 or 500 can be plain text) would leave the client waiting on
        # its id forever, or put a non-JSON line on its stdout.
        # A 2xx stream can end without the answer too (only a priming event or a log
        # notification, typically because the server went down mid-stream during a
        # /restart), so every request needs a frame under its own id, whatever the status.
        # A tools/call is never replayed here: it may already have run in the browser.
        answered = not is_request or any(_frame_id(f) == msg_id for f in frames)
        dropped = []
        for frame in frames:
            if not answered and _orphan(frame):
                dropped.append(frame)
                continue  # re-issued below under the request's id
            self.emit(frame)

        if not answered:
            err_code, err_message = _first_error(frames)
            if not err_message and status < 400:
                err_message = ("shared server ended its HTTP {} response to {} without an "
                               "answer (it may have restarted mid-call); retry".format(
                                   status, method))
            elif not err_message:
                err_message = "shared server returned HTTP {} for {}".format(status, method)
                plain = [f for f in dropped if _frame_id(f) is _UNPARSED]
                if plain:
                    err_message += ": " + plain[0][:200]
            self.emit_error(msg_id, err_message, err_code)

        if method == "tools/call":
            tool = (message.get("params") or {}).get("name")
            self.report_timing(tool, started, (time.time() - started) * 1000.0)

    # ---- tool-surface watch ------------------------------------------------------

    def health(self):
        req = urllib.request.Request(self.health_url, method="GET")
        req.add_header(CLIENT_PID_HEADER, str(self.client_pid))
        for k, v in self.identity_headers.items():
            req.add_header(k, v)
        with self._opener.open(req, timeout=HEALTH_TIMEOUT_SECONDS) as r:
            return json.loads(r.read().decode("utf-8"))

    def announce_tool_change(self, key):
        """Tell the client the tool list changed, in the form its era allows.

        Under any era but `all` the notice is the unsolicited notification it has always been.
        Under `all` a client on 2026-07-28 may not be sent one it did not ask for, so it is
        told on its listen streams only, and a client on the handshake era (one that sent
        `initialize`) gets the unsolicited notification. A client that has done neither
        is told nothing: it fetches the list before it uses it.
        """
        told = self.carriage.tools_changed()
        if not self.carriage.enabled or self._initialize_frame is not None:
            self.emit(json.dumps({"jsonrpc": "2.0",
                                  "method": "notifications/tools/list_changed"}))
            self.log("tool surface changed (pid/sig {}); sent "
                     "tools/list_changed".format(key))
        elif told:
            self.log("tool surface changed (pid/sig {}); told {} listen "
                     "stream(s)".format(key, told))
        return told

    def watch_tools(self):
        """Tell the client when the shared server's tool surface changes.

        server_pid moves on a restart (new code); tools_signature moves when the same
        server re-shapes a tool in place, which is what toggling the extension's optional
        proxy permission does to tabs/navigate. Either one means the client's tool list is
        stale. The /health GET is also the channel that reports THIS launcher's build to
        the server: it runs for the launcher's whole life, so it answers even for a
        launcher whose calls never reach a browser.
        """
        last_key = None
        failures = 0
        while not self._stopping.is_set():
            self._stopping.wait(self.poll_seconds)
            if self._stopping.is_set():
                return
            epoch = self._recovery_epoch
            try:
                health = self.health()
            except Exception:
                failures += 1
                if failures >= WATCH_FAILURES_BEFORE_RECOVERY and self.recover is not None:
                    # A server nobody is calling still has to be back before the next call
                    # needs it. recover_backend is single-flight and backs off.
                    if self.recover_backend(epoch):
                        failures = 0
                continue
            failures = 0
            key = (health.get("server_pid"), health.get("tools_signature"))
            if key == last_key:
                continue
            if last_key is not None:
                self.announce_tool_change(key)
            last_key = key

    # ---- lifecycle ---------------------------------------------------------------

    def close_session(self):
        """Best-effort DELETE so the server drops the session now rather than on timeout."""
        if not self._session_id:
            return
        try:
            req = urllib.request.Request(self.mcp_url, method="DELETE")
            req.add_header(SESSION_HEADER, self._session_id)
            req.add_header(CLIENT_PID_HEADER, str(self.client_pid))
            if self._protocol_version:
                req.add_header(PROTOCOL_HEADER, self._protocol_version)
            self._opener.open(req, timeout=HEALTH_TIMEOUT_SECONDS).close()
        except Exception:
            pass

    def run(self, stdin=None):
        """Read frames until stdin closes. Returns the process exit code."""
        from concurrent.futures import ThreadPoolExecutor

        stream = stdin if stdin is not None else sys.stdin.buffer
        watcher = threading.Thread(target=self.watch_tools,
                                   name="browserplug-tools-watch", daemon=True)
        watcher.start()
        pool = ThreadPoolExecutor(max_workers=MAX_WORKERS,
                                  thread_name_prefix="browserplug-relay")
        control = ThreadPoolExecutor(max_workers=CONTROL_WORKERS,
                                     thread_name_prefix="browserplug-control")
        try:
            for line in stream:
                text = (line.decode("utf-8", "replace") if isinstance(line, bytes)
                        else line).strip()
                if not text:
                    continue
                self.dispatch(text, pool, control)
        except KeyboardInterrupt:
            pass
        finally:
            self._stopping.set()
            # The client is gone: work still queued would drive the user's browser for
            # nobody. Calls already running finish (bounded by their deadline).
            pool.shutdown(wait=False, cancel_futures=True)
            control.shutdown(wait=False, cancel_futures=True)
            pool.shutdown(wait=True)
            control.shutdown(wait=True)
            self.close_session()
        return 0

    def dispatch(self, text, pool, control):
        """Route one stdin line: answer what cannot be forwarded, else pick a lane."""
        try:
            message = json.loads(text)
        except Exception as e:
            self.log("unparseable frame from the client:", e)
            self.emit_error(None, "Parse error: the frame is not valid JSON",
                            JSONRPC_PARSE_ERROR)
            return
        if isinstance(message, list):
            # MCP dropped JSON-RPC batching (2025-06-18) and the server does not take it.
            # Say so under every request id instead of leaving the client waiting.
            ids = [m.get("id") for m in message
                   if isinstance(m, dict) and m.get("id") is not None and "method" in m]
            self.log("rejecting a JSON-RPC batch of {} frame(s)".format(len(message)))
            for msg_id in ids:
                self.emit_error(msg_id, "JSON-RPC batches are not supported; send each "
                                        "request as its own frame", JSONRPC_INVALID_REQUEST)
            if not ids:
                self.emit_error(None, "JSON-RPC batches are not supported",
                                JSONRPC_INVALID_REQUEST)
            return
        # 2026-07-28 traffic the relay settles on this thread, in stdin order: a listen
        # stream is answered here and a cancel ends its request here. Nothing blocks. An
        # error must not end the reader, which is the client's whole session, so the frame
        # then goes on as any other would.
        exchange = None
        try:
            if isinstance(message, dict):
                if self.carriage.intercept(message):
                    return
                exchange = self.carriage.enter(message)
        except Exception as e:
            self.log("could not settle a frame locally:", e)
        lane = pool if (isinstance(message, dict)
                        and message.get("method") == "tools/call") else control
        lane.submit(self.guarded, text, exchange)

    def guarded(self, text, exchange=None):
        """A worker that raised would die silently in the pool and strand its caller."""
        try:
            self.handle_frame(text, exchange)
        except Exception as e:
            self.log("relay worker error:", e)


def run_relay(port, log, identity_headers=None, recover=None, recover_detail=None, era=None):
    """Serve one stdio client against the shared server on `port`.

    `recover` is launcher.ensure_server: called when the connection to the server breaks.
    `era` is the mcp.era setting (browserplug_shared/mcp_era.py); only `all` carries
    2026-07-28.
    """
    return StdioRelay(port, log=log, identity_headers=identity_headers, recover=recover,
                      recover_detail=recover_detail, era=era).run()
