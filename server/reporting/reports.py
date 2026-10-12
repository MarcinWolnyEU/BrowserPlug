"""The two reports the popup, the options page and the launcher read.

`Reports.health()` is the body of GET /health. `Reports.connections()` is the payload of
`server.get_connections` and of the connections block inside /health: one row per server,
browser and MCP client, plus the per-browser instance list, the last command with its hop
timings, the concurrency block and the version-skew warning.

Both are built from a ReportSources, a bundle of callables the server fills in with
lambdas that read its attributes when a report is built, so a test that replaces an
attribute on the server instance is seen here. Pure helpers (client naming, argument
formatting, the version comparison) are imported directly. The builders are synchronous
and safe to call from any thread. A serving loop must not call them directly: they walk the
TCP table and the process tree. `SnapshotCache` runs a builder on a small thread pool and
keeps the result for a couple of seconds.

Nothing here imports server.py, and importing the module has no side effects.
"""

import asyncio
import logging
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from browserplug_shared import server_env
from browserplug_shared.mcp_port import HEALTH_SERVICE
from browserplug_shared.version import __version__ as BROWSERPLUG_VERSION
from browserplug_shared.ws_token import WS_HOST_ENV

from server.reporting import call_telemetry, client_identity
from server.browsers import routing
from server.usage_panels import usage_panels
from server.browsers.request_queue import concurrency_info
from server.reporting.version_check import build_version_warning
from server.ws.ws_auth import AUTH_OFF, is_localhost_bind

logger = logging.getLogger(__name__)


def _rows(value) -> list:
    """The dict rows of an array off the wire, whatever junk the key actually holds."""
    if not isinstance(value, (list, tuple)):
        return []
    return [item for item in value if isinstance(item, dict)]


def summarize_connections(info) -> dict:
    """`get_connections_info()` reduced to what an AGENT can act on.

    The full payload measures ~9.4 KB, of which one MCP-client row is ~4.9 KB and its
    `sessions[].commands[]` replay alone is ~4.2 KB. That array exists for the
    extension popup's 2-second poll, not for a language model's context, so it is
    dropped here and kept behind server_info(verbose=True). Never raises: a partial
    or malformed payload degrades to empty lists, because the caller is a diagnostic.
    """
    if not isinstance(info, dict):
        return {"error": f"connection info unavailable (expected an object, got "
                         f"{type(info).__name__})"}
    conns = _rows(info.get("connections"))
    instances = {i.get("slot"): i for i in _rows(info.get("extension_instances"))}

    server_row = next((c for c in conns if c.get("kind") == "server"), None)
    browsers = []
    for c in conns:
        if c.get("kind") != "extension":
            continue
        inst = instances.get(c.get("slot")) or {}
        browsers.append({
            "slot": c.get("slot"),
            "label": c.get("label"),
            "pid": c.get("pid"),
            "extension_version": inst.get("version"),
            "windows": inst.get("windows"),
            "tabs": inst.get("tabs"),
            "is_remote": c.get("is_remote"),
            "last_activity_hms": c.get("activity_hms"),
        })
    clients = [{
        "friendly": c.get("friendly"),
        "pid": c.get("pid"),
        "cwd": c.get("cwd"),
        "launcher_pid": c.get("launcher_pid"),
        "sessions": c.get("session_count"),
        "last_activity_hms": c.get("activity_hms"),
    } for c in conns if c.get("kind") == "mcp_client"]

    return {
        # get_connections_info() spells it generated_hms; get_health_info() re-exports
        # the same rows under generated_at. Accept either, so ONE psutil sweep can feed
        # both this summary and the checks.
        "generated_hms": info.get("generated_hms") or info.get("generated_at"),
        "server": ({"pid": server_row.get("pid"), "version": server_row.get("version"),
                    "detail": server_row.get("detail")} if server_row else None),
        "browsers": browsers,
        "mcp_clients": clients,
        "version_warning": info.get("version_warning"),
        "note": "compact view — server_info(verbose=true) returns the full roster "
                "(per-session command history, browser state, timings)",
    }


