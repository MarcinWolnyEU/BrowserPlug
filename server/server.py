#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11,<3.13"
# dependencies = [
#     # The official MCP Python SDK: the low-level Server, streamable HTTP and stdio. The <3
#     # ceiling keeps a clean install off a major nobody has run; server/server.py.lock pins
#     # the exact release (uv lock --script server/server.py).
#     "mcp>=2.2.0,<3",
#     # Imported directly (server/rpc/protocol_era.py, server/tools/tool_registry.py). mcp
#     # pins its exact version; this range only restates mcp's.
#     "mcp-types>=2.2.0,<3",
#     "websockets>=17.1",
#     "uvicorn>=0.54.0",
#     # mcp needs pydantic>=2.12; 2.13.5 is the newest STABLE release and the one the lock
#     # pins and the suite has run on (.tmp/dev-docs/dependencies.md). A 2.14 beta also satisfies this
#     # floor, and the lock keeps it out.
#     "pydantic>=2.13.5",
#     # Connection diagnostics: map peer port -> PID/exe/cwd.
#     "psutil>=7.2.2",
#     # The encrypted extension WebSocket (server/ws/ws_crypto.py). 48.0.0 is the first release
#     # with ML-KEM on the OpenSSL backend its wheels ship.
#     "cryptography>=48",
#     # The proxy transport (server/proxy/) imports these directly; mcp requires both at lower
#     # floors, so these name the versions the suite has run on.
#     "anyio>=4.15.1",
#     "httpx2>=2.13.1",
# ]
# ///
"""
BrowserPlug Server - WebSocket server that bridges browser extension with MCP clients

Copyright 2026 Marcin Wolny
Licensed under the MIT License - see LICENSE file for details
"""

import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime
from typing import Dict, Any, Optional

# Import roots, computed from __file__ and never from the cwd. shared-py/ is a folder, not a
# package: it holds the one package `browserplug_shared`, so the folder goes on sys.path.
# The repo root makes `server` importable. Run as a script (`python server/server.py`),
# sys.path[0] is server/ itself, whose server.py would shadow the `server` package, so it
# is dropped first.
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if __package__ in (None, ""):
    sys.path[:] = [p for p in sys.path
                   if os.path.normcase(os.path.abspath(p or os.curdir)) != os.path.normcase(_HERE)]
for _path in (os.path.join(_ROOT, "shared-py"), _ROOT):
    if _path not in sys.path:
        sys.path.insert(0, _path)

# Explicitly: websockets 17 lazy-loads its submodules, so `websockets.exceptions` is an
# AttributeError unless the name is imported.
from websockets.exceptions import ConnectionClosed
from server.tools.mcp_tools import BrowserPlugTools

# Who may call what on the MCP HTTP port now that it follows the LAN/WAN bind. The policy
# (local peers free, remote peers token-authenticated, /mcp and /ws-token loopback-only)
# lives in http_auth.py; the routes and the gate that applies it live in http_endpoints.py,
# and server.py keeps one call into it plus the bind host.
from server.web import host_guard, http_endpoints
from server.ws import http_auth

# The encrypted extension WebSocket (the handshake, the records, the socket wrapper) and
# the signed HTTP header that replaced the raw token; see .tmp/dev-docs/ws-encryption.md.
from server.ws import ws_crypto
from server.ws import ws_limits

# Which browser a client with no selection of its own reaches, and whether the server
# may start that browser. Settings, validation and the launch live in the module; the
# hooks here are the WS dispatch, one line in resolve_target_browser and one guard.
from server.browsers import default_browser as browser_pref

# Why a browser left, how slowly it said hello, and what it says happened while it was
# gone. The whole record of the 2026-09-21 drop was "connection closed"; the renderers
# live in the module and the hooks here are the connection handler's teardown, one line
# in the hello, and a close reason.
from server.ws import connection_log

# Telling a client its calls have started landing in a different browser. The memory and
# the wording live in the module; server.py's share is the departed-browser record and
# one attribute (mcp_tools.py carries the rest).
from server.browsers.client_state import CLIENT_TTL_SECONDS, MAX_CLIENT_ENTRIES, ExpiringMap
from server.browsers.routing_notice import RoutingMemory

# Fair per-browser admission. Driven by ONE agent the bridge times out on 0.8% of
# navigations; driven by several subagents sharing the same browser that became 39.3%:
# 474 calls that returned no page at all, and nothing in the response said contention was
# the cause, so every reasonable inference (the site is slow, the URL is bad, retry) was
# wrong, and retrying was actively the worst of them. The policy lives in its own module.
from server.browsers.request_queue import RequestQueue

# Token resolution/persistence lives in its own stdlib-only module so gen_ws_token.py can
# load it without dragging in websockets/uvicorn/mcp.
from browserplug_shared.ws_token import (EXPLICIT_SOURCES, WS_HOST_ENV,
                                         WS_TOKEN_FILE_ENV, resolve_token)  # noqa: F401 (WS_TOKEN_FILE_ENV is re-exported; tests read it off this module)

# Every persisted SETTING (as opposed to the token) lives in one config.json; see
# config_store.py for the file, the directory and why the secret is not in it.
from browserplug_shared.config_store import (config_path, read_ws_host, read_ws_mode,
                                             reset as reset_config_file, write_ws_host, write_ws_mode)

# The /health identity a client seeks the server by (the rest of the MCP port policy, which
# launcher.py shares, is used by lifecycle.py).
from browserplug_shared.mcp_port import HEALTH_SERVICE

# The extension WebSocket's front door (bind policy, who must authenticate, the encrypted
# upgrade), the browser registry (register, displace, promote, remove, hello and state)
# and the routing rules (client key, selection, resolve). Each is built or called from
# here; none imports server.py.
from server.browsers import routing
from server.browsers.browser_registry import BrowserRegistry, handle_browser_state
from server.ws.ws_auth import (AUTH_OFF, WEAK_TOKEN_CHARS, WsAuth, is_localhost_bind,
                               peer_of, ws_auth_timeout_seconds, ws_bind_refusal)

