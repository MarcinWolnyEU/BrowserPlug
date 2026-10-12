#!/usr/bin/env python3
"""MCP HTTP port policy: the ONE place that knows which ports BrowserPlug uses.

server.py (which binds) and launcher.py (which has to FIND an already-running server)
both import this, so the two can never disagree about the sequence again. The
3000-vs-3456 split this replaces was exactly that bug.

The rules:
  * The MCP HTTP port defaults to **2830**.
  * If 2830 is taken, the server walks **2831, 2832, … 2942**, strictly ascending,
    one port at a time, no gaps, no randomness, no wrap. A client that does not find
    the server on 2830 rediscovers it by walking the SAME sequence, so determinism is
    load-bearing here, not a style preference.
  * `BROWSERPLUG_MCP_PORT` pins the port explicitly: when it is set, nobody searches and
    nobody seeks. The server refuses to relocate a pinned port (`--mcp-port-pinned`).
  * The extension WebSocket port (8772) is a different resource and never moves.

Stdlib-only on purpose: the launcher must stay light (no websockets/uvicorn/mcp; test_import_boundaries enforces it).

Copyright 2026 Marcin Wolny
Licensed under the MIT License - see LICENSE file for details
"""
import errno
import json
import os
import socket
import urllib.request

MCP_PORT_ENV = "BROWSERPLUG_MCP_PORT"
MCP_PORT_DEFAULT = 2830
MCP_PORT_LAST = 2942  # inclusive: the last port the search/seek will ever touch

# /health names the service so a client can tell OUR server from a stranger's HTTP
# server squatting on a port in the range. "Something answered" is NOT identification.
HEALTH_SERVICE = "browserplug"

# /health's `status`. "degraded" means the server answered but could not build its full
# report (probes.health_body): still this server, so a seeker must accept it. Walking
# past it would spawn a duplicate that dies on the busy WebSocket port.
HEALTH_STATUS_OK = "ok"
HEALTH_STATUS_DEGRADED = "degraded"

# Added by seek_browserplug to the /health it returns: the port that answered. Not a /health
# field; the underscore keeps it from ever colliding with one.
PROBED_PORT_KEY = "_probed_port"

# Per-port timeout while seeking. It is only ever paid on a port that HAS a listener:
# ports with nothing on them are filtered out first, for free, by a bind probe
# (_seek_skippable). Do NOT reintroduce the assumption that a closed port refuses
# instantly. On Windows it does not, and that assumption cost a 93-second seek.
SEEK_TIMEOUT_SECONDS = 0.4


def mcp_port_sequence(start_port=MCP_PORT_DEFAULT, last_port=MCP_PORT_LAST):
    """The search order: start_port, start_port+1, …, last_port (inclusive)."""
    return range(start_port, last_port + 1)


def pinned_mcp_port(env=None):
    """The BROWSERPLUG_MCP_PORT pin as an int, or None. A pin disables search AND seek."""
    raw = (os.environ if env is None else env).get(MCP_PORT_ENV)
    if raw is None or str(raw).strip() == "":
        return None
    try:
        return int(str(raw).strip())
    except ValueError:
        return None


# Bind errors that mean "somebody else has this port" (WSAEACCES 10013 / WSAEADDRINUSE
# 10048 are the Windows spellings). Anything else from the IPv6 leg (no IPv6 stack,
# address unavailable) means the probe cannot speak, not that the port is taken.
_PORT_BUSY_ERRNOS = {errno.EADDRINUSE, errno.EACCES, 10013, 10048}


def _bind_targets(host):
    """The (family, address) pairs a listener on `host` would actually occupy.

    "localhost" resolves to BOTH 127.0.0.1 and ::1, and that is what asyncio/uvicorn bind.
    So a port is only really free when both families are free. Probing just IPv4 would
    hand out a port whose ::1 half is taken, and the real bind would then fail (fatally,
    for the standalone server). Same reasoning as tests/port_coordinator.is_port_free.
    """
    if host in ("localhost", "", None):
        targets = [(socket.AF_INET, "127.0.0.1")]
        if socket.has_ipv6:
            targets.append((socket.AF_INET6, "::1"))
        return targets
    family = socket.AF_INET6 if ":" in str(host) else socket.AF_INET
    return [(family, host)]