@dataclass(frozen=True)
class ReportSources:
    """The server state the reports read. Each field is a zero-argument callable except
    where the name says otherwise (`launcher_version`, `routed_browser_for_client`)."""
    # Call records
    last_command_info: Callable[[], Any]
    # The extension the legacy scalar fields describe (the default browser)
    extension_connection: Callable[[], Any]
    extension_version: Callable[[], Any]
    extension_file_access: Callable[[], Any]
    extension_proxy_access: Callable[[], Any]
    proxy_tools_enabled: Callable[[], bool]
    tools_signature: Callable[[], Any]
    # The registry and routing
    browsers_by_slot: Callable[[], Any]
    preferred_browser: Callable[[], Any]
    routed_browser_for_client: Callable[[Any], Any]
    browser_selection: Callable[[], dict]
    default_browser_pref: Callable[[], Any]
    # Clients and launchers
    http_clients: Callable[[], dict]
    launcher_version: Callable[[Any], Any]
    connected_browser_pids: Callable[[], Any]
    request_queue: Callable[[], Any]
    # The server itself
    server_start_time: Callable[[], datetime]
    extension_last_activity: Callable[[], Any]
    ws_port: Callable[[], int]
    mcp_port: Callable[[], int]
    ws_host: Callable[[], str]
    ws_host_source: Callable[[], str]
    ws_mode: Callable[[], str]
    ws_auth_mode: Callable[[], str]
    ws_token_source: Callable[[], str]
    ws_token_endpoint: Callable[[], Any]
    lan_addresses: Callable[[], Any]
    ws_lan_addresses: Callable[[], Any]
    # The connections report, which /health embeds. A hook rather than a direct call so a
    # test that replaces get_connections_info on the server is seen by both.
    connections_info: Callable[[], dict]
    # The newest restart's record (trigger, time, outcome, reason) or None; see
    # ServerLifecycle.last_restart.
    last_restart: Callable[[], Any] = lambda: None


#: Host characters only (letters, digits, dot, dash, underscore, colon and the
#: brackets of an IPv6 literal), length-capped. `viewer_host` reaches us from an
#: extension, which may be a REMOTE browser we do not trust, and it is rendered in
#: every other browser's popup, so it is validated rather than echoed.
SAFE_VIEWER_HOST = re.compile(r"^[A-Za-z0-9._:\[\]-]{1,80}$")


def safe_viewer_host(value):
    """The host to print in the server row: the caller's own when it looks like a
    host, else localhost. A server row reading `ws://localhost:8772` is a lie to a
    browser on a phone, which reached this server at a LAN address and can never
    reach it at localhost; only the caller knows which address it dialled."""
    if not isinstance(value, str):
        return "localhost"
    value = value.strip()
    return value if SAFE_VIEWER_HOST.match(value) else "localhost"


def version_parties(conns, instances):
    """Who to compare against this server's version, in the shape version_check wants.

    Browsers come from the registry (their EXTENSION version); launchers from the MCP
    client rows. A launcher with a pid but no reported version is flagged
    `unversioned`; it predates version reporting, which is itself the mismatch.
    """
    # `friendly` (the edition) when a pid follows it anyway; `name` embeds the pid
    # and would stutter ("Zen Browser pid 38288 (PID 38288)"). A REMOTE browser has
    # no pid and a generic edition, so there `name` (remote-<addr>) is what identifies it.
    parties = [{"kind": "browser",
                "name": (i.get("friendly") if i.get("pid") else i.get("name")),
                "pid": i.get("pid"), "version": i.get("version")}
               for i in instances
               if i.get("currently_connected") and i.get("version")]
    seen = set()
    for row in conns:
        pid = row.get("launcher_pid")
        if row.get("kind") != "mcp_client" or not pid or pid in seen:
            continue
        seen.add(pid)
        parties.append({"kind": "launcher", "name": row.get("friendly"), "pid": pid,
                        "version": row.get("launcher_version"),
                        "unversioned": not row.get("launcher_version")})
    return parties