# The send/wait/retry path to an extension and the pending-request table
# (extension_rpc.py), and the dispatch of what an extension sends us (ws_rpc.py). The
# retry policy they consult lives in request_retry.py. Both are built in __init__ from
# callables on this class; neither imports server.py.
from server.rpc.extension_rpc import ExtensionRpc
from server.rpc.ws_rpc import WsRpc, WsRpcHooks

# Naming a browser (brand tables, fork and edition logic, the ExtBrowser record), naming an
# MCP client from the process table, the per-call telemetry slots, and the two reports
# built from all of it. Each is built or called from here; none imports server.py.
from server.browsers import browser_identity
from server.reporting import call_telemetry, client_identity
from server.reporting.call_telemetry import CallTelemetry
from server.reporting.reports import Reports, ReportSources

# The process side (the MCP thread, the WebSocket listener, restart, shutdown, the
# instance lock, logging setup, main()) and the machine's network addresses. Neither
# imports server.py; the entry points below and __main__.py hand this module's class to
# lifecycle.run().
from server.runtime import lifecycle
from server.runtime.lifecycle import LifecycleHooks, ServerLifecycle
from server.reporting.net_addresses import lan_addresses, ws_lan_addresses
from server.ws import ws_host_policy

# NOTE: the server deliberately does NOT import tests/port_coordinator. It used to; it put
# tests/ on sys.path and let the TEST allocator choose a production port, which (a) is absent
# from a release, and (b) made a live server relocate into the test port pool. Tests pass the
# ports they allocated; production scans for its own.

# Logging is configured by an entry point (lifecycle.setup_logging), never by importing
# this module.
logger = logging.getLogger(__name__)