def port_is_bindable(port, host="localhost"):
    """Could we LISTEN on host:port right now? (bind-probe; the socket is closed again)

    The socket options are the whole point, and they differ per platform:

      * **Windows: SO_EXCLUSIVEADDRUSE, never SO_REUSEADDR.** A SO_REUSEADDR bind here
        SUCCEEDS on a port another process is actively listening on, as long as that
        listener also set SO_REUSEADDR (live-verified on Win11: first=REUSEADDR +
        second=REUSEADDR → the second bind wins and steals the port). websockets/uvicorn
        used to do exactly that, so a REUSEADDR probe reported an OCCUPIED port as FREE.
        That is how the test port coordinator handed out live ports, and how a squatter
        could bind OVER the server and impersonate it to the extension.
        SO_EXCLUSIVEADDRUSE refuses in every one of those cases and never lies.
      * **POSIX: SO_REUSEADDR**, which there means only "reuse a TIME_WAIT port" and
        can NOT bind over an active listener. Without it the probe would call a
        just-restarted server's own port busy (its TIME_WAIT sockets linger for minutes)
        and we would relocate for no reason. It is also exactly what asyncio sets when it
        really binds on POSIX, so the probe answers the question we are actually asking.
        (Windows has no such need: a listening bind there is not blocked by TIME_WAIT.)
    """
    for family, addr in _bind_targets(host):
        try:
            with socket.socket(family, socket.SOCK_STREAM) as sock:
                if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):  # Windows
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                else:  # POSIX
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind((addr, port))
        except OSError as e:
            if family == socket.AF_INET6 and e.errno not in _PORT_BUSY_ERRNOS:
                continue  # no IPv6 stack / address unavailable; not a "busy" answer
            return False
    return True


def find_mcp_port(start_port=MCP_PORT_DEFAULT, last_port=MCP_PORT_LAST, host="localhost"):
    """The first bindable port in start_port..last_port, ascending.

    Deterministic: every port is probed in order, none is skipped, and the search never
    wraps around. It raises at the end of the range so the failure is loud.
    """
    for port in mcp_port_sequence(start_port, last_port):
        if port_is_bindable(port, host):
            return port
    raise RuntimeError(
        f"No free MCP HTTP port in {start_port}-{last_port}: every port in the range is "
        f"in use (probed ascending, one at a time). Free one of them, or pin a port "
        f"explicitly with {MCP_PORT_ENV}=<port> / --mcp-port <port>."
    )


def connect_host(host):
    """The address to CONNECT to for a server that binds `host`.

    "localhost" resolves to ::1 first, but in LAN/WAN mode uvicorn binds 0.0.0.0 only, so
    every new connection paid a failed IPv6 attempt (about 2 s where the firewall drops
    the SYN) before falling back to IPv4. A server bound to "localhost" listens on
    127.0.0.1 too, so the literal is right in both modes. Bind probes keep "localhost"
    (both families); this is for URLs only.
    """
    return "127.0.0.1" if host in ("localhost", "", None) else host


def health_url(port, host="localhost"):
    return f"http://{connect_host(host)}:{port}/health"


def browserplug_health(port, timeout=SEEK_TIMEOUT_SECONDS, host="localhost"):
    """GET /health on this port, and return it ONLY if a BrowserPlug server answered.

    The identity check is `service == "browserplug"` plus a status of ok or degraded.
    "Something answered on 2830" is not identification: a stranger's dev server would
    otherwise be proxied to as if it were ours.

    Proxy-BLIND on purpose. urlopen's default opener honours HTTP_PROXY/http_proxy from
    the environment, so on a corporate-proxy machine with no NO_PROXY for localhost this
    probe was sent to the proxy, which cannot reach our loopback server. Every port in
    the range then reads as empty: the seek finds nothing, each launcher spawns a server
    that dies as a duplicate (:8772 is taken), and every client times out against a
    server that is up and healthy. That is the exact failure signature .tmp/dev-docs/ports.md records
    for the --mcp-port pin, reachable here without any pin at all.
    """
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(health_url(port, host), timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    if (data.get("service") != HEALTH_SERVICE
            or data.get("status") not in (HEALTH_STATUS_OK, HEALTH_STATUS_DEGRADED)):
        return None
    return data


# Hosts whose ports may be pre-filtered with a bind probe. For a real remote address a
# bind is either meaningless or, with IP_FREEBIND / ip_nonlocal_bind, able to SUCCEED
# on an address we do not own, which would skip past a live remote server. Only these.
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "", None})