#: How long a served snapshot stays valid. The popup polls every 2 s and each launcher every
#: 3 s, so this collapses a burst of pollers into one build without making the popup lag.
SNAPSHOT_TTL_SECONDS = 2.0

_snapshot_pool = None
_snapshot_pool_lock = threading.Lock()


def snapshot_pool():
    """The pool report builders run on. Small on purpose: a build is a burst of psutil
    calls, and two at once is already more than the box needs."""
    global _snapshot_pool
    with _snapshot_pool_lock:
        if _snapshot_pool is None:
            _snapshot_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="reports")
        return _snapshot_pool


class SnapshotCache:
    """Build a report off the event loop, keep it for `ttl` seconds, build it once.

    `await cache.get(key, build)` returns the cached value for `key` while it is fresh,
    otherwise runs `build()` on the pool. Callers that arrive while a build runs wait for
    it and share its result (single flight), whichever event loop they sit on: the MCP
    loop and the WebSocket loop share one cache and each waits without blocking. A build
    that raises is not cached; every waiter sees the exception.
    """

    def __init__(self, ttl=SNAPSHOT_TTL_SECONDS, clock=time.monotonic):
        self.ttl = ttl
        self._clock = clock
        self._entries = {}                 # key -> (built_at, value)
        self._entries_lock = threading.Lock()
        self._build_lock = threading.Lock()

    def _fresh(self, key):
        with self._entries_lock:
            entry = self._entries.get(key)
        if entry is not None and self._clock() - entry[0] < self.ttl:
            return True, entry[1]
        return False, None

    def _compute(self, key, build):
        # Runs on a pool thread. The second look under the build lock is what makes the
        # callers that queued behind a build take its result instead of building again.
        with self._build_lock:
            hit, value = self._fresh(key)
            if hit:
                return value
            value = build()
            with self._entries_lock:
                self._entries[key] = (self._clock(), value)
                if len(self._entries) > 16:
                    self._entries.pop(next(iter(self._entries)))
            return value

    async def get(self, key, build):
        hit, value = self._fresh(key)
        if hit:
            return value
        return await asyncio.get_running_loop().run_in_executor(
            snapshot_pool(), self._compute, key, build)


def file_access_report(browser, efa):
    """The `extension_file_access` object for one browser record and its hello's fileAccess.

    can_open_file_url is NOT a constant, and calling it one was wrong. No GECKO browser
    lets an extension navigate a tab to file://; always "Illegal URL", re-confirmed on
    Firefox 153 on 2026-08-03, where the refusal creates no tab either. Chromium DOES
    allow it once the per-extension file-access toggle is on, so a flat false told a
    Chrome agent its capability was missing when it merely needed a setting. `browser` is
    the registry record (it has `engine`); a socket does not.
    """
    efa = efa or {}
    return {
        "can_open_file_url": (getattr(browser, "engine", None) == "blink"
                              and bool(efa.get("can_read_files"))),
        # Whether the extension can read a file:// tab the user opened.
        "can_read_opened_file_tab": efa.get("can_read_files"),
    }