class BrowserPlugServer:
    def __init__(self, host: str = "localhost", port: int = 8772, mcp_port: int = None, start_mcp: bool = True, use_stdio: bool = False, fatal_on_mcp_failure: bool = False, ws_host: str = None, ws_token: str = None, mcp_port_pinned: bool = False):
        self.host = host
        self.port = port

        # --- Extension WebSocket: bind address + token auth ------------------------
        # The WS binds wherever `host` says (localhost) unless ws_host /
        # BROWSERPLUG_WS_HOST opts into a LAN bind for a REMOTE Firefox. Since 1.7.31 the
        # MCP HTTP port FOLLOWS that bind (mcp_bind_host below), so one setting governs
        # both and a remote browser's options page can reach the panels it needs;
        # http_auth gates who may call what. This comment used to say the MCP port stayed
        # localhost-only either way, which stopped being true with that change.
        #
        # A token ALWAYS exists now: explicit arg -> BROWSERPLUG_WS_TOKEN -> the persisted
        # token file -> freshly minted (and persisted). What varies is who has to PRESENT
        # it; see WsAuth.auth_mode(): a remote peer always must; a local one only when the user
        # configured a token on purpose, or when it can bootstrap one from /ws-token.
        # ws_token_explicit is that "on purpose" bit: an auto-minted token must never
        # brick a local browser that has no way of knowing it.
        #
        # Bind address, in order: explicit arg -> BROWSERPLUG_WS_HOST -> the persisted
        # config.json -> `host`. The FILE is how the options page's "allow browsers on
        # other machines" button can stick: a browser cannot set an env var, and the
        # always-on server runs from a scheduled task that never sees a shell's anyway.
        self.ws_host, self.ws_host_source = self._resolve_ws_host(ws_host, host)
        self.ws_token, self.ws_token_source = resolve_token(ws_token, log=logger.warning)
        self.ws_token_explicit = self.ws_token_source in EXPLICIT_SOURCES
        if self.ws_token_explicit and len(self.ws_token) < WEAK_TOKEN_CHARS:
            # A minted token is 256 bits. Every device secret is derived from this value,
            # so a short one can be guessed offline by anyone who records a handshake
            # while impersonating the server. device_tokens stretches it with scrypt,
            # which makes each guess cost a third of a second; it cannot add entropy.
            logger.warning(
                f"The connection token set by {self.ws_token_source} is "
                f"{len(self.ws_token)} characters, under {WEAK_TOKEN_CHARS} (128 bits). "
                f"It is stretched with scrypt, but that only slows guessing. Unset it to "
                f"let the server mint a strong one, or set a longer value.")
        refusal = ws_bind_refusal(self.ws_host, self.ws_token)
        if refusal:
            raise ValueError(refusal)
        self.ws_auth_timeout = ws_auth_timeout_seconds()
        # Who must authenticate and how (ws_auth.py). The callables are read per use:
        # tests and set_ws_host() change these attributes after construction.
        self.preauth_gate = ws_limits.PreauthGate()
        self.ws_auth = WsAuth(
            token=lambda: self.ws_token,
            token_explicit=lambda: self.ws_token_explicit,
            auth_timeout=lambda: self.ws_auth_timeout,
            http_port=lambda: (None if not self.start_mcp or self.use_stdio
                               else self.mcp_port))

        # --- MCP HTTP port ---------------------------------------------------------
        # The MCP HTTP port: lifecycle.choose_mcp_port has the policy (the standard
        # ascending search, the launcher's requested port, the bind-address probe). The bind
        # address is recomputed here rather than cached because set_ws_host() can move
        # self.ws_host before the server starts.
        mcp_bind = http_auth.mcp_bind_host(self.ws_host, self.host)
        self.mcp_port_pinned = bool(mcp_port_pinned)
        self.mcp_port = lifecycle.choose_mcp_port(mcp_port, mcp_bind, pinned=self.mcp_port_pinned)

        logger.info(f"MCP server will use port {self.mcp_port}")
        self.start_mcp = start_mcp
        self.use_stdio = use_stdio
        # Standalone server (the process real clients spawn): an MCP HTTP listener that
        # dies leaves this process holding the Firefox WS port + the single-instance lock
        # with nothing serving MCP -- every launcher then spawns a replacement that exits
        # against the zombie lock. Fatal there; embedded/test servers tolerate it.
        self.fatal_on_mcp_failure = fatal_on_mcp_failure

        # BROWSER REGISTRY (browser_registry.py): one entry per connected Firefox, keyed
        # by the extension's stable instance_id (a provisional key until its
        # extension_hello names it). Several Firefoxes may be connected at once. Every
        # legacy call site (and the extension_connection/_version/_file_access/
        # _proxy_access compat properties below) resolves to the DEFAULT browser (first
        # connected, still alive), so with one browser connected the behavior is exactly
        # what it always was. The lambdas are looked up per call: tests patch these
        # methods on the instance.
        self.registry = BrowserRegistry(
            make_browser=lambda *args: browser_identity.ExtBrowser(*args),
            probe_identity=lambda websocket: self._probe_browser_identity(websocket),
            fail_pending=lambda *args, **kwargs: self.rpc.fail_pending_requests(
                *args, **kwargs),
            reassign_pending_owner=lambda old, new: self.rpc.reassign_pending_owner(
                old, new),
            drop_gate=lambda instance_id: self.request_queue.drop_browser(instance_id),
            close_socket=lambda conn: lifecycle.close_extension_connection(conn),
            refresh_browsers_tool=lambda: self._refresh_browsers_tool(),
            refresh_proxy_tool_visibility=lambda: (
                self.mcp_tools.refresh_proxy_tool_visibility()),
            hello_window=lambda: self.ws_auth_timeout,
            move_gate=lambda old, new: self.request_queue.move_browser(old, new))
        # Which browser each MCP CLIENT drives. Per-client, never global: several Claude
        # Codes share this one server, and a global switch would silently hijack another
        # agent's session; acting in the wrong *logged-in* Firefox is the worst failure
        # this product has. In-memory only (a restart clears it; the no-selection rules
        # then take over gracefully). Bounded: a client not heard from in a day loses its
        # selection, and past MAX_CLIENT_ENTRIES the least recently heard from goes first.
        self.browser_selection: Dict[str, str] = ExpiringMap(   # client_key -> instance_id
            ttl=CLIENT_TTL_SECONDS, max_entries=MAX_CLIENT_ENTRIES)
        # Which browser served each MCP client's last browser-bound call. Read and
        # written by BrowserCalls.request (server/tools); it is what makes a silent reroute speak up.
        self.routing_memory = RoutingMemory()
        # A client whose selection expired is gone; its "served last" entry goes too.
        self.browser_selection.on_drop = self.routing_memory.drop_clients
        # Serializes "nothing is connected, start the pinned browser" across concurrent
        # tool calls and holds off a retry after a start that never connected.
        self._launch_guard = browser_pref.LaunchGuard()

        # One FIFO admission gate per browser. A request that has to wait is QUEUED,
        # never failed for arriving Nth, and its wait is accounted separately from its
        # execution budget, so queuing costs latency, which is recoverable, instead of
        # failures, which are not.
        self.request_queue = RequestQueue()

        # Send, wait, retry and the pending futures. The lambdas are looked up per call:
        # `browsers` is replaced on shutdown and tests patch these methods on the instance.
        self.rpc = ExtensionRpc(
            request_queue=self.request_queue,
            default_browser=lambda: self.default_browser,
            browser_by_instance_id=lambda instance_id: self.browsers.get(instance_id),
            note_command_sent=lambda request: self.telemetry.note_command_sent(request),
            note_browser_activity=lambda browser, action: call_telemetry.note_browser_activity(
                browser, action),
            current_mcp_call=lambda: self.telemetry.mcp_call,
            ws_loop=lambda: self.lifecycle.ws_loop)
        # What an extension sends us over the WebSocket, dispatched by type and action.
        self.ws_rpc = WsRpc(WsRpcHooks(
            note_activity=self._note_extension_activity,
            browser_for_ws=lambda ws: self.registry.browser_for_ws(ws),
            default_browser=lambda: self.default_browser,
            handle_hello=lambda browser, hello: self.registry.handle_hello(browser, hello),
            refresh_browsers_tool=lambda: self._refresh_browsers_tool(),
            handle_browser_state=lambda browser, state: handle_browser_state(
                browser, state),
            send=lambda message, browser=None: self.rpc.send_to_extension(
                message, browser=browser),
            server_start_time=lambda: self.server_start_time,
            mcp_port=lambda: self.mcp_port,
            legacy_extension_version=lambda: self.extension_version,
            begin_restart=lambda trigger: self._begin_restart(trigger),
            default_browser_payload=lambda browser: self.default_browser_payload(browser),
            set_default_browser=lambda payload, browser: self.set_default_browser(
                payload, browser),
            connections_info=lambda **kw: self.get_connections_info(**kw),
            handle_response=lambda data: self.rpc.handle_response(data)))

        # Connection event management
        self._connection_waiters = []  # List of futures waiting for connection

        # Initialize MCP tools
        self.mcp_tools = BrowserPlugTools(self)
        self.mcp_app = self.mcp_tools.get_mcp_app()
        self.mcp_server_task = None

        # --- Connection diagnostics (server self + extension + MCP HTTP clients) ---
        self.server_start_time = datetime.now()
        self.extension_last_activity = None
        # mcp-session-id -> {pid, friendly, exe_path, peer_port, first_seen, last_activity}
        self.http_clients = {}
        # launcher pid -> {version, seen}. A launcher is a SEPARATE process, often started
        # from an older checkout than the running server (a long-lived Claude Code keeps
        # its launcher across server restarts), so its version has to be reported rather
        # than inferred; it stamps every /health poll with it (see version.py's headers).
        # Pruned by age so a dead launcher's version can't linger in the skew warning.
        self.launcher_versions = {}
        # The newest browser command, the newest tools/call and its hop timings
        # (call_telemetry.py), and the two reports built from them and from the state
        # above (reports.py). Every source is a callable read when a report is built.
        self.telemetry = CallTelemetry(lambda: self.http_clients)
        self.reports = Reports(ReportSources(
            last_command_info=lambda: self.telemetry.last_command_info(),
            last_restart=lambda: self.lifecycle.last_restart,
            extension_connection=lambda: self._preferred_field("ws"),
            extension_version=lambda: self._preferred_field("version"),
            extension_file_access=lambda: self._preferred_field("file_access"),
            extension_proxy_access=lambda: self._preferred_field("proxy_access"),
            proxy_tools_enabled=lambda: self.mcp_tools.proxy_tools_enabled(),
            tools_signature=lambda: getattr(self.mcp_tools, "tools_signature", None),
            browsers_by_slot=lambda: self.registry.by_slot(),
            preferred_browser=lambda: self.preferred_browser(),
            routed_browser_for_client=lambda key: self.routed_browser_for_client(key),
            browser_selection=lambda: self.browser_selection,
            default_browser_pref=lambda: browser_pref.read(),
            http_clients=lambda: self.http_clients,
            launcher_version=lambda pid: self.launcher_version(pid),
            connected_browser_pids=lambda: client_identity.connected_browser_pids(self.port),
            request_queue=lambda: self.request_queue,
            server_start_time=lambda: self.server_start_time,
            extension_last_activity=lambda: self.extension_last_activity,
            ws_port=lambda: self.port,
            mcp_port=lambda: self.mcp_port,
            ws_host=lambda: self.ws_host,
            ws_host_source=lambda: self.ws_host_source,
            ws_mode=lambda: self.current_ws_mode(),
            ws_auth_mode=lambda: self.ws_auth.auth_mode(peer_local=True),
            ws_token_source=lambda: self.ws_token_source,
            ws_token_endpoint=lambda: self.ws_auth.token_endpoint_url(),
            lan_addresses=lambda: lan_addresses(),
            ws_lan_addresses=lambda: ws_lan_addresses(),
            connections_info=lambda: self.get_connections_info()))

        # The process side: the MCP thread, the WebSocket listener, restart, shutdown
        # (lifecycle.py). Every value it needs is read through these callables at use time.
        self.lifecycle = ServerLifecycle(LifecycleHooks(
            host=lambda: self.host,
            port=lambda: self.port,
            mcp_port=lambda: self.mcp_port,
            ws_host=lambda: self.ws_host,
            ws_host_source=lambda: self.ws_host_source,
            start_mcp=lambda: self.start_mcp,
            use_stdio=lambda: self.use_stdio,
            mcp_port_pinned=lambda: self.mcp_port_pinned,
            fatal_on_mcp_failure=lambda: self.fatal_on_mcp_failure,
            # A fresh app (and session manager) per start: a manager runs only once.
            http_app=lambda: self._wrap_client_tracking(self.mcp_app.http_app()),
            run_stdio=lambda: self.mcp_app.run_stdio(),
            ws_handler=lambda websocket: self.handle_extension_connection(websocket),
            ws_process_request=lambda connection, request: self._ws_process_request(
                connection, request),
            local_auth_mode=lambda: self.ws_auth.auth_mode(peer_local=True),
            token_source=lambda: self.ws_token_source,
            count_in_flight=lambda: len(self.rpc.pending_requests),
            open_connections=lambda: [b.ws for b in list(self.browsers.values())
                                      if b.ws is not None],
            close_registry=lambda *args, **kwargs: self.registry.close_all(*args, **kwargs),
            set_restarting=lambda on: self.rpc.set_restarting(on)))

    # ---- Browser registry --------------------------------------------------------
    # The records and what changes them live in browser_registry.py, the routing rules in
    # routing.py. What stays here is the surface that mcp_tools and the HTTP layer read.

    @property
    def browsers(self):
        return self.registry.browsers

    @browsers.setter
    def browsers(self, value):
        self.registry.browsers = value

    @property
    def _departed(self):
        return self.registry.departed

    @property
    def default_browser(self) -> Optional[browser_identity.ExtBrowser]:
        """The browser every unrouted call goes to: first-connected, still alive. The
        legacy single-socket answer; routing uses preferred_browser."""
        return self.registry.default_browser

    def live_browsers(self):
        """Browsers a tool call can actually be routed to, in slot order."""
        return self.registry.live_browsers()

    def preferred_browser(self):
        """Where a client with NO selection of its own lands (routing.preferred_browser)."""
        return routing.preferred_browser(self.live_browsers())

    def _preferred_field(self, name: str):
        """One field of the browser an unrouted client lands in, None with no browser.
        The /health scalars read through this, not through the legacy compat properties
        (first-connected), so they describe the browser that takes the call."""
        b = self.preferred_browser()
        return getattr(b, name, None) if b is not None else None

    def resolve_target_browser(self, client_key: str):
        """Which Firefox this MCP client's call goes to: an ExtBrowser, or an
        agent-facing ERROR STRING (routing.resolve_target has the rules)."""
        return routing.resolve_target(self.live_browsers(), self.browser_selection,
                                      self._departed, client_key)

    async def ensure_default_browser_running(self):
        """Start the pinned browser when nothing is connected, if the user allowed it.

        Returns the browser once it has connected, else None. Called from the tool path
        after routing has already failed, so None simply leaves that failure standing:
        the agent is told nothing is connected either way, and nothing here can make a
        working call fail.
        """
        if self.live_browsers():
            return None
        return await self._launch_guard.ensure_running(self)

    def default_browser_payload(self, requester=None):
        """The default-browser setting plus the roster to pick from (options page)."""
        return browser_pref.payload(self, requester)

    def set_default_browser(self, request, requester=None):
        """Persist a default-browser setting. Returns (payload, error)."""
        return browser_pref.apply_request(self, request, requester)

    def routed_browser_for_client(self, client_key: str):
        """Which browser this client's calls would reach right now, or None. Read-only
        twin of resolve_target_browser (routing.routed_browser)."""
        return routing.routed_browser(self.live_browsers(), self.browser_selection,
                                      client_key)

    @staticmethod
    def _resolve_ws_host(explicit, default_host):
        """(host, source) for the extension WebSocket bind. `source` is what the options
        page shows the user, so they know WHY it is bound where it is, and whether the
        button can change it (an env var set outside the browser always wins)."""
        if explicit:
            return explicit, "arg"
        env = (os.environ.get(WS_HOST_ENV) or "").strip()
        if env:
            return env, "env"
        saved = read_ws_host()
        if saved:
            # Only the file is second-guessed: env and --ws-host are explicit and fail
            # loudly. A stored host that cannot be bound (hand-edited, or written before
            # /ws-bind validated) would otherwise fail every start until the file is fixed.
            if ws_host_policy.host_is_bindable(saved):
                return saved, "file"
            logger.error(
                f"ws.host {saved!r} in {config_path()} cannot be bound on this machine; "
                f"falling back to {default_host!r}. Fix it from the options page or "
                f"delete the key.")
        return default_host, "default"

    def current_ws_mode(self):
        """'local' | 'lan' | 'wan': the network posture the options page shows.

        'local' is derived from the bind being localhost (0.0.0.0 is NOT local:
        ws_auth.LOCALHOST_BIND_HOSTS); a non-local bind reads the persisted LAN/WAN choice,
        defaulting to 'lan' (a stored bind with no mode beside it is LAN-oriented).
        Re-read per call, never cached: /ws-bind writes the config moments before the
        restart, and the replacement process re-reads it naturally."""
        if is_localhost_bind(self.ws_host):
            return "local"
        return read_ws_mode() or "lan"

    def set_ws_host(self, host, mode=None):
        """Persist a new bind address and adopt it (the restart is the caller's job: the
        socket is already bound, so only a fresh process can honour this).

        Returns (host, error). An env-set bind is NOT overridden: it would look like it
        worked and then revert on the next start, which is worse than saying no.

        `mode` ("lan"/"wan") qualifies a non-local bind; reverting to localhost clears
        the stored mode, and a non-local bind WITHOUT a mode keeps whatever is stored.
        """
        host = (host or "").strip() or self.host
        if self.ws_host_source == "env":
            return None, (f"{WS_HOST_ENV} is set in this server's environment ({self.ws_host}) "
                          f"and always wins. Unset it, or change it there.")
        refusal = ws_bind_refusal(host, self.ws_token)
        if refusal:
            return None, refusal
        if write_ws_host(host) is None:
            return None, f"Could not write {config_path()}"
        if is_localhost_bind(host):
            write_ws_mode(None)
        elif mode:
            write_ws_mode(mode)
        self.ws_host = host
        self.ws_host_source = "file"
        return host, None

    def reset_config(self):
        """Delete config.json: every stored setting reverts to its fresh-install default.

        The options page's "Reset all settings to defaults" clears the EXTENSION's
        storage; this is the server's half of the same button. Returns
        (rebind_host, error): `rebind_host` is the address the caller must restart onto
        for the reset to take effect, or None when the running bind already matches what
        a fresh install would choose.

        The token is NOT touched. It is not a setting -- deleting it would silently
        lock out every remote browser the user configured, and rotation is
        `gen_ws_token.py --write`, which says so and prints the replacement.
        """
        if not reset_config_file():
            return None, f"Could not delete {config_path()}"
        # An arg- or env-set bind outlives the reset (it never came from the file), so
        # the running bind is already what a fresh start would pick.
        if self.ws_host_source != "file":
            return None, None
        previous = self.ws_host
        self.ws_host, self.ws_host_source = self._resolve_ws_host(None, self.host)
        return (self.ws_host, None) if self.ws_host != previous else (None, None)

    def _probe_browser_identity(self, websocket):
        """Local-browser enrichment (pid / exe / brand / start time) for a peer
        socket: Firefox or any fork of it (browser_brand).

        Display only; identity is the extension's instance_id. Empty for a remote peer
        (no local process to probe), when the pid lookup comes up dry, and importantly
        when the peer is NOT a browser at all: two extension sockets from one such
        process (a test harness faking the extension) share a pid, and a pid is only a
        browser identity when it really is a browser.
        """
        try:
            port = websocket.remote_address[1]
        except Exception:
            return {}
        if not isinstance(port, int):
            return {}
        ident = client_identity.browser_instance_identity(
            client_identity.pid_for_local_port(port))
        return ident if ident.get("pid") and ident.get("is_browser") else {}

    def ws_lan_addresses(self):
        """The adapter-named LAN addresses, for /diagnose-wan (net_addresses.py)."""
        return ws_lan_addresses()

    def _refresh_browsers_tool(self):
        """Show/hide the `browsers` tool (and refresh the roster in its description) to
        match who is connected right now. Idempotent; safe to call on any registry change."""
        try:
            self.mcp_tools.refresh_browsers_tool()
        except Exception as e:
            logger.error(f"Failed to refresh the browsers tool: {e}")

    # ---- Single-extension compat properties ---------------------------------------
    # The registry is the truth; these keep every existing consumer (tests, the popup,
    # /health, mcp_tools) working against the DEFAULT browser exactly as before.

    @property
    def extension_connection(self):
        b = self.preferred_browser()
        return b.ws if b is not None else None

    @extension_connection.setter
    def extension_connection(self, websocket):
        """Assigning the socket binds/unbinds the default browser's slot."""
        if websocket is None:
            b = self.preferred_browser()
            if b is not None:
                self.registry.remove(b)
            return
        if self.registry.browser_for_ws(websocket) is not None:
            return
        b = self.preferred_browser()
        if b is not None:
            b.ws = websocket   # raw rebind: no displacement bookkeeping on this path
            b.last_seen = datetime.now()
        else:
            self.registry.register(websocket)

    @property
    def extension_version(self):
        b = self.preferred_browser()
        return b.version if b is not None else None

    @extension_version.setter
    def extension_version(self, value):
        b = self.preferred_browser()
        if b is not None:
            b.version = value

    @property
    def extension_file_access(self):
        b = self.preferred_browser()
        return b.file_access if b is not None else None

    @extension_file_access.setter
    def extension_file_access(self, value):
        b = self.preferred_browser()
        if b is not None:
            b.file_access = value

    @property
    def extension_proxy_access(self):
        b = self.preferred_browser()
        return b.proxy_access if b is not None else None

    @extension_proxy_access.setter
    def extension_proxy_access(self, value):
        b = self.preferred_browser()
        if b is not None:
            b.proxy_access = value

    async def _upgrade_in_place(self, websocket, request):
        """The AUTH_OFF form of WsAuth.upgrade_socket: the socket is already registered, so the
        record's socket is replaced by the SecureSocket, or the socket is closed."""
        loop = asyncio.get_event_loop()
        peer = getattr(websocket, "remote_address", None)
        secure = await self.ws_auth.upgrade_socket(websocket, request, peer, True,
                                                   loop.time() + self.ws_auth_timeout)
        record = self.registry.browser_for_ws(websocket)
        if secure is not None and record is not None:
            record.ws = secure
        return secure

    def _ws_process_request(self, connection, request):
        """Plain-HTTP fallback on the WS port: GET /reachable answers 200, token-free.

        This is the ONE deliberately unauthenticated endpoint the server exposes: the
        target the options page's WAN checker hands to check-host.net, so an external
        probe can prove the port is reachable WITHOUT the token ever leaving the page.
        The body must stay minimal: an internet-facing 200 leaks no version, no pids,
        no config; only "a BrowserPlug answers here", which the 4401 close reason of a bare
        WS attempt reveals anyway.

        Returning None hands every other request to the normal WebSocket handshake.
        Kept sync + I/O-free: an exception here would abort the handshake it serves.
        NB `request.path` includes any query string; strip before comparing.
        """
        path = (request.path or "").split("?", 1)[0]
        if path.rstrip("/") != "/reachable":
            return None
        # websockets 17 calls this hook for requests 16 dropped while parsing: any HTTP
        # method, and HTTP/1.0. Under 16 a POST/HEAD/DELETE here never reached us at all,
        # so answering them alike would silently widen the one endpoint that is
        # deliberately token-free and may face the internet. Stay GET-only; returning None
        # lets the library answer 405 itself. 16's Request has no `method` at all, so the
        # getattr default keeps this a no-op there.
        if getattr(request, "method", "GET") != "GET":
            return None
        body = json.dumps({"service": HEALTH_SERVICE, "reachable": True}).encode("utf-8")
        # Construct the Response directly. NOT connection.respond(): that hard-codes
        # Content-Type: text/plain, and Headers.__setitem__ APPENDS (multidict), so
        # "overwriting" it would emit two Content-Type headers.
        from websockets.datastructures import Headers
        from websockets.http11 import Response
        return Response(200, "OK", Headers([
            ("Content-Type", "application/json"),
            ("Content-Length", str(len(body))),
            ("Cache-Control", "no-store"),
            ("Connection", "close"),
        ]), body)

    async def handle_extension_connection(self, websocket):
        """Handle a WebSocket connection from a browser extension.

        Any number of Firefoxes may be connected. A connection with the same identity as
        a registered browser (same instance_id, or same Firefox pid before the hello
        lands) displaces that browser's old socket; a different one is simply a new
        registry entry; no kick.

        Unless WsAuth.auth_mode() says AUTH_OFF for this peer, it must authenticate
        BEFORE any of that happens: an unauthenticated socket is closed (4401, with the
        token endpoint in the close reason) and never registered.
        """
        logger.info(f"Extension connected from {websocket.remote_address}")
        accepted_at = time.monotonic()

        _peer_host, is_remote = peer_of(websocket)
        mode = self.ws_auth.auth_mode(peer_local=not is_remote)
        replay = []
        if mode != AUTH_OFF:
            # serve() starts every socket at the small pre-auth frame limit and this
            # bounds how many can sit there (ws_limits.py). A refused socket costs nothing.
            if not self.preauth_gate.try_acquire(_peer_host):
                logger.warning(f"Refused extension connection from "
                               f"{websocket.remote_address}: too many unauthenticated "
                               f"sockets ({self.preauth_gate.held()} held)")
                try:
                    await websocket.close(code=ws_limits.CLOSE_TRY_LATER,
                                          reason=ws_limits.BUSY_REASON)
                except Exception:
                    pass
                return
            try:
                authenticated, replay, websocket = await self.ws_auth.authenticate(
                    websocket, mode=mode, peer_local=not is_remote)
            finally:
                self.preauth_gate.release(_peer_host)
            if not authenticated:
                return  # closed; never registered, nothing in flight, no waiters notified
        # Authenticated (or AUTH_OFF, which registers unproven local peers by design):
        # replies may now be as large as a full-page screenshot.
        ws_limits.raise_limit(websocket)

        # register() and everything after it sits INSIDE the try: the finally below is what
        # removes the record, so a raise between the two would leave a zombie that routing
        # still reaches.
        close_code, close_reason = None, None
        browser = None
        try:
            # The peer's process lookup scans the TCP table and walks parents: on an
            # executor, so it cannot hold up every other browser's traffic on this loop.
            ident = None if is_remote else await asyncio.get_running_loop().run_in_executor(
                None, self._probe_browser_identity, websocket)
            browser = self.registry.register(websocket, accepted_at=accepted_at, ident=ident)
            browser.encrypted = bool(getattr(websocket, "encrypted", False))
            self.extension_last_activity = datetime.now()
            self._refresh_browsers_tool()

            # Notify all waiters that a connection has been established
            self._notify_connection_waiters()

            # Frames received while authenticating (the hello itself, plus anything that
            # overtook it), handled now, in arrival order, exactly as if they had come in
            # through the loop below.
            for message in replay:
                await self.handle_extension_message(message, websocket)
            reading = websocket
            while reading is not None:
                current, reading = reading, None
                async for message in current:
                    owner = self.registry.browser_for_ws(current) or browser
                    if (mode == AUTH_OFF and not owner.encrypted
                            and ws_crypto.is_request(message)):
                        # AUTH_OFF registers before anything is read, so an extension
                        # that holds a token asks for the channel here instead. The
                        # handshake still proves the token; the record swaps sockets,
                        # and reading resumes through the SecureSocket.
                        secure = await self._upgrade_in_place(current, message)
                        if secure is not None:
                            websocket = reading = secure
                            # The hello may have re-keyed or merged the record: set the
                            # flag on whichever one owns the socket now.
                            owner = self.registry.browser_for_ws(secure) or owner
                            owner.encrypted = True
                        break
                    await self.handle_extension_message(message, websocket)
        except ConnectionAbortedError:
            # Windows' shape of an abrupt peer death. It used to log a bare "Extension
            # disconnected"; the teardown below says the same thing and names the browser.
            pass
        except ConnectionClosed as e:
            # NOT an error. "no close frame received or sent" is what a browser being
            # killed looks like, and logging it at ERROR with no browser named is what
            # made the 2026-09-21 drop unreadable.
            close_code, close_reason = connection_log.closed_from_exception(e)
        except Exception as e:
            logger.error(f"Error handling extension connection: {e}")
        finally:
            # Re-resolve by socket: the hello may have re-keyed or merged this entry, and
            # a newer connection may already have taken the record over (displacement).
            # A displaced socket must NOT tear down its successor; that would leave the
            # live socket receiving but unreachable for sends.
            browser = self.registry.browser_for_ws(websocket)
            if browser is not None:
                if close_code is None:
                    # Type-checked, not trusted: a test double answers these with a Mock,
                    # and a websockets connection that never closed answers with None.
                    code = getattr(websocket, "close_code", None)
                    reason = getattr(websocket, "close_reason", None)
                    close_code = code if isinstance(code, int) else None
                    close_reason = reason if isinstance(reason, str) else None
                held_for = (time.monotonic() - browser.accepted_at
                            if browser.accepted_at is not None else None)
                logger.info(connection_log.disconnect_line(
                    browser.describe(), connected_for=held_for,
                    code=close_code, reason=close_reason))
                # Kept on the RECORD, not just in the log: after a reconnect the question
                # is always "why did it go last time?", and the socket that knew is gone.
                browser.last_disconnect_at = datetime.now()
                browser.last_close_code = close_code
                browser.last_close_reason = close_reason or None
                self.registry.remove(browser)
                # Nothing can answer this browser's in-flight requests now, so fail them
                # here instead of letting each burn its full timeout and then misreport
                # "Request timed out". Only ITS requests: other browsers are unaffected.
                if close_code == ws_limits.CLOSE_TOO_BIG:
                    # websockets closes with 1009 when a frame exceeds max_size. Without
                    # this the caller was told the browser "disconnected", and a retry of
                    # the same read would hit the same limit.
                    failure = ws_limits.oversize_reason(browser.describe())
                else:
                    failure = (
                        f"{browser.describe()} disconnected before responding (the "
                        f"browser closed, restarted, or reloaded the extension). This is usually "
                        f"transient — the extension typically reconnects within seconds.")
                self.rpc.fail_pending_requests(
                    failure, instance_id=browser.instance_id,
                    retry=close_code != ws_limits.CLOSE_TOO_BIG)
                self._refresh_browsers_tool()

    def _note_extension_activity(self):
        self.extension_last_activity = datetime.now()

    async def handle_extension_message(self, message: str, websocket=None):
        """Entry point for one frame from an extension; the dispatch is in ws_rpc.py."""
        await self.ws_rpc.handle_message(message, websocket)

    # ---- Connection diagnostics -------------------------------------------------

    # ---- Client tracking, call telemetry and the reports ------------------------------
    # The state lives on this object (http_clients, launcher_versions, telemetry); the logic
    # is in client_identity.py, call_telemetry.py and reports.py. What stays here is the
    # surface that http_endpoints, mcp_tools and the WebSocket dispatch call.

    def record_call_outcome(self, tool_name, outcome, call=None):
        """File a finished call's verdict on its own record (call_telemetry.py)."""
        self.telemetry.record_call_outcome(tool_name, outcome, call)

    def track_http_client(self, session_id, client, client_pid=None):
        """Record/update an MCP HTTP client (called from the ASGI tracking wrapper).

        client_pid: when set (forwarded by the stdio launcher via the
        X-BrowserPlug-Client-Pid header), it identifies the real Claude Code process
        behind the proxy. Without it we fall back to mapping the TCP peer port to
        a PID (correct for direct HTTP clients).
        """
        client_identity.track_http_client(self.http_clients, session_id, client, client_pid)

    def note_http_client(self, session_id, client, client_pid=None):
        """The loop-safe half of track_http_client: file the session's row and return it when
        its identity still has to be looked up (http_endpoints runs that off the loop)."""
        return client_identity.note_http_client(self.http_clients, session_id, client,
                                                client_pid)

    def identify_http_client(self, entry, client, client_pid=None):
        """The blocking half of track_http_client: resolve who is behind `entry`."""
        client_identity.identify_http_client(entry, client, client_pid)

    def record_launcher_version(self, launcher_pid, version):
        """Remember which build a launcher process is running (from its /health poll)."""
        client_identity.record_launcher_version(self.launcher_versions, launcher_pid, version)

    def launcher_version(self, launcher_pid):
        """The version a launcher last reported, or None (never reported / gone quiet)."""
        return client_identity.launcher_version(self.launcher_versions, launcher_pid)

    def record_launcher_timing(self, report):
        """Merge a launcher's stdio-side report (POST /timing) into the current call."""
        return self.telemetry.record_launcher_timing(report)

    def _record_from_body(self, sid, body):
        """Record any tools/call in a JSON-RPC request body; True when one was recorded."""
        return self.telemetry.record_from_body(sid, body)

    def _note_mcp_http_done(self, sid):
        """Stamp the end of the HTTP response for the current MCP call."""
        self.telemetry.note_mcp_http_done(sid)

    def get_health_info(self):
        """Health snapshot served at GET /health and embedded in the duplicate-conflict message."""
        return self.reports.health()

    def get_connections_info(self, kilo_details=True, viewer_host=None):
        """Snapshot of everything connected to this server. Sync; safe from any thread.

        kilo_details: when False, skip reading other agents' logs and transcripts for
        provider/model. The stored switch (usage_panels.enabled) applies as well.
        """
        return self.reports.connections(kilo_details=kilo_details, viewer_host=viewer_host)

    def _begin_restart(self, trigger):
        """Start the graceful restart sequence; callable from ANY thread (lifecycle.py).

        Raises lifecycle.RestartInProgress when one is already running; otherwise returns
        the verdict future (None = replacement confirmed, str = abandoned)."""
        return self.lifecycle.begin_restart(trigger)

    def _restart_onto(self, host):
        """Restart into a process that binds `host`. Its own method so a test can stub the
        one call that would otherwise take the test runner down with os._exit()."""
        self.lifecycle.restart_onto(host)

    def _wrap_client_tracking(self, asgi_app):
        """Wrap the MCP ASGI app: peer gate, route table, MCP session tracking.

        The substance lives in http_endpoints.py.
        """
        guard = host_guard.HostGuard(extra_hosts=lambda: (self.ws_host,))
        endpoints = http_endpoints.HttpEndpoints(
            self, asgi_app, is_localhost_bind=is_localhost_bind, host_check=guard.check,
            host_check_async=guard.check_async,
            on_loop=lambda loop: self.lifecycle.attach_mcp_loop(loop))

        async def wrapped(scope, receive, send):
            await endpoints.handle(scope, receive, send)

        return wrapped

    async def send_request_and_wait(self, request: Dict[str, Any], timeout: float = 30.0,
                                    browser: 'browser_identity.ExtBrowser' = None) -> Dict[str, Any]:
        """Send a request to a browser extension and wait for its response. This is the
        call the tool layer makes; the substance is in extension_rpc.py."""
        return await self.rpc.send_request_and_wait(request, timeout=timeout, browser=browser)

    def _notify_connection_waiters(self):
        """Notify all futures waiting for a connection"""
        waiters, self._connection_waiters = self._connection_waiters, []
        for future in waiters:
            # Per waiter: one already-settled future (InvalidStateError) or a closed loop
            # must not starve the rest, nor abort the connection handler that called us.
            try:
                if not future.done():
                    future.set_result(True)
            except Exception:
                logger.warning("Could not notify a connection waiter", exc_info=True)

    async def wait_for_extension_connection(self, timeout: float = 30.0) -> bool:
        """
        Wait until ANY browser extension is connected.

        Args:
            timeout: Maximum time to wait for connection in seconds

        Returns:
            bool: True if connection was established, False if timeout occurred

        Example:
            # Wait for Firefox extension to connect
            connected = await server.wait_for_extension_connection(timeout=10.0)
            if connected:
                print("Extension connected!")
            else:
                print("Connection timeout")
        """
        # If already connected, return immediately
        if any(b.connected for b in self.registry.by_slot()):
            return True

        # Create a future to wait for connection
        connection_future = asyncio.Future()
        self._connection_waiters.append(connection_future)

        try:
            # Wait for connection with timeout
            await asyncio.wait_for(connection_future, timeout=timeout)
            return True
        except asyncio.TimeoutError:
            # Remove the future from waiters if it timed out
            if connection_future in self._connection_waiters:
                self._connection_waiters.remove(connection_future)
            return False
        except Exception:
            # Remove the future from waiters on any other error
            if connection_future in self._connection_waiters:
                self._connection_waiters.remove(connection_future)
            return False

    async def start_mcp_server(self):
        """Start the MCP HTTP thread (lifecycle.py)."""
        await self.lifecycle.start_mcp_server()

    async def shutdown(self, server_task):
        """Gracefully shut the server and its task down (lifecycle.py)."""
        await self.lifecycle.shutdown(server_task)

    async def start_server(self):
        """Start both WebSocket and MCP servers (lifecycle.py)."""
        await self.lifecycle.start_server()


def main():
    """Entry point for `python server/server.py` and `python -m server`: hand the class to
    lifecycle.run(), which sets up logging, claims the instance and serves."""
    lifecycle.run(BrowserPlugServer)


if __name__ == "__main__":
    main()