# The wildcard addresses a listener can sit on. Probed per family in ADDITION to the
# loopback ones, because a wildcard listener serves 127.0.0.1 and ::1 without blocking a
# bind to either of them. "::" is harmless where there is no IPv6 stack: the bind fails
# with a non-busy errno, which port_is_bindable already reads as "not a busy answer".
_WILDCARD_HOSTS = ("0.0.0.0", "::")


def _seek_skippable(port, host):
    """True when `port` provably has NO listener, so its HTTP probe can be skipped.

    This is the difference between a failed seek costing milliseconds and costing a
    minute and a half. A closed port does not always refuse instantly: with Windows
    Firewall enabled (the default, DefaultInboundAction=NotConfigured => drop) the SYN is
    DROPPED rather than answered with an RST, on loopback too, so every closed port
    costs the full timeout, and it is paid TWICE because "localhost" resolves to both ::1
    and 127.0.0.1. Live-measured on this project's Win11 box: 0.82 s per closed port x
    113 ports = 93 s for one failed seek, against a 30 s client connect budget.

    That made the RECOVERY path the broken one, which is why it stayed latent for months:
    while a server was up on 2830 the seek stopped at the first probe and was instant. The
    moment no server was running, every launcher was killed mid-walk at 30 s, before ever
    reaching the spawn that would have brought one back. Up: fine. Down: unrecoverable.

    port_is_bindable answers the same question for free (a bind syscall, no network, 0.0 s
    measured). See its docstring for the SO_EXCLUSIVEADDRUSE reasoning. Fails SAFE:
    anything short of a confident "nothing is listening here" falls through to the real
    probe, which costs one HTTP timeout and never a wrong answer.

    THE LOOPBACK PROBE ALONE IS NOT ENOUGH, and an earlier version of this docstring
    claimed otherwise ("never reports an occupied port as free"). A listener on a WILDCARD
    address answers connections to 127.0.0.1 and ::1, but on Windows it does not stop a
    bind to those specific addresses: probing 127.0.0.1 succeeds while a server is serving
    on 0.0.0.0:port. Live-verified against this project's own server (bind 127.0.0.1:2830
    and ::1:2830 both succeeded; bind 0.0.0.0:2830 was refused; /health on 127.0.0.1:2830
    answered "ok" the whole time).

    That is reachable from the options page, not a corner case. mcp_bind_host() puts the
    MCP port wherever the WebSocket is, so switching to LAN or WAN mode moves the shared
    server to 0.0.0.0 -- and every launcher then skipped the live port, reported "no
    BrowserPlug server found", and spawned a duplicate that died on the busy :8772. The
    symptom is the one .tmp/dev-docs/ports.md already warns about for the retired --mcp-port pin
    (clients failing while /health answers fine), with no pin involved.

    So the wildcards are probed too. That can only turn a "free" verdict into "probe it
    properly", never the reverse, which keeps the fail-safe direction intact: an unrelated
    listener on some other interface costs this seek one HTTP probe.
    """
    if host not in _LOCAL_HOSTS:
        return False
    if not port_is_bindable(port, host):
        return False
    return all(port_is_bindable(port, host=wildcard) for wildcard in _WILDCARD_HOSTS)


def port_is_occupied(port, host="localhost"):
    """True when something may hold `port`: loopback or any wildcard bind is refused.

    The no-network counterpart of a connect probe. A refused connect to "localhost"
    costs ~2 s on Windows, so "is anything on this port" is answered by binding, which
    is free. Wildcards are included because a LAN-mode listener on 0.0.0.0 leaves a
    loopback bind succeeding. Unlike _seek_skippable this is not fail-safe toward
    probing: a remote `host` is bind-tested as given.
    """
    if not port_is_bindable(port, host):
        return True
    return not all(port_is_bindable(port, host=w) for w in _WILDCARD_HOSTS)