class Reports:
    def __init__(self, sources: ReportSources):
        self._s = sources

    def health(self):
        """Health snapshot served at GET /health and embedded in the duplicate-conflict message."""
        s = self._s
        now = datetime.now()

        def fmt(dt):
            return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else None

        last_command = s.last_command_info()

        ext = s.extension_connection()
        # The browser an unrouted client lands in. `ext` above is its SOCKET, which has no
        # `engine`; reading it there is what kept can_open_file_url false for Chrome.
        routed = s.preferred_browser()

        # The connected extension reports {can_read_files, browser_major} in its
        # extension_hello (see background.js getFileAccessState). Present file
        # access as a self-documenting object, and surface the Firefox major
        # version next to the other Firefox info.
        efa = s.extension_file_access() or {}
        file_access = None
        if s.extension_file_access() is not None:
            file_access = file_access_report(routed, efa)
        browser_major = efa.get("browser_major")

        # Optional "proxy" WebExtension permission (see background.js
        # getProxyPermissionState). has_proxy_permission gates the `proxy` arg on
        # the tabs/navigate tools; proxy_tools_exposed echoes what the tool schema
        # currently reflects. tools_signature changes whenever that schema flips,
        # so the launcher can push tools/list_changed to connected clients.
        epa = s.extension_proxy_access() or {}
        proxy_access = None
        if s.extension_proxy_access() is not None:
            proxy_access = {
                "has_proxy_permission": epa.get("has_proxy_permission"),
                "proxy_tools_exposed": s.proxy_tools_enabled(),
            }

        conn_info = s.connections_info()

        # The browser registry. Every scalar above/below is the DEFAULT browser's value,
        # kept as an alias so existing consumers (popup, launcher, tests) read the same
        # thing they always did; this array is the whole truth.
        default = s.preferred_browser()
        browsers = [{
            "slot": b.slot,
            "name": b.name,
            "instance_id": b.instance_id,
            "provisional": b.provisional,
            # Which browser this really is, against what it CLAIMS to be in
            # `browser_info` below (a fork says "Firefox" there, but adds a key of its
            # own, which is where fork_version comes from).
            "brand": b.family,
            "edition": b.edition,
            # Engine ("gecko"/"blink"/None) so the options pages can badge Blink vs
            # Gecko; brand/edition/fork_version are already engine-aware above.
            "engine": b.engine,
            "fork_version": b.fork_version,
            "probe_brand": b.brand,
            "exe_path": b.exe_path,
            "version": b.version,
            "connected_at": fmt(b.connected_at),
            "last_activity": fmt(b.last_activity),
            "browser_pid": b.browser_pid,
            "firefox_pid": b.browser_pid,      # legacy alias of browser_pid; no reader left
            "remote_addr": b.remote_addr,
            "is_remote": b.is_remote,
            "platform": b.platform,
            "browser_info": b.browser_info,
            # Per-browser grants. The scalars above are the DEFAULT browser's; the
            # `proxy` tool arg, by contrast, is aggregate (shown when ANY browser holds
            # the grant), so only this list explains a mismatch between the two.
            "file_access": b.file_access,
            "proxy_access": b.proxy_access,
            # Probed platform APIs. null = the extension predates the probe (unknown),
            # NOT "unsupported"; the two must stay distinguishable here, because this is
            # what tells you whether a documentId-dependent path can run at all.
            "api_support": b.api_support,
            "is_default": b is default,
        } for b in s.browsers_by_slot() if b.connected]

        report = {
            "status": "ok",
            # Identity. A client that seeks a relocated server walks 2830..2942 and must
            # be able to tell OUR server from a stranger's HTTP server squatting on one of
            # those ports: "something answered" is not identification. Never remove this
            # field or rename its value: launcher.py (and any other seeker) keys on it.
            "service": HEALTH_SERVICE,
            "server_pid": os.getpid(),
            # This server's own build (shared-py/browserplug_shared/version.py; the launcher shares it), and
            # the plain-text warning naming every connected browser/launcher whose build
            # differs. version_warning is null when everything is in lockstep.
            "server_version": BROWSERPLUG_VERSION,
            "version_warning": conn_info.get("version_warning"),
            "started": fmt(s.server_start_time()),
            "uptime_seconds": int((now - s.server_start_time()).total_seconds()),
            "ws_port": s.ws_port(),
            # Where the extension WebSocket is bound and how it authenticates. The token
            # VALUE is a secret: it is never reported here; only at GET /ws-token, which
            # is localhost-only. ws_auth_required/ws_auth_mode describe a LOCAL peer; a
            # remote one is always "strict" (WsAuth.auth_mode()).
            "ws_host": s.ws_host(),
            # Where a browser on ANOTHER machine would dial this server, and whether the
            # WS is actually listening for one (it binds localhost unless BROWSERPLUG_WS_HOST
            # says otherwise). The options page turns these into copy-paste setup steps.
            "ws_lan_urls": [f"ws://{ip}:{s.ws_port()}" for ip in s.lan_addresses()],
            # The same addresses with their adapters named ({ip, interface, recommended},
            # 192.168.* first); the options page's LAN panel labels each candidate so
            # the user can tell the real NIC from the WSL/Hyper-V/Docker virtual ones.
            "ws_lan_addresses": s.ws_lan_addresses(),
            "ws_accepts_remote": not is_localhost_bind(s.ws_host()),
            # local | lan | wan; which panel of the options page describes this server.
            # Local is derived from the bind; LAN vs WAN is the user's persisted choice.
            "ws_mode": s.ws_mode(),
            "ws_host_env": WS_HOST_ENV,
            # arg | env | file | default; the options page needs this to know whether its
            # "allow remote browsers" button can change the bind (an env var always wins).
            "ws_host_source": s.ws_host_source(),
            "ws_auth_required": s.ws_auth_mode() != AUTH_OFF,
            "ws_auth_mode": s.ws_auth_mode(),
            "ws_token_source": s.ws_token_source(),
            "ws_token_endpoint": s.ws_token_endpoint(),
            "mcp_port": s.mcp_port(),
            "browsers": browsers,
            "browsers_count": len(browsers),
            # Which browser a client with no selection of its own reaches, and whether
            # the server may start it. `is_default` above is who that resolves to NOW;
            # this is the stored preference, which outlives the browser being closed.
            "default_browser": s.default_browser_pref(),
            "extension_connected": ext is not None and getattr(ext, "close_code", None) is None,
            "extension_version": s.extension_version() or "unknown",
            "extension_file_access": file_access,
            "extension_proxy_access": proxy_access,
            "tools_signature": s.tools_signature(),
            "browser_pids": (browser_pids := s.connected_browser_pids()),
            # Legacy alias: older options pages read this key. One scan feeds both.
            "firefox_pids": browser_pids,
            "browser_major": browser_major,
            # list(): the table grows on the uvicorn thread while this runs on another,
            # and walking the live view raises "changed size during iteration".
            "client_pids": sorted({
                info.get("pid") for info in list(s.http_clients().values())
                if info.get("pid") and info.get("last_activity")
                and info["last_activity"].timestamp() > now.timestamp() - 300
            }),
            # Full connection roster (same view the extension popup renders): the
            # server/extension/MCP-client rows with friendly names, cwd, sessions
            # and recent commands. The report is built off the loop and cached
            # for a moment (SnapshotCache), one table scan per build.
            "connections": conn_info.get("connections"),
            "connections_count": conn_info.get("count"),
            # extension_instances is the browser registry; several browsers are a
            # supported state, not a conflict.
            "extension_instances": conn_info.get("extension_instances"),
            "last_command": last_command,
            # The newest restart, so an abandoned one is visible after the request that
            # asked for it has been answered: {trigger, time, outcome, reason}.
            "last_restart": s.last_restart(),
            "generated_at": fmt(now),
            # Server-clock epoch to pair with connections[].activity_epoch (the options
            # page's connection map ages activity without touching the client clock).
            "generated_epoch": now.timestamp(),
        }
        # Only when it is true, so a healthy report keeps its exact shape: the environment
        # this server runs from lost its pyvenv.cfg (a `uv cache prune` under it).
        damage = server_env.running_env_damage()
        if damage:
            report["server_env_damaged"] = damage
        return report

    def connections(self, kilo_details=True, viewer_host=None):
        """Snapshot of everything connected to this server. Sync; safe from any thread.

        kilo_details: when False, skip reading other agents' logs and transcripts for
        provider/model. The stored switch (usage_panels.enabled) applies as well, so
        callers that pass nothing, /health and server_info, obey it too.
        """
        s = self._s
        now = datetime.now()

        def fmt(dt):
            return dt.strftime("%H:%M:%S") if dt else None

        def fmt_since(dt):
            # Process creation time. Same-day → HH:MM:SS; an earlier day → full
            # date (YYYY-MM-DD HH:MM) so a long-lived process isn't ambiguous.
            if not dt:
                return None
            return (dt.strftime("%H:%M:%S") if dt.date() == now.date()
                    else dt.strftime("%Y-%m-%d %H:%M"))

        conns = []
        host = safe_viewer_host(viewer_host)

        # The server process itself
        srv = client_identity.describe_pid(os.getpid())
        conns.append({
            "kind": "server",
            "label": "BrowserPlug server",
            "pid": os.getpid(),
            # This build, for the options page's map + the version-skew comparison below.
            "version": BROWSERPLUG_VERSION,
            "exe_path": srv.get("exe_path") or sys.executable,
            "activity_hms": fmt(now),
            "activity_epoch": now.timestamp(),
            "since_hms": fmt_since(srv.get("start_time")),
            "detail": (f"ws://{host}:{s.ws_port()}  +  http://{host}:{s.mcp_port()}/mcp"),
        })

        # One row per connected Firefox (the popup renders the rows as the server builds
        # them; same shape as before, just possibly several). Identity is cached on the
        # registry entry at connect, so this no longer re-scans psutil on every poll.
        browsers = [b for b in s.browsers_by_slot() if b.connected]
        for b in browsers:
            # The NAME, and nothing else. The slot rides the row's own `slot` field (the
            # popup paints it on the icon) and the pid rides `pid` (under the activity
            # time), so neither is crammed in here to be read back out later. "Remote"
            # stays: it is a fact about the browser, and no column of the panel carries
            # it. Example: `[Remote] Firefox for Android Nightly`.
            label = f"[Remote] {b.display_name}" if b.is_remote else b.display_name
            detail = f"WebSocket: {s.ws_port()}"
            if b.is_remote and b.remote_addr:
                detail += f" · from {b.remote_addr}"
            act = b.last_seen or s.extension_last_activity() or now
            conns.append({
                "kind": "extension",
                "label": label,
                # slot correlates this row with its extension_instances entry (pid
                # can't: a REMOTE browser has none). is_remote/remote_addr let the
                # options page's connection map draw remote browsers distinctly.
                "slot": b.slot,
                "is_remote": b.is_remote,
                "remote_addr": b.remote_addr,
                "pid": b.browser_pid,
                "exe_path": b.exe_path,
                # What IDENTIFIES this browser, as opposed to the process running it.
                # A remote one has no pid, no exe path and no start time here, so its
                # own hello is the whole answer to "which browser is that?"; the popup's
                # tooltip and its "Copy connection info" both print these, which is what
                # a person pastes when the wrong browser answered.
                "engine": b.engine,
                "platform": b.platform,
                "browser_info": b.browser_info,
                # The EXTENSION's build, the same number /health compares for version
                # skew; a browser answering oddly is often one running older code.
                "version": b.version,
                "activity_hms": fmt(act),
                "activity_epoch": act.timestamp(),
                "since_hms": fmt_since(b.proc_started),
                "detail": detail,
            })

        # MCP HTTP clients; drop ones idle > 5 min, then collapse sessions from
        # the same process (PID) into one entry, keeping per-session detail.
        cutoff = now.timestamp() - 300
        by_pid = {}
        http_clients = s.http_clients()
        for sid, info in list(http_clients.items()):
            la = info.get("last_activity")
            if not la or la.timestamp() < cutoff:
                http_clients.pop(sid, None)
                continue
            key = info.get("pid") if info.get("pid") is not None else f"sid:{sid}"
            agg = by_pid.get(key)
            if agg is None:
                agg = by_pid[key] = {
                    "pid": info.get("pid"),
                    "friendly": info.get("friendly") or "MCP client",
                    "cwd": info.get("cwd"),
                    "exe_path": info.get("exe_path"),
                    "cmdline": info.get("cmdline"),
                    "start_time": info.get("start_time"),
                    "launcher_pid": info.get("launcher_pid"),
                    "launcher_start_time": info.get("launcher_start_time"),
                    "last_activity": la,
                    "sessions": [],
                    "client_keys": [],
                }
            if info.get("client_key"):
                agg["client_keys"].append(info["client_key"])
            if la.timestamp() > agg["last_activity"].timestamp():
                agg["last_activity"] = la
            agg["cwd"] = agg["cwd"] or info.get("cwd")
            agg["exe_path"] = agg["exe_path"] or info.get("exe_path")
            agg["cmdline"] = agg["cmdline"] or info.get("cmdline")
            agg["start_time"] = agg["start_time"] or info.get("start_time")
            agg["launcher_pid"] = agg["launcher_pid"] or info.get("launcher_pid")
            agg["launcher_start_time"] = (agg["launcher_start_time"]
                                          or info.get("launcher_start_time"))
            if agg["friendly"] in (None, "MCP client") and info.get("friendly"):
                agg["friendly"] = info.get("friendly")

            commands = []
            for h in info.get("history", [])[-10:]:
                commands.append({
                    "time_hms": h["time"].strftime("%H:%M:%S"),
                    "tool": h.get("tool"),
                    "params": call_telemetry.format_args(h.get("args")),
                })
            commands.reverse()  # most recent first
            agg["sessions"].append({
                "id": str(sid)[:8],
                "last_activity_hms": la.strftime("%H:%M:%S"),
                "commands": commands,
            })

        for agg in by_pid.values():
            friendly = agg["friendly"] or "MCP client"
            cwd = agg["cwd"]
            la = agg["last_activity"]
            st = agg.get("start_time")
            n = len(agg["sessions"])
            # "since" is rendered uniformly by the popup from `since_hms` (with a
            # tooltip), for this row and the server/extension rows alike, so it's
            # no longer baked into `detail` here.
            # The pid is a row field the popup renders under the activity time, the
            # same place every other kind of row shows it; it is not repeated here.
            detail = f"{n} session" + ("s" if n != 1 else "")
            # Which browser this client's tool calls actually reach (routing.client_key
            # keys the selection by pid). None = nothing connected, or its selection points
            # at a browser that left, in which case its calls ERROR rather than route
            # somewhere else, so the map must draw no routing line at all.
            # A session's key is fixed at its first tool call (routing.client_key stores it
            # on the entry); read that back rather than rebuilding it, and rebuild only for
            # a client that has not called a tool yet.
            selected = s.browser_selection()
            client_key = (next((k for k in agg["client_keys"] if k in selected), None)
                          or (agg["client_keys"][0] if agg["client_keys"] else None)
                          or (routing.pid_client_key(agg["pid"], agg.get("start_time"))
                              if agg["pid"] is not None else None))
            routed = s.routed_browser_for_client(client_key) if client_key else None
            entry = {
                "kind": "mcp_client",
                "label": f"{friendly} — {cwd}" if cwd else friendly,
                "friendly": friendly,
                # Who ships this client. None for anything we could not name, which is
                # every unrecognized row, so the popup must treat it as optional prose,
                # never as something to lay out around.
                "vendor": client_identity.client_vendor(friendly),
                "cwd": cwd,
                "pid": agg["pid"],
                # The stdio proxy between this client and the server, when there is
                # one (None = a direct HTTP client, e.g. GitLab Duo). The options
                # page's connection map draws it as its own hop.
                "launcher_pid": agg.get("launcher_pid"),
                # What that launcher process reports it is running (None = it never said,
                # i.e. a launcher older than version reporting; see the skew warning).
                "launcher_version": s.launcher_version(agg.get("launcher_pid")),
                "launcher_since_hms": fmt_since(agg.get("launcher_start_time")),
                # The browser this client is routed to, and whether that was an explicit
                # browsers(action="use") or just the default; the connection map draws it.
                "target_browser_slot": routed.slot if routed is not None else None,
                "target_browser_explicit": bool(client_key
                                                and client_key in s.browser_selection()),
                "exe_path": agg["exe_path"],
                # What the process was STARTED with (secret-looking values masked; see
                # client_identity.sanitized_cmdline). The popup copies it, because for a client running
                # under a generic interpreter it is the only field that identifies it —
                # and an unrecognized row is exactly the one somebody copies.
                "cmdline": agg["cmdline"],
                "since_hms": fmt_since(st),
                "started_hms": st.strftime("%H:%M:%S") if st else None,
                "session_count": n,
                "sessions": agg["sessions"],
                "activity_hms": fmt(la),
                "activity_epoch": la.timestamp(),
                "detail": detail,
            }
            conns.append(entry)

        # Agent CLIs that leave their current provider/model in local state (Kilo, pi,
        # Copilot Chat). usage_panels owns the detectors and the switch; with the switch
        # off, or `kilo_details` False, it touches nothing and the rows stay as built.
        usage_panels.snapshot(conns, allowed=kilo_details)

        # --- Per Firefox-instance view -------------------------------------------
        # The browser registry. Several browsers are a supported state and never
        # displace each other, so no conflict flag is emitted.
        def _state(b, key):
            return (b.browser_state or {}).get(key)

        instances = [{
            "slot": b.slot,
            "pid": b.browser_pid,
            "name": b.name,
            "friendly": b.edition,
            # The EXTENSION's version (manifest.json), as distinct from everything else
            # here, which describes the BROWSER; the options page's map draws the two as
            # separate boxes and this is what tells them apart.
            "version": b.version,
            # What the browser holds right now (browser_state pushes; null until the
            # first one lands, and on an extension too old to send them).
            "windows": _state(b, "windows"),
            "tabs": _state(b, "tabs"),
            "active_tabs": _state(b, "active_tabs"),
            "loaded_tabs": _state(b, "loaded_tabs"),
            # One entry per window's foreground tab ({id, title, last_hms}); null on an
            # extension too old to send it, which the tooltip renders as nothing at all
            # rather than as "no tabs".
            "active_tab_list": _state(b, "active_tab_list"),
            "state_hms": fmt(b.browser_state_at),
            "exe_path": b.exe_path,
            "last_connect_hms": fmt(b.connected_at),
            "connect_count": b.connect_count,
            # The PREVIOUS drop, which outlives the reconnect that replaced it. Without
            # these three the only record of a browser going away was a log line, and a
            # log line is what nobody has when they ask the question.
            "last_disconnect_hms": fmt(b.last_disconnect_at),
            "last_close_code": b.last_close_code,
            "last_close_reason": b.last_close_reason,
            # Socket accept -> valid hello. A browser that takes seconds to answer is a
            # browser under load, which is a different problem from one that never
            # answers, and without this field the two look identical from here.
            "hello_latency_ms": b.hello_latency_ms,
            "encrypted": b.encrypted,
            # The extension's own account of its last gap (connection_log.clean_journal).
            "reported_journal": b.reported_journal,
            "last_activity_hms": fmt(b.last_activity) if b.last_action else None,
            # Epoch twin of last_activity_hms: REAL browser commands only (heartbeats
            # and the connections poll itself excluded), so the options page's map can
            # light an edge on actual traffic without lighting it on its own polling.
            "last_activity_epoch": (b.last_activity.timestamp() if b.last_action else None),
            "last_action": b.last_action,
            "remote_addr": b.remote_addr,
            "is_remote": b.is_remote,
            "currently_connected": b.connected,
        } for b in s.browsers_by_slot()]

        # generated_epoch pairs with the rows' activity_epoch: age = generated - activity
        # is computed entirely in SERVER clock, so a client (the options page's map)
        # never mixes its own clock into the arithmetic. last_command (with its
        # timing breakdown) rides along so the map can replay completed calls
        # without a second fetch.
        return {"count": len(conns), "generated_hms": fmt(now),
                "generated_epoch": now.timestamp(), "connections": conns,
                "last_command": s.last_command_info(),
                "extension_instances": instances,
                # The practical concurrency limit must be DISCOVERABLE rather than
                # something each operator infers from failure rates after the fact.
                # server_info already dumps this payload, so it costs no description
                # bytes; which matters, because the tool-description budget caps those.
                "browser_concurrency": concurrency_info(s.request_queue()),
                # Plain-text version skew across server/launchers/extensions, or None when
                # everything matches. Built from the rows above so it can never describe a
                # party this payload doesn't also list. /health re-exports it unchanged.
                "version_warning": build_version_warning(
                    BROWSERPLUG_VERSION, version_parties(conns, instances))}