def seek_browserplug(start_port=MCP_PORT_DEFAULT, last_port=MCP_PORT_LAST,
                timeout=SEEK_TIMEOUT_SECONDS, host="localhost", accept=None):
    """Walk the range ascending and return (port, health) of the FIRST BrowserPlug server.

    (None, None) when the whole range holds no BrowserPlug. Stops at the first hit, so the
    common case, the server sitting on 2830, costs exactly one probe.

    Ports with no listener are skipped by a free bind probe before their HTTP probe is
    paid for (_seek_skippable). The walk ORDER is deliberately untouched: still strictly
    ascending, every port considered, no gaps, no randomness, because a client
    rediscovers a relocated server by replaying exactly this sequence. The skip removes
    cost, never a candidate.

    `accept(health)` lets a caller refuse a hit that identifies as BrowserPlug but is not
    the server it wants (the launcher uses it to skip another checkout's or another
    user's server); a refused port is walked past like any stranger. The health dict handed
    to `accept`, and returned, carries PROBED_PORT_KEY: the port the probe reached, which
    server_pid_is_ours uses to tie the pid /health names to the socket that answered.
    """
    for port in mcp_port_sequence(start_port, last_port):
        if _seek_skippable(port, host):
            continue
        health = browserplug_health(port, timeout=timeout, host=host)
        if health is None:
            continue
        health[PROBED_PORT_KEY] = port
        if accept is None or accept(health):
            return port, health
    return None, None


# --- Detached-child startup capture ------------------------------------------
# A detached server's stdout/stderr has to go SOMEWHERE or a crash before logging is
# configured (bad interpreter, import error, syntax error) leaves no trace at all and
# the launcher only ever reports a connect timeout.
#
# It must not go to BROWSERPLUG_LOG_FILE. The server's own RotatingFileHandler owns that
# path; a second life-long handle on it makes the rollover rename fail forever
# (WinError 32 on Windows), and each failed emit then dumps a handler traceback to
# stderr, back into the same file. Measured cost of getting this wrong: a 37 MB
# browserplug-server.log against a 2 MB cap, with 13k rotation-error blocks.
#
# Lives here because BOTH spawners need the identical answer: launcher.py's first
# spawn and server.py's /restart replacement. (Same reason the port policy lives here.)
def startup_log_path(log_file):
    """The sibling file a detached child's stdout/stderr is captured to."""
    base, _, _ = str(log_file).rpartition(".log")
    return (base or str(log_file)) + ".startup.log"


def spawn_startup_log_path(log_file, tag):
    """A launcher spawn's own capture file: `<base>.<tag>.startup.log`.

    Unique per spawn because two launchers starting together would open the one shared
    name with "w" and truncate each other's capture, which is the file that holds the
    only traceback of a server that dies before its logging is up. `tag` is
    `<launcher pid>-<epoch ms>`; `startup_log_glob` finds them all.
    """
    base, _, _ = str(log_file).rpartition(".log")
    return f"{base or str(log_file)}.{tag}.startup.log"


def startup_log_glob(log_file):
    """Glob naming every startup capture for `log_file`: the launcher's per-spawn files
    and the restart spawner's fixed one (`startup_log_path`)."""
    base, _, _ = str(log_file).rpartition(".log")
    return f"{base or str(log_file)}*.startup.log"


def restart_log_file(server_dir):
    """The log path /restart uses when BROWSERPLUG_LOG_FILE is unset; its startup
    capture is `startup_log_path()` of this. One definition so lifecycle, the launcher's
    error text and preflight cannot name different files."""
    return os.path.join(server_dir, "browserplug-restart.log")


def restart_startup_log_glob(server_dir):
    """Glob naming the captures of a /restart replacement when BROWSERPLUG_LOG_FILE is
    unset: lifecycle names them `<restart_log_file base>.restart-<pid>-<ms>.startup.log`."""
    return startup_log_glob(restart_log_file(server_dir))


LOG_FILE_ENV = "BROWSERPLUG_LOG_FILE"


# The log file a server writes to when nothing configured one. It exists because an
# UNSET BROWSERPLUG_LOG_FILE used to mean "no file logging at all", and /restart copies
# the environment verbatim, so one server started without it left the whole lineage
# logging nowhere for as long as it lived. Live-caught 2026-08-05: the running server had
# no rotating handler, and because the stderr-handler removal sits INSIDE the block that
# only runs when the variable is set, every record went to the detached child's stdout
# capture instead. That is the .startup.log sibling, which has no rotation and is meant to
# hold one server's startup rather than its history; it was at 136 KB and climbing, with
# no timestamps, while the file it should have been rotating did not exist.
#
# Same failure shape as the retired `--mcp-port 3456` pin: a value absent at the head of
# the lineage survives every restart, and the symptom (no logs anywhere) shows up long
# after the cause. So the default lives HERE, next to the port policy and startup_log_path,
# because both spawners and the server itself must land on the identical answer.
def default_log_path():
    """Where a server logs when BROWSERPLUG_LOG_FILE is unset (repo root)."""
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(repo_root, "browserplug-server.log")


def resolve_log_path(env=None):
    """The log file this process should use: BROWSERPLUG_LOG_FILE, else the default.

    An empty or whitespace-only value counts as unset rather than as a request to log
    to a file named "" (which would raise at handler construction and, before this,
    fall through to logging nowhere).
    """
    raw = (env if env is not None else os.environ).get(LOG_FILE_ENV)
    return raw.strip() if raw and raw.strip() else default_log_path()


# Set by a spawner on the child's env. Tells the child that its stderr is being
# captured to the startup file, so once its own file handler is up it should stop
# duplicating every record to stderr. Otherwise the startup file grows without bound
# and every line is logged twice.
DETACHED_ENV = "BROWSERPLUG_DETACHED"


# --- How a detached child is spawned (Windows: WITHOUT a console window) -------
# Both spawners used DETACHED_PROCESS (0x8), the textbook "no console" flag.
# On this project it POPPED A VISIBLE TERMINAL WINDOW on every spawn and every
# /restart. The reason is one hop further down than the flag can see: a uv environment's
# `Scripts/python.exe` (the server's script environment, as the old checkout .venv's was) is
# a **uv trampoline** (~45 KB) that re-execs the real interpreter
# (~/AppData/Roaming/uv/python/...) as a CHILD, with no creation flags of its own. So:
#   DETACHED_PROCESS -> trampoline has NO console -> Windows' rule for a console app
#   started by a console-less parent with no console flag is to ALLOCATE A NEW ONE,
#   visible. The flag that suppressed our window handed the grandchild a fresh one.
# CREATE_NO_WINDOW (0x08000000) instead: the child is a console app run without a
# console window, and the trampoline's own child inherits that. Measured
# GetConsoleWindow() == 0 for the interpreter that actually runs server.py, vs. a
# visible HWND under DETACHED_PROCESS.
#
# The detachment properties we actually rely on are unchanged: the child does not sit in
# our console (so a Ctrl-C or a console close cannot reach it), CREATE_NEW_PROCESS_GROUP
# keeps it out of our process group, and on Windows a child outlives its parent
# regardless. Neither flag escapes a job object, so nothing regressed there either.
#
# NB the flags are mutually exclusive: DETACHED_PROCESS wins if both are passed, which
# would silently restore the window. Pass exactly what this returns.
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000


def detached_spawn_kwargs():
    """The subprocess.Popen kwargs that detach a child *and* give it no window."""
    if os.name == "nt":
        return {"creationflags": CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


# --- What a detached server inherits ------------------------------------------
# The shared server outlives every client and /restart copies its environment as is, so
# whatever the FIRST spawning client had in its environment (its API keys, its TMPDIR, a
# stray port pin) configured the server for the rest of the lineage. The spawner now
# passes an allowlist. Every BROWSERPLUG_* variable still flows on purpose: they are the
# operator's knobs (log file, ports, admission limits), and dropping them would silently
# ignore a setting the user made. Other people's variables do not.
#
# uv's settings are named one by one, never as a UV_ prefix: the /restart replacement and
# the server env's sync run uv again, and a client's UV_PYTHON or UV_PROJECT_ENVIRONMENT
# carried into that lineage would re-target or recreate the environment under the running
# server (server_env.py). Where uv caches, which index it reads and how it trusts TLS are
# the user's machine settings and do flow.
_SPAWN_ENV_EXACT = frozenset({
    "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "PATH", "TEMP", "TMP", "TMPDIR",
    "USERPROFILE", "USERNAME", "LOCALAPPDATA", "APPDATA", "PROGRAMDATA", "PROGRAMFILES",
    "PROGRAMFILES(X86)", "HOMEDRIVE", "HOMEPATH", "HOME", "LANG", "TZ",
    "XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_RUNTIME_DIR",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY",
    "UV_CACHE_DIR", "UV_PYTHON_INSTALL_DIR", "UV_DEFAULT_INDEX", "UV_INDEX_URL",
    "UV_EXTRA_INDEX_URL", "UV_NATIVE_TLS", "SSL_CERT_FILE",
})
_SPAWN_ENV_PREFIXES = ("BROWSERPLUG_", "PYTHON", "LC_")
# PYTHONHOME (and the macOS PYTHONEXECUTABLE) point an interpreter at another installation's
# standard library, which breaks the server env's python outright. The launcher strips them
# from its proxy child too, which runs that same python (launcher._proxy_spawn_kwargs).
SPAWN_ENV_DENIED = frozenset({"PYTHONHOME", "PYTHONEXECUTABLE"})


def server_spawn_env(environ=None):
    """The environment a detached server is started with: an allowlisted copy.

    Names compare upper-cased because Windows variable names are case-insensitive
    (`Path`, `SystemRoot`). A lifecycle restart may use this too; a server that was
    spawned through it already holds only these names, so the restart stays consistent.
    BROWSERPLUG_UV names the uv this spawn found, so a /restart finds the same one even when
    the client named uv by a full path that is not on PATH.
    """
    from browserplug_shared import server_env
    source = os.environ if environ is None else environ
    out = {}
    for name, value in source.items():
        key = name.upper()
        if key in SPAWN_ENV_DENIED:
            continue
        if key in _SPAWN_ENV_EXACT or key.startswith(_SPAWN_ENV_PREFIXES):
            out[name] = value
    if not any(name.upper() == server_env.UV_ENV for name in out):
        uv = server_env.find_uv(source)
        if uv:
            out[server_env.UV_ENV] = uv
    return out


# --- Which interpreter runs the server ------------------------------------------
def _norm(path):
    """normcase(realpath()): a client registered through a junction or symlink to the
    checkout and a server started under the real path (or the other way round) must read as
    the same place."""
    return os.path.normcase(os.path.realpath(path))


def resolve_server_python(project_root, warn=None, deadline=None):
    """The interpreter to run server.py with: the server's own environment.

    A client may start the launcher with any Python (usually uv's bare interpreter, with no
    site-packages); the server needs its own dependencies, so this asks server_env, which
    builds, updates or repairs the environment while nothing runs from it and checks it
    before answering.
    Raises server_env.ServerEnvError, whose text names the remedy. `warn(str)` receives the
    slow or unusual events: a first build, a repair. `deadline` (time.monotonic()) bounds
    the whole lookup.
    """
    from browserplug_shared import server_env
    return server_env.ensure_server_python(project_root, log=warn, deadline=deadline)


def existing_server_python(project_root, deadline=None):
    """The same interpreter, looked up without changing the environment, for a process that
    joins a server already running from it (server_env.existing_server_python)."""
    from browserplug_shared import server_env
    return server_env.existing_server_python(project_root, deadline=deadline)


# --- Is this /health really OUR server? ----------------------------------------
def server_pid_is_ours(pid, project_root, probe=None, port=None, listener_probe=None):
    """True when `pid` is a BrowserPlug server of THIS checkout run by THIS user and, when
    `port` names the port that answered, the process that owns that port's listener.

    /health saying service=browserplug only proves something imitates the reply. A
    launcher that adopts a squatter on 2830..2942 hands the client's MCP traffic to it,
    so the port must also belong to a process whose command line names this checkout's
    server entry (`server.py`, `launcher.py`, or `-m server[.server]` with this
    checkout as cwd) and whose owner is the current user.

    The facts come from proc_facts (`probe(pid) -> (same_user, argv, cwd)` replaces it in
    tests), which reads them with the standard library on Windows and Linux. Anything
    unverifiable is foreign. The one exception is a platform with no native probe and no
    psutil (macOS on a bare interpreter): there nothing can check, so /health is trusted
    and a warning says so once.

    The pid is only what /health CLAIMS: any process that binds a port in the range can
    quote the real server's pid. So with a `port` the pid must also be the owner of the
    listener a connection to 127.0.0.1:port reaches (proc_facts.pid_owns_listener;
    `listener_probe(pid, port)` replaces it in tests). A platform that cannot read the
    socket table, or a read that fails (warned once), skips this half.
    """
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    from browserplug_shared import proc_facts
    try:
        same_user, cmdline, cwd = (probe or proc_facts.facts)(pid)
    except proc_facts.ProbeUnavailable:
        _warn_no_probe_once()
        return True
    except Exception:
        return False
    if not same_user:
        return False
    if not _names_checkout(cmdline, cwd, project_root):
        return False
    if port is None:
        return True
    try:
        return bool((listener_probe or proc_facts.pid_owns_listener)(pid, port))
    except proc_facts.ProbeUnavailable:
        return True
    except OSError as e:
        # The table could not be read (iphlpapi missing, the API erroring, a table that kept
        # growing). That says nothing about this server, and refusing on it would send every
        # launcher to spawn a duplicate of a healthy one, so only this half is skipped; the
        # pid, argv and cwd checks above still held.
        _warn_no_socket_table_once(e)
        return True
    except Exception:
        return False


def _names_checkout(cmdline, cwd, project_root):
    """True when argv runs one of this checkout's server entry points."""
    server_dir = os.path.join(project_root, "server")
    entries = {_norm(os.path.join(server_dir, name)) for name in ("server.py", "launcher.py")}
    entries.add(_norm(os.path.join(project_root, "launcher", "launcher.py")))
    for arg in cmdline[1:]:
        if not arg.lower().endswith(".py"):
            continue
        # A relative argument is placed by the process's cwd, never by this one's.
        candidate = arg if os.path.isabs(arg) else (os.path.join(cwd, arg) if cwd else None)
        if candidate and _norm(candidate) in entries:
            return True
    if "-m" in cmdline:
        idx = cmdline.index("-m")
        module = cmdline[idx + 1] if idx + 1 < len(cmdline) else ""
        if (module in ("server", "server.server", "server.launcher", "launcher.launcher")
                and cwd and _norm(cwd) == _norm(project_root)):
            return True
    return False


_warned_no_probe = False


def _warn_no_probe_once():
    global _warned_no_probe
    if _warned_no_probe:
        return
    _warned_no_probe = True
    import logging
    import sys
    logging.getLogger(__name__).warning(
        "%s has no native process probe and psutil is not importable in this interpreter, "
        "so a BrowserPlug-looking /health is trusted without checking which process answers "
        "it. Installing psutil for this interpreter turns the check on.", sys.platform)


_warned_no_socket_table = False


def _warn_no_socket_table_once(error):
    global _warned_no_socket_table
    if _warned_no_socket_table:
        return
    _warned_no_socket_table = True
    import logging
    logging.getLogger(__name__).warning(
        "The socket table could not be read (%s), so a BrowserPlug-looking /health is trusted "
        "without checking that its pid owns the listener on the port.", error)


# --- Single-instance lock location ------------------------------------------------
def instance_lock_path(port):
    """The lock file for `port`, in the per-user state directory.

    Not tempfile.gettempdir(): that honours TMPDIR/TEMP, so two clients with different
    temp directories would take two different locks and both believe they own the port.
    """
    from browserplug_shared.config_store import state_dir
    directory = state_dir()
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError:
        pass
    return os.path.join(directory, f"browserplug-instance-{port}.lock")
