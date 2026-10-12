"""The shared server's life as a process: claim the instance, serve, restart, stop.

server.py composes the object graph and dispatches; this module owns the parts that only
matter to a running process and that a test can drive without a browser: the
single-instance lock, the port checks, the MCP HTTP thread and its death policy, the
WebSocket listener, the restart sequence's process side, shutdown, logging setup, and
main().

Importing this module has no side effects. In particular it configures no logging: that
is `setup_logging()`, called by an entry point (`run()`, server/server.py as a script,
server/__main__.py). Configuring at import time is what once let a plain `import server`
attach a handler to the live log file, and a second copy of the module attach a second one.

It does not import server.py. `ServerLifecycle` receives everything it needs from the
server as callables (`LifecycleHooks`, read per use because tests and set_ws_host() change
the values after construction), and `main()` receives the server class as an argument.
"""

import argparse
import asyncio
import json
import logging
import os
import sys
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any, Callable

import uvicorn
import websockets

from browserplug_shared import server_env
from browserplug_shared.config_store import state_dir
from browserplug_shared.mcp_port import (DETACHED_ENV, MCP_PORT_DEFAULT, MCP_PORT_ENV, MCP_PORT_LAST,
                                         browserplug_health, detached_spawn_kwargs, find_mcp_port,
                                         pinned_mcp_port, port_is_bindable, port_is_occupied, resolve_log_path,
                                         restart_log_file, seek_browserplug, startup_log_glob,
                                         spawn_startup_log_path, instance_lock_path)
from browserplug_shared.ws_token import WS_HOST_ENV

from server.ws import http_auth
from server.runtime import watchdog
from server.runtime.graceful_restart import RestartHooks, RestartInProgress, perform_restart
from server.runtime.log_handler import SafeRotatingFileHandler
from server.ws import ws_limits
from server.ws.ws_auth import AUTH_STRICT, is_localhost_bind, ws_bind_addresses

logger = logging.getLogger(__name__)

# What a restart replacement runs. The server is started as `python server/server.py`
# (launcher, scheduled task, /restart) or `python -m server`; both end in the same
# main(), and a replacement always uses the script form because it needs no package
# context and works from any cwd.
SERVER_SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "server.py")

# The handshake that proves a restart replacement is alive. The outgoing server names a
# file in this variable; the replacement creates it once its imports and argument parsing
# succeeded and it is about to wait for the instance lock (signal_restart_ready). A
# replacement that dies on import never writes it, which is the whole signal: `Popen`
# returning says only that a process was created.
RESTART_READY_ENV = "BROWSERPLUG_RESTART_READY_FILE"
# How long the outgoing server waits for that marker. A cold import of the server takes
# 1-3 s here; this leaves a loaded box room without holding a broken restart open, and
# the replacement's own 25 s lock wait only starts once the marker exists.
REPLACEMENT_READY_SECONDS = 15.0
_READY_POLL_SECONDS = 0.05
# Used only when no marker file can be created (unwritable state dir): a child that is
# still running after this long is taken as alive.
_NO_MARKER_GRACE_SECONDS = 2.0
_STARTUP_TAIL_BYTES = 1500
# Restart captures kept next to the log (each /restart writes a new one).
STARTUP_LOGS_KEPT = 5
# How long a /restart may spend naming the replacement's interpreter. It runs before the
# drain, off the loop, and a restart that runs out of it is refused with the server still
# whole. server_env gets the budget as a deadline and cuts every uv call and probe to fit
# it, so its worker thread is done by then; the wait adds the time a timed-out subprocess
# takes to be killed and reaped. From the server's own environment it is read-only uv calls
# only (python_for_restart), well under a second.
RESTART_ENV_BUDGET_SECONDS = 180.0
RESTART_ENV_TIMEOUT_SECONDS = RESTART_ENV_BUDGET_SECONDS + 30.0


def restart_python(project_root=None, *, environ=None, prefix=None, runner=None):
    """The interpreter a /restart replacement runs on.

    It is asked of server_env.python_for_restart rather than taken from sys.executable,
    because the environment this process started from may have been pruned or gutted since.
    That never syncs the environment `prefix` (default sys.prefix) runs from: a change there
    refuses the restart (deps_changed). Raises server_env.ServerEnvError.
    """
    root = project_root or os.path.dirname(os.path.dirname(SERVER_SCRIPT))
    return server_env.python_for_restart(
        root, running_prefix=prefix, environ=environ, runner=runner,
        deadline=time.monotonic() + RESTART_ENV_BUDGET_SECONDS,
        log=lambda message: logger.warning("Restart: %s", message))


def signal_restart_ready():
    """Replacement side of the handshake: create the marker the outgoing server named.

    Never raises, and consumes the variable so the next restart of THIS process does not
    inherit a stale path (spawn_restart_process sets a fresh one each time).
    """
    path = os.environ.pop(RESTART_READY_ENV, None)
    if not path:
        return
    try:
        with open(path, "w") as fh:
            fh.write(str(os.getpid()))
    except OSError as e:
        logger.warning("Restart: could not write the readiness marker %s (%s) — the "
                       "outgoing server will treat this replacement as unconfirmed", path, e)


# --- Logging ------------------------------------------------------------------------

def setup_logging(environ=None):
    """Console logging on stderr plus the rotating file. Entry points call this once.

    Explicitly stderr so it never corrupts stdio MCP framing on stdout. BROWSERPLUG_LOG_FILE
    chooses the file; unset falls back to mcp_port.default_log_path() rather than to no
    file logging at all, because /restart inherits the environment unchanged and one server
    started without the variable would strand its whole lineage with no log (see
    resolve_log_path). Size-based rotation keeps the file bounded.

    Idempotent: a second call for the same file adds nothing. The file logging must have
    exactly one writer per process, and a doubled handler duplicated every line.
    """
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    log_file = resolve_log_path(environ)
    if not log_file:
        return
    root = logging.getLogger()
    wanted = os.path.abspath(log_file)
    if any(isinstance(h, SafeRotatingFileHandler) and h.baseFilename == wanted
           for h in root.handlers):
        return
    try:
        # SafeRotatingFileHandler, not the stdlib one: if a rollover rename is ever
        # blocked (another process holding the file: a second server instance, an AV
        # scan, a stray tail), it truncates in place instead of retrying the doomed
        # rename on every emit. That retry-flood is what wedged the shared server and
        # grew this log to 37 MB against its 2 MB cap (2026-07-17). See log_handler.py.
        handler = SafeRotatingFileHandler(
            log_file, maxBytes=2_000_000, backupCount=2, encoding="utf-8"
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s:%(name)s:%(message)s")
        )
        root.addHandler(handler)
        logger.info(f"BrowserPlug file logging enabled at {log_file}")
        # A DETACHED child's stderr is captured to the startup file (see
        # mcp_port.startup_log_path): its spawner points it there so a crash BEFORE
        # this block runs is not traceless. Now that the rotating handler is up, stop
        # duplicating every record to stderr; otherwise that startup file grows
        # without bound and every line is written twice, once bare and once formatted.
        # Interactive runs keep their console output; only a spawner sets this.
        if (environ if environ is not None else os.environ).get(DETACHED_ENV):
            for h in list(root.handlers):
                if isinstance(h, logging.StreamHandler) and not isinstance(
                        h, logging.FileHandler) and getattr(h, "stream", None) is sys.stderr:
                    root.removeHandler(h)
    except Exception as e:
        logger.warning(f"Could not enable file logging to {log_file}: {e}")


# --- Ports ----------------------------------------------------------------------------

def is_port_in_use(port, host="localhost"):
    """True if something holds `port` (loopback or wildcard), decided by a bind probe.

    Never connect here: a refused loopback connect to "localhost" takes ~2 s on
    Windows (::1 first, SYN dropped), which once added 8 s to every restart wait.
    port_is_bindable uses SO_EXCLUSIVEADDRUSE on Windows, so it does not report an
    occupied port as free (.tmp/dev-docs/ports.md).
    """
    return port_is_occupied(port, host)


def pid_listening_on_port(port):
    """PID of the process LISTENing on the given local port (or None)."""
    try:
        import psutil
        for c in psutil.net_connections(kind='inet'):
            if c.laddr and c.laddr.port == port and c.status == psutil.CONN_LISTEN and c.pid:
                return c.pid
    except Exception:
        pass
    return None


def choose_mcp_port(requested, mcp_bind, pinned=False):
    """The MCP HTTP port this server will bind.

    `pinned` (BROWSERPLUG_MCP_PORT, or --mcp-port-pinned) means nobody searches: the
    port is taken as is, and a busy one raises instead of relocating. A fixed-URL HTTP
    client or a pinned launcher looks only there, so a server that wandered off would be
    unreachable AND would lose the lock race against the one it duplicated.

    No port given: take the first free one in the standard sequence (2830, 2831,
    ... 2942, ascending, no gaps). That sequence is public policy, not an internal
    detail: launcher.py rediscovers a relocated server by walking exactly the same
    ports (mcp_port.py owns both halves). Tests pass the ports they allocated.

    The probe is port_is_bindable(), which NEVER sets SO_REUSEADDR on Windows; the
    old check here did, and a SO_REUSEADDR bind on Windows succeeds on a port a
    SO_REUSEADDR listener is actively serving, so it could call a live server's port
    "free" and bind on top of it. See mcp_port.port_is_bindable for the full table.
    Probe the address we are actually going to BIND, which is not always localhost:
    mcp_bind_host() moves this port to 0.0.0.0 with the WebSocket in LAN/WAN mode.
    Probing loopback while binding the wildcard asks the wrong question in both
    directions. It can call a port free that a wildcard listener already holds (the
    bind then fails at startup), and the launcher's seek had the mirror image of the
    same mismatch, where it skipped a live server and every client reported "no
    server found". The pure function is shared with the uvicorn config, so the two
    cannot drift; the caller recomputes it because set_ws_host() can move the WS bind
    before the server starts.
    """
    if requested is None:
        return find_mcp_port(host=mcp_bind)
    if port_is_bindable(requested, mcp_bind):
        return requested
    if pinned:
        raise RuntimeError(
            f"The pinned MCP HTTP port {requested} ({MCP_PORT_ENV} / --mcp-port-pinned) is "
            f"in use by another process (PID: {pid_listening_on_port(requested)}). A pin is "
            f"never relocated: free the port, or unset the pin to let the server walk "
            f"{MCP_PORT_DEFAULT}-{MCP_PORT_LAST}.")
    if MCP_PORT_DEFAULT <= requested <= MCP_PORT_LAST:
        # A port inside the standard range (what launcher.py asks for): keep walking
        # the same sequence, so a client that misses us on the requested port finds us
        # on the next one it tries.
        logger.warning(f"Requested MCP port {requested} is in use, walking up the "
                       f"{MCP_PORT_DEFAULT}-{MCP_PORT_LAST} range...")
        return find_mcp_port(requested, host=mcp_bind)
    # A custom port outside the range (tests allocate from their own pool): no
    # client seeks out here, so just take the next free port after it, as before.
    logger.warning(f"Requested MCP port {requested} is in use, finding alternative...")
    return find_mcp_port(requested, requested + 100, host=mcp_bind)


# --- Single-instance lock ---------------------------------------------------
# The Firefox WebSocket port is a singleton resource (one live extension link),
# so only ONE server may run. A connect-probe guard alone is TOCTOU-racy during
# a restart: the port is momentarily free while the old process exits, so two
# replacements can both pass the check, and on Windows SO_REUSEADDR even lets
# both bind. An OS advisory lock is atomic and is released automatically when the
# holding process exits (including os._exit), so it is the real authority.
_INSTANCE_LOCK_FH = None  # kept alive for the whole process lifetime


def _instance_lock_path(port):
    # State dir, not tempfile.gettempdir(): TMPDIR differs per client (mcp_port.instance_lock_path).
    return instance_lock_path(port)


def acquire_instance_lock(port):
    """Non-blocking: grab the single-instance lock for `port`. True if we own it
    (now or already), False if another live process holds it. If the lock file
    can't even be created we do NOT block startup (return True) and fall back to
    the port-conflict check below."""
    global _INSTANCE_LOCK_FH
    if _INSTANCE_LOCK_FH is not None:
        return True
    try:
        fh = open(_instance_lock_path(port), "a+")
    except OSError:
        return True
    try:
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return False
    _INSTANCE_LOCK_FH = fh
    return True


def release_instance_lock():
    """Release the single-instance lock we hold (if any). Process exit releases it
    anyway, but a fatal path that has to leave from a daemon thread releases it first
    so the replacement server never races a lock file the OS hasn't reclaimed yet."""
    global _INSTANCE_LOCK_FH
    fh, _INSTANCE_LOCK_FH = _INSTANCE_LOCK_FH, None
    if fh is None:
        return
    try:
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        fh.close()
    except OSError:
        pass


def acquire_instance_lock_waiting(port, deadline):
    """Block (until monotonic `deadline`) for the single-instance lock; used by a
    --wait-for-port restart replacement waiting for the old server to release it."""
    import time as _t
    while _t.monotonic() < deadline:
        if acquire_instance_lock(port):
            return True
        _t.sleep(0.2)
    return acquire_instance_lock(port)


# NB: there is no health-probe helper here. mcp_port.browserplug_health() is the ONE
# probe (imported by both this module and launcher.py, so they can never disagree)
# and it identity-checks `service == "browserplug"`, which a local helper duplicating
# "GET /health and parse" would quietly not do.


# --- Sockets --------------------------------------------------------------------------

def close_extension_connection(conn):
    """Close an extension socket from a SYNC stop path.

    websockets 16's Connection.close() is `async def`: calling it bare merely
    builds a coroutine; the socket is never closed and Python warns
    "coroutine was never awaited". Run it on the loop we are stopping from.
    """
    try:
        closing = conn.close()
    except Exception as e:
        logger.warning(f"Error closing extension connection: {e}")
        return
    if not asyncio.iscoroutine(closing):
        return  # already closed synchronously (pre-14 websockets API)
    try:
        asyncio.get_running_loop().create_task(closing)
    except RuntimeError:
        closing.close()
        logger.warning("No running event loop to close the extension connection on — "
                       "leaving it to process teardown")


# --- The running server -------------------------------------------------------------

@dataclass
class LifecycleHooks:
    """What the lifecycle reads from the server, as plain callables."""
    host: Callable[[], str]
    port: Callable[[], int]
    mcp_port: Callable[[], int]
    ws_host: Callable[[], str]
    ws_host_source: Callable[[], str]
    start_mcp: Callable[[], bool]
    use_stdio: Callable[[], bool]
    fatal_on_mcp_failure: Callable[[], bool]
    # The ASGI app uvicorn serves (already wrapped by the endpoint gate).
    http_app: Callable[[], Any]
    # An awaitable that serves MCP over stdin/stdout for --stdio.
    run_stdio: Callable[[], Any]
    # The extension WebSocket handler and its plain-HTTP fallback.
    ws_handler: Callable[..., Any]
    ws_process_request: Callable[..., Any]
    # For the startup line only.
    local_auth_mode: Callable[[], str]
    token_source: Callable[[], str]
    # The restart sequence's view of the server.
    count_in_flight: Callable[[], int]
    open_connections: Callable[[], Any]
    close_registry: Callable[[str], None]
    # A pinned MCP port is handed on to the /restart replacement.
    mcp_port_pinned: Callable[[], bool] = lambda: False
    # Toggles the "restarting" state that refuses new calls (ExtensionRpc.set_restarting).
    set_restarting: Callable[[bool], None] = lambda on: None


class ServerLifecycle:
    """The MCP HTTP thread, the WebSocket listener, restart and shutdown.

    The runtime state of the server process lives here: the uvicorn
    thread and instance, the "we are leaving on purpose" flag, the listener and the loop
    it runs on.
    """

    def __init__(self, hooks: LifecycleHooks):
        self.hooks = hooks
        # Set by every intentional stop path so the uvicorn thread's exit isn't read as
        # a crash.
        self.stopping = False
        # Single-flight guard for begin_restart: without it every POST /restart spawns its own
        # replacement pair. Guarded by a lock because the triggers arrive on different
        # threads (uvicorn's loop, the WebSocket loop).
        self.restarting = False
        self._restart_lock = threading.Lock()
        # The newest restart's record for /health: trigger, time, outcome (pending, ok,
        # aborted) and the reason it was abandoned. Replaced whole, never mutated, so a
        # reader on another thread sees one consistent record. A restart that succeeded
        # is only ever seen by the outgoing process, which exits right after.
        self.last_restart = None
        # (child, marker path, startup log path) of the replacement spawned last.
        self._replacement = None
        # The interpreter the pending restart spawns, resolved by begin_restart before the
        # sequence starts. `resolve_restart_python` is the seam tests replace.
        self._restart_python = None
        self.resolve_restart_python = restart_python
        # The loop websockets.serve runs on, captured in start_server. A restart may be
        # triggered from the uvicorn thread (POST /restart, /ws-bind) but has to close
        # extension sockets, which is this loop's business and nobody else's.
        self.ws_loop = None
        self.mcp_thread = None
        self.mcp_server_instance = None
        self.shutdown_event = None
        self.websocket_server = None
        # The frozen-loop watchdog (runtime/watchdog.py): only in the standalone server
        # process, started once the listeners are up. `_mcp_loop` is the uvicorn loop, which
        # reports itself from the lifespan before or after the watchdog exists.
        self.watchdog = None
        self._mcp_loop = None

    # ---- Watchdog ---------------------------------------------------------------

    def attach_mcp_loop(self, loop):
        """The uvicorn thread's loop, reported from the ASGI lifespan (http_endpoints)."""
        self._mcp_loop = loop
        if self.watchdog is not None:
            self.watchdog.attach_loop("mcp", loop)

    def start_watchdog(self):
        """Start the watchdog in the standalone server. An embedded server (tests build
        those directly) leaves `fatal_on_mcp_failure` off and gets none: the dead-man timer
        ends the process it runs in."""
        h = self.hooks
        if self.watchdog is not None or not h.fatal_on_mcp_failure() or not watchdog.enabled():
            return
        try:
            self.watchdog = watchdog.start(
                watchdog.Config.from_env(), hang_file=_open_hang_log(), ws_loop=self.ws_loop,
                begin_restart=self.begin_restart, hand_over=self._watchdog_hand_over,
                exit_process=self._watchdog_exit,
                mcp_port=lambda: h.mcp_port() if h.start_mcp() and not h.use_stdio() else None,
                # The probe goes to the address uvicorn really listens on (--ws-host ::).
                mcp_host=lambda: http_auth.mcp_bind_host(h.ws_host(), h.host()),
                leaving=lambda: self.stopping, restarting=lambda: self.restarting)
            if self._mcp_loop is not None:
                self.watchdog.attach_loop("mcp", self._mcp_loop)
            logger.info("Watchdog running (hang log: %s)",
                        getattr(self.watchdog.hang_file, "name", "stderr"))
        except Exception:
            logger.warning("Could not start the watchdog", exc_info=True)

    def _watchdog_hand_over(self):
        """The watchdog's last resort, first half: release the instance lock, then start a
        successor, so a confirmed freeze ends with a server coming up and not with a
        process that is gone and a port nobody serves. Order matters: the successor
        (`--wait-for-port`) waits for the lock for 25 s and then for our ports for 10 s,
        and it can take the lock only once this releases it. It runs on a watchdog worker
        thread with a deadline (watchdog._hand_over), because creating a process is one of
        the things a freeze can stop. A graceful restart that already spawned a replacement
        is not repeated."""
        release_instance_lock()
        child = self._replacement
        if child is not None and child[0].poll() is None:
            return
        self.spawn_restart_process()

    @staticmethod
    def _watchdog_exit():
        """The watchdog's last resort, second half: leave without the cooperation of a
        frozen loop. TerminateProcess on Windows (`os._exit` is ExitProcess, which can wait
        on the loader lock; see watchdog.terminate_process). The lock was released by the
        hand-over, and the OS drops it anyway when the process ends."""
        watchdog.terminate_process(1)

    # ---- MCP HTTP thread ---------------------------------------------------------

    async def start_mcp_server(self):
        """Start the MCP server in a separate thread"""
        h = self.hooks

        # Create shutdown event
        self.shutdown_event = threading.Event()
        self.stopping = False  # re-arm the death guard (a prior stop_mcp_server set it)

        def run_mcp_server():
            # BaseException, not Exception: uvicorn's bind-failure path calls sys.exit(1)
            # (uvicorn/server.py startup()), which raises SystemExit; threading then
            # swallows it silently, so `except Exception` would make the death invisible.
            instance = None
            try:
                # The HTTP port follows the WebSocket's bind, so "Configure local server
                # for incoming connections" governs both. What keeps that safe is the
                # gate in _wrap_client_tracking, not the bind: /mcp and /ws-token stay
                # refused to remote peers, everything else needs the token.
                mcp_host = http_auth.mcp_bind_host(h.ws_host(), h.host())
                logger.info(f"Starting MCP server on {mcp_host}:{h.mcp_port()}")

                # Create server config
                config = uvicorn.Config(
                    h.http_app(),
                    host=mcp_host,
                    port=h.mcp_port(),
                    log_level="error"  # Reduce log noise during tests
                )

                # Create server instance
                instance = uvicorn.Server(config)
                self.mcp_server_instance = instance

                # Run server
                instance.run()

            except BaseException as e:
                self.on_mcp_thread_exit(e, instance)
                return
            # run() also returns normally when the HTTP server stops; expected only when
            # we asked it to (stop_mcp_server sets should_exit).
            self.on_mcp_thread_exit(None, instance)

        # Run MCP server in separate thread
        self.mcp_thread = threading.Thread(target=run_mcp_server, daemon=True)
        self.mcp_thread.start()
        logger.info(f"MCP server thread started for {h.host()}:{h.mcp_port()}")

        # Give MCP server time to start (reduced time for faster tests)
        await asyncio.sleep(0.5)

    def on_mcp_thread_exit(self, exc, instance=None):
        """The uvicorn thread left run() -- fail hard if we didn't ask it to.

        Called from the MCP thread itself. An unrequested exit means there is no HTTP
        listener left, yet the process still owns the Firefox WebSocket port and the
        single-instance lock: launchers keep proxying to a dead port and their spawned
        replacements exit(1) against our lock, so every client stays broken until the
        PID is killed. The standalone server therefore dies; embedded servers (tests,
        which construct BrowserPlugServer directly) keep the WebSocket bridge alive.

        `instance` is the uvicorn.Server whose thread is reporting. stop_mcp_server
        gives up joining after 5s and drops its references anyway, so a SUPERSEDED thread
        can still be alive and exit later, while a replacement serves happily; only the
        current instance's death may kill the process.
        """
        if instance is not None and instance is not self.mcp_server_instance:
            logger.info(f"A superseded MCP server thread exited ({exc!r}) — ignoring")
            return
        if self.stopping:
            return
        where = f"{self.hooks.host()}:{self.hooks.mcp_port()}"
        reason = repr(exc) if exc is not None else "run() returned without a shutdown request"
        if not self.hooks.fatal_on_mcp_failure():
            logger.warning(f"MCP server on {where} stopped unexpectedly: {reason}", exc_info=exc)
            return
        logger.critical(
            f"MCP HTTP server on {where} died ({reason}) — this process still holds the "
            f"WebSocket port and the single-instance lock but serves no MCP. Exiting so the "
            f"next launcher can start a healthy replacement.", exc_info=exc)
        # os._exit from this daemon thread: the main thread is parked in
        # websocket_server.wait_closed() and there is no cross-thread way to fail it out
        # of asyncio.run with a non-zero status. Drop the instance lock explicitly first
        # so the replacement doesn't wait on the OS to reclaim it.
        release_instance_lock()
        os._exit(1)

    def stop_mcp_server(self):
        """Stop the MCP server gracefully"""
        self.stopping = True
        if self.mcp_server_instance:
            try:
                logger.info("Stopping MCP server...")

                # A subscriptions/listen stream never ends by itself and uvicorn waits for
                # every open response, so end them first. Imported here: server.tools sits
                # above server.runtime in the package graph.
                from server.tools import list_changed
                list_changed.close_streams(self.mcp_thread)

                # Signal server to shutdown
                self.mcp_server_instance.should_exit = True

                # Wait for thread to finish with timeout
                if self.mcp_thread and self.mcp_thread.is_alive():
                    self.mcp_thread.join(timeout=5.0)

                    if self.mcp_thread.is_alive():
                        logger.warning("MCP server thread did not stop gracefully within timeout")
                    else:
                        logger.info("MCP server stopped gracefully")

                # Clean up references
                self.mcp_server_instance = None
                self.mcp_thread = None

            except Exception as e:
                logger.warning(f"Error stopping MCP server: {e}")

    # ---- Restart -----------------------------------------------------------------

    def spawn_restart_process(self, python=None):
        """Spawn a replacement server (detached) that waits for our ports to free.

        Used by POST /restart: we spawn the new process with --wait-for-port, then
        exit; it waits for our WS + MCP ports to release and binds fresh, picking
        up any code changes. The stdio proxies reconnect to it on their next call.

        The interpreter is `python`, else the one begin_restart resolved (restart_python)
        before the sequence started, else this process's own (a direct call); this stays a
        plain Popen of it.
        """
        import subprocess
        h = self.hooks
        args = [python or self._restart_python or sys.executable, SERVER_SCRIPT,
                "--port", str(h.port()),
                "--mcp-port", str(h.mcp_port()), "--wait-for-port"]
        if h.mcp_port_pinned():
            args.append("--mcp-port-pinned")
        # A WS bind that came from --ws-host would otherwise be silently dropped by the
        # replacement. The other sources need no help: the env is inherited below, and a
        # bind set from the options page is in the ws-host FILE, which it re-reads.
        if h.ws_host_source() == "arg" and h.ws_host() != h.host():
            args += ["--ws-host", str(h.ws_host())]
        # Capture the replacement's stdout/stderr so a failed restart is NOT silent.
        # (DEVNULL would hide a replacement that crashed or hung on startup: no trace
        # at all, yet the old server has already exited, taking the service down
        # invisibly.)
        #
        # Writing into BROWSERPLUG_LOG_FILE itself breaks log rotation everywhere:
        # the replacement's RotatingFileHandler owns that path,
        # and a second life-long handle on it makes the rollover rename fail forever
        # (WinError 32), after which every record dumps a handler traceback into the
        # same file. Measured: a 37 MB browserplug-server.log against a 2 MB cap. The capture
        # goes to the same startup sibling launcher.py uses, truncated per spawn, and
        # DETACHED_ENV tells the child to stop echoing records to stderr once its own
        # file handler is up.
        log_path = os.environ.get("BROWSERPLUG_LOG_FILE") or restart_log_file(
            os.path.dirname(SERVER_SCRIPT))
        env = dict(os.environ)
        env[DETACHED_ENV] = "1"
        # One capture file per spawn, like the launcher's: a restart that follows a failed
        # one must not truncate the only traceback of the replacement that died.
        startup_log = spawn_startup_log_path(
            log_path, f"restart-{os.getpid()}-{int(time.time() * 1000)}")
        self._prune_startup_logs(log_path, keep=startup_log)
        marker = self._new_ready_marker()
        if marker:
            env[RESTART_READY_ENV] = marker
        else:
            env.pop(RESTART_READY_ENV, None)
        try:
            restart_out = open(startup_log, "w", buffering=1)
        except OSError:
            restart_out = subprocess.DEVNULL
        kwargs = dict(stdin=subprocess.DEVNULL, stdout=restart_out,
                      stderr=restart_out, env=env, cwd=os.getcwd())
        # Windowless AND detached; see detached_spawn_kwargs() for why the obvious
        # DETACHED_PROCESS is the flag that made a terminal window appear on /restart.
        kwargs.update(detached_spawn_kwargs())
        try:
            child = subprocess.Popen(args, **kwargs)
        finally:
            # The child has its own inherited handle. On the success path we os._exit()
            # right after, but the spawn-failed-stay-alive path keeps THIS process
            # running, and a retained handle here would block our own next rotation.
            if restart_out is not subprocess.DEVNULL:
                restart_out.close()
        self._replacement = (child, marker, startup_log)
        # Outside the try, and defensive about `pid`, because a raise HERE would report a
        # spawn that SUCCEEDED as failed: perform_restart would then abort the restart and
        # keep this server alive alongside the replacement it just started, both of them
        # going for the same ports.
        # PID %s is what we spawned, which under a uv environment is the trampoline, not the
        # interpreter that ends up running server.py. The replacement logs its OWN
        # pid on arrival ("is the replacement"), so the pair reads correctly either way.
        logger.info("Restart: replacement spawned as PID %s (waiting for ports %s and %s "
                    "to free); this process (PID %s) is leaving",
                    getattr(child, "pid", "?"), h.port(), h.mcp_port(), os.getpid())

    @staticmethod
    def _prune_startup_logs(log_file, keep):
        """Keep the newest few restart captures (and the launcher's); delete the rest."""
        import glob
        try:
            found = [f for f in glob.glob(startup_log_glob(log_file))
                     if os.path.abspath(f) != os.path.abspath(keep)]
            found.sort(key=os.path.getmtime, reverse=True)
            for old in found[max(STARTUP_LOGS_KEPT - 1, 0):]:
                try:
                    os.remove(old)
                except OSError:
                    pass    # held open by a live child, or already gone
        except OSError:
            pass

    @staticmethod
    def _new_ready_marker():
        """A fresh, absent path for the replacement's readiness marker, or None when the
        state directory cannot be written (then confirmation degrades to a liveness check)."""
        try:
            directory = state_dir()
            os.makedirs(directory, exist_ok=True)
            path = os.path.join(directory, f"restart-ready-{os.getpid()}-"
                                           f"{time.time_ns()}.marker")
            if os.path.exists(path):
                os.remove(path)
            return path
        except OSError:
            return None

    def confirm_replacement(self):
        """Block until the replacement proved it is alive; None if it did, else why not.

        Alive means: still running AND the readiness marker exists (it got through its
        imports to the lock wait). A child that exited is reported with its exit code and
        the tail of its startup log, which is where its traceback went; one that stays
        silent past REPLACEMENT_READY_SECONDS is stopped, so an abandoned restart leaves
        no second server queueing for our lock.
        """
        spawned = self._replacement
        if spawned is None:
            return None
        child, marker, startup_log = spawned
        poll = getattr(child, "poll", None)
        if poll is None:
            return None     # not a real process handle; nothing to observe
        started = time.monotonic()
        limit = REPLACEMENT_READY_SECONDS if marker else _NO_MARKER_GRACE_SECONDS
        while True:
            code = poll()
            if code is not None:
                self._drop_marker(marker)
                return (f"the replacement exited with code {code} before reaching the lock "
                        f"wait; startup log tail: {self._startup_tail(startup_log)}")
            if marker and os.path.exists(marker):
                self._drop_marker(marker)
                return None
            if time.monotonic() - started >= limit:
                break
            time.sleep(_READY_POLL_SECONDS)
        if not marker:
            return None     # alive and no way to ask more; trust it, as before
        self._drop_marker(marker)
        try:
            child.terminate()
        except OSError:
            pass
        return (f"the replacement did not reach the lock wait within {limit:.0f}s "
                f"(stopped it); startup log tail: {self._startup_tail(startup_log)}")

    @staticmethod
    def _drop_marker(path):
        if not path:
            return
        try:
            os.remove(path)
        except OSError:
            pass

    @staticmethod
    def _startup_tail(path):
        try:
            with open(path, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                fh.seek(max(0, fh.tell() - _STARTUP_TAIL_BYTES))
                text = fh.read().decode("utf-8", "replace").strip()
        except OSError:
            return "(unreadable)"
        return " | ".join(text.splitlines()[-12:]) or "(empty)"

    def restart_hooks(self):
        """The server half of graceful_restart.RestartHooks (see that module's docstring
        for why the steps run in the order they do)."""
        h = self.hooks

        def arm():
            self.stopping = True    # intentional exit, not an MCP crash

        def disarm():
            self.stopping = False   # the spawn failed; we are still serving

        def exit_process():
            # Flush the rotating handler before a hard exit, and drop the instance lock
            # explicitly so the replacement isn't left waiting on the OS to reclaim it.
            logging.shutdown()
            release_instance_lock()
            os._exit(0)

        return RestartHooks(
            count_in_flight=h.count_in_flight,
            connections=h.open_connections,
            spawn_replacement=self.spawn_restart_process,
            close_registry=h.close_registry,
            stop_http=self.stop_mcp_server,
            exit_process=exit_process,
            arm=arm,
            disarm=disarm,
            confirm_replacement=self.confirm_replacement,
            set_restarting=h.set_restarting,
        )

    @staticmethod
    def _restart_record(trigger, outcome, reason=None):
        return {"trigger": trigger, "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "outcome": outcome, "reason": reason}

    def begin_restart(self, trigger):
        """Start the graceful restart sequence. Callable from ANY thread.

        The WS handler runs on the WebSocket loop; POST /restart and the rebind
        endpoints run on uvicorn's. The sequence itself always runs on the WebSocket
        loop, because closing an extension socket belongs to the loop that serves it.

        Single-flight: raises RestartInProgress while one is running. Otherwise returns a
        concurrent Future that resolves to None once the replacement is confirmed, or to
        the error text when the restart was abandoned (the server then keeps serving).
        """
        with self._restart_lock:
            if self.restarting:
                raise RestartInProgress(
                    f"a restart is already in progress; ignoring the second request "
                    f"({trigger})")
            self.restarting = True
            self.last_restart = self._restart_record(trigger, "pending")

        # Resolved once by the sequence: None = the replacement is confirmed and teardown
        # follows, a string = the restart was abandoned and the server keeps serving.
        verdict = Future()
        hooks = self.restart_hooks()
        announce = hooks.announce

        def settle(error):
            self.last_restart = self._restart_record(
                trigger, "aborted" if error else "ok", error)
            announce(error)
            if not verdict.done():
                verdict.set_result(error)

        hooks.announce = settle

        async def run():
            try:
                # Before perform_restart, so before set_restarting(True) and the drain: a
                # restart that cannot name its replacement's interpreter is refused while
                # this server is still whole, and uv never blocks the WebSocket loop.
                self._restart_python = None
                try:
                    self._restart_python = await asyncio.wait_for(
                        asyncio.to_thread(self.resolve_restart_python),
                        timeout=RESTART_ENV_TIMEOUT_SECONDS)
                except asyncio.TimeoutError:
                    error = (f"refused: the replacement's interpreter was not ready within "
                             f"{RESTART_ENV_TIMEOUT_SECONDS:.0f}s (server_env: uv sync); "
                             f"the server keeps serving")
                    logger.error("Restart (%s) %s", trigger, error)
                    settle(error)
                    return
                except Exception as e:
                    # deps_changed is not "no interpreter": the environment works, and
                    # updating it under this process is what the refusal prevents.
                    error = (f"refused: {e}"
                             if getattr(e, "kind", None) == "deps_changed" else
                             f"refused: no interpreter for the replacement: {e}")
                    logger.error("Restart (%s) %s", trigger, error)
                    settle(error)
                    return
                await perform_restart(hooks, trigger=trigger)
            except BaseException as e:
                settle(f"restart failed: {e!r}")
                logger.exception("Restart (%s) failed", trigger)
            finally:
                with self._restart_lock:
                    self.restarting = False

        loop = self.ws_loop
        if loop is None or loop.is_closed():
            # No WebSocket loop: an embedded server (tests) or one that never started.
            # Nothing to close there, so a private loop runs the same sequence.
            threading.Thread(target=lambda: asyncio.run(run()), daemon=True).start()
        else:
            asyncio.run_coroutine_threadsafe(run(), loop)
        return verdict

    def restart_onto(self, host):
        """Restart into a process that binds `host`."""
        logger.info("Rebinding the extension WebSocket to %s — restarting onto it", host)
        try:
            self.begin_restart(f"rebind to {host}")
        except RestartInProgress as e:
            # This request did not start a restart of its own. The bind is already
            # persisted, so it takes effect on the next restart.
            logger.warning("Rebind to %s: %s", host, e)

    # ---- Stop --------------------------------------------------------------------

    def stop_websocket_server(self):
        """Stop the WebSocket server gracefully"""
        if self.websocket_server:
            try:
                logger.info("Stopping WebSocket server...")

                # Close every browser's socket first. Emptying the registry means the
                # handlers' finally no longer recognize their sockets and will not fail
                # their in-flight requests; registry.close_all does it, or a call in
                # flight hangs for its whole timeout budget while the server is gone.
                self.hooks.close_registry(
                    "Server is shutting down — the extension connection was closed")

                # Close the WebSocket server
                self.websocket_server.close()

                # Clean up reference
                self.websocket_server = None

                logger.info("WebSocket server stopped gracefully")

            except Exception as e:
                logger.warning(f"Error stopping WebSocket server: {e}")

    def stop(self):
        """Stop all servers (WebSocket and MCP)"""
        logger.info("Stopping BrowserPlug server...")

        if self.watchdog is not None:
            self.watchdog.stop()

        # Stop MCP server
        self.stop_mcp_server()

        # Stop WebSocket server
        self.stop_websocket_server()

        logger.info("BrowserPlug server stopped")

    async def shutdown(self, server_task):
        """
        Gracefully shutdown the server and its task.

        Args:
            server_task: asyncio.Task running the server
        """
        try:
            # Stop server resources first
            self.stop()

            # Cancel the task
            server_task.cancel()

            # Wait for task to finish, handling CancelledError
            try:
                await server_task
            except asyncio.CancelledError:
                pass

        except Exception as e:
            logger.warning(f"Error during server shutdown: {e}")

    # ---- Serve -------------------------------------------------------------------

    async def start_server(self):
        """Start both WebSocket and MCP servers"""
        h = self.hooks
        logger.info(f"Starting BrowserPlug server on {h.ws_host()}:{h.port()}")

        # Every restart hops back onto this loop to close its sockets (begin_restart).
        self.ws_loop = asyncio.get_running_loop()

        # In HTTP mode start MCP first; in stdio mode it runs as a sibling task below
        if h.start_mcp() and not h.use_stdio():
            await self.start_mcp_server()
            logger.info(f"MCP tools available at http://{h.host()}:{h.mcp_port()}/")
        elif not h.start_mcp():
            logger.info("MCP server disabled for this instance")

        # NO reuse_address here; deliberately. This socket is full control of a
        # logged-in browser: a local process that binds OVER it can impersonate the
        # server to the extension and harvest the token from its hello.
        #
        # Passing reuse_address=True ("for immediate port reuse") would set
        # SO_REUSEADDR on every platform. On Windows that OPENS the hole: a second
        # SO_REUSEADDR socket may bind a port an SO_REUSEADDR listener is actively
        # serving, and the newcomer wins (live-verified on Win11; with our listener
        # plain, the same steal is refused with WSAEACCES). Leaving the kwarg out keeps
        # asyncio's per-platform default, which is exactly what we want:
        #   * Windows: no SO_REUSEADDR (asyncio never sets it there) → nobody binds
        #     over us. Nothing is lost: a listening bind on Windows is not blocked by
        #     TIME_WAIT, so restarts still take the port back immediately.
        #   * POSIX: SO_REUSEADDR (asyncio's default), which there only means "reuse a
        #     TIME_WAIT port". It cannot bind over an active listener, so fast restart
        #     keeps working and the steal is impossible anyway.
        # See mcp_port.port_is_bindable for the same reasoning on the probe side.
        self.websocket_server = await websockets.serve(
            h.ws_handler,
            ws_bind_addresses(h.ws_host()),
            h.port(),
            process_request=h.ws_process_request,
            # Ciphertext does not compress, and a browser's inflate limit then refuses
            # a large record outright (Node: "1009 Max decompressed message size").
            compression=None,
            # Small frames until the peer has authenticated; handle_extension_connection
            # raises the limit afterwards (ws_limits.py).
            **ws_limits.serve_kwargs(),
        )

        if not is_localhost_bind(h.ws_host()):
            logger.warning(
                f"Extension WebSocket is bound to {h.ws_host()}:{h.port()}, reachable "
                f"from other machines. Each remote browser needs a device token of its "
                f"own: press Copy next to the connection token in Options on this "
                f"machine, or run `python gen_ws_token.py --device`.")
        logger.info(f"BrowserPlug WebSocket server is running on {h.ws_host()}:{h.port()} "
                    f"(local peers: {h.local_auth_mode()}; remote peers: "
                    f"{AUTH_STRICT}; token source: {h.token_source()})...")
        self.start_watchdog()

        if h.start_mcp() and h.use_stdio():
            # stdio mode: run MCP over stdin/stdout in the same loop as the websocket bridge
            logger.info("MCP serving over stdio (no HTTP listener)")
            mcp_task = asyncio.create_task(
                h.run_stdio(),
                name="browserplug-stdio",
            )
            ws_task = asyncio.create_task(self.websocket_server.wait_closed(), name="browserplug-ws")
            done, pending = await asyncio.wait(
                {mcp_task, ws_task}, return_when=asyncio.FIRST_COMPLETED
            )
            for t in pending:
                t.cancel()
            for t in done:
                exc = t.exception()
                if exc is not None:
                    raise exc
        else:
            await self.websocket_server.wait_closed()


# --- Entry point ----------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(description='BrowserPlug Server - WebSocket server for browser extension')
    parser.add_argument('--host', default='localhost',
                        help='Host to bind to (default: localhost)')
    parser.add_argument('--port', type=int, default=8772,
                        help='WebSocket port (default: 8772)')
    parser.add_argument('--ws-host', default=None,
                        help=f'Bind address for the extension WebSocket only (default: '
                             f'localhost; env: {WS_HOST_ENV}). Use it to let a Firefox on '
                             f'ANOTHER machine connect: it needs a device token of its '
                             f'own (gen_ws_token.py --device), which also encrypts the '
                             f'connection. The MCP HTTP port follows this bind: a remote '
                             f'caller there must sign requests with its device token '
                             f'(X-BrowserPlug-Auth), and /mcp plus /ws-token stay '
                             f'loopback-only whatever the bind.')
    parser.add_argument('--mcp-port', type=int, default=None,
                        help=f'MCP HTTP port (default: {MCP_PORT_DEFAULT}; if it is taken the '
                             f'server walks up one port at a time to {MCP_PORT_LAST}, and '
                             f'clients rediscover it by walking the same range). Env: '
                             f'{MCP_PORT_ENV} pins a port explicitly.')
    parser.add_argument('--mcp-port-pinned', action='store_true',
                        help='Treat --mcp-port as a pin: never relocate, fail if it is busy '
                             f'(same as setting {MCP_PORT_ENV}; the launcher passes it when '
                             'its own environment holds the pin).')
    parser.add_argument('--no-mcp', action='store_true',
                        help='Disable MCP server')
    parser.add_argument('--stdio', action='store_true',
                        help='Serve MCP over stdio instead of HTTP (so an MCP client can spawn this process directly)')
    parser.add_argument('--wait-for-port', action='store_true',
                        help='If the ports are busy at startup, wait for them to free instead of refusing '
                             '(used by /restart so the replacement waits for the old process to exit)')
    return parser


async def main(server_factory, argv=None):
    """Claim the instance and the ports, build the server with `server_factory`
    (the BrowserPlugServer class; passed in so this module never imports server.py),
    and serve until it stops."""
    args = build_parser().parse_args(argv)

    # --host never exposes the MCP port on its own: it is forced back to localhost, and
    # the only way that port leaves loopback is by following the WebSocket bind
    # (http_auth.mcp_bind_host).
    if args.host != 'localhost' and args.host != '127.0.0.1':
        logger.warning(f"Host '{args.host}' changed to 'localhost' for security")
        args.host = 'localhost'

    # The extension WebSocket may be exposed on purpose (a Firefox on another machine).
    # It is never exposed WITHOUT a token, but there is no longer anything to refuse
    # here: BrowserPlugServer resolves (and, if need be, mints + persists) one at construction,
    # and ws_bind_refusal() stays as the invariant check inside it.
    #
    # Pass ONLY the flag: resolving env/file here too would flatten every source into
    # "arg", and the server could no longer tell the options page why it is bound where
    # it is (nor let its button change a bind the env did not pin).
    ws_host = args.ws_host

    # Pre-flight: refuse to start a duplicate. Two BrowserPlug servers would fight over the
    # single Firefox WebSocket connection (port 8772) and destabilize it.
    http_enabled = not args.no_mcp and not args.stdio
    # MCP HTTP port precedence: --mcp-port, then the BROWSERPLUG_MCP_PORT pin, else "unset";
    # which means the standard search (2830 upward). Either way BrowserPlugServer resolves the
    # final port; nothing here may refuse to start merely because the MCP port is taken:
    # walking up the range is the designed response, and clients seek the server there.
    requested_mcp_port = args.mcp_port if args.mcp_port is not None else pinned_mcp_port()
    # A pin never relocates: the env var, or the launcher's flag for the same thing.
    mcp_port_pinned = requested_mcp_port is not None and (
        args.mcp_port_pinned or pinned_mcp_port() is not None)
    mcp_port_to_check = (requested_mcp_port if requested_mcp_port is not None
                         else MCP_PORT_DEFAULT)
    import time as _time
    health_url = f"http://127.0.0.1:{mcp_port_to_check}/health"

    # Single-instance authority: acquire the lock BEFORE binding. A --wait-for-port
    # restart replacement waits for the old server to release it (on exit); a normal
    # start takes it non-blocking. This is race-free where the old connect-probe
    # guard was not; two replacements (or a launcher-spawned server during the
    # restart gap) could each see the port briefly free and both proceed.
    if args.wait_for_port:
        # Tell the outgoing server we survived our imports; it holds its teardown until
        # it sees this (ServerLifecycle.confirm_replacement).
        signal_restart_ready()
        # The wait polls with time.sleep for up to 25 s; a thread keeps the loop free.
        # ruff's ASYNC rules cannot see through a sync helper, so this call site is the
        # only guard against putting the blocking call back on the loop.
        got_lock = await asyncio.to_thread(
            acquire_instance_lock_waiting, args.port, _time.monotonic() + 25.0)
    else:
        got_lock = acquire_instance_lock(args.port)

    if not got_lock:
        # Someone else owns the instance lock. If it's a healthy server this is a
        # benign outcome (e.g. a restart replacement that lost the race to a
        # launcher-spawned server) -> exit 0. Otherwise refuse loudly.
        #
        # WHERE the incumbent serves MCP is not knowable a priori: it may have walked up
        # the range. So look for it the way a client does; probe the pinned port if one
        # was requested, else seek 2830..2942 for a /health that identifies as BrowserPlug.
        existing_pid = pid_listening_on_port(args.port)
        health, health_where = None, health_url
        if http_enabled:
            if requested_mcp_port is not None:
                health = browserplug_health(requested_mcp_port, timeout=2.0)
            else:
                found_port, health = seek_browserplug()
                if found_port is not None:
                    health_where = f"http://127.0.0.1:{found_port}/health"
                    if existing_pid is None:
                        existing_pid = pid_listening_on_port(found_port)
        if health is not None:
            logger.info("Another healthy BrowserPlug server already holds the instance lock "
                        "(PID: %s). Nothing to do -- exiting. Health at %s.", existing_pid, health_where)
            sys.exit(0)
        logger.error("Could not acquire the BrowserPlug single-instance lock (held by PID: %s) and no "
                     "healthy BrowserPlug server answered on %s. Refusing to start a second instance "
                     "that would fight over the single Firefox connection on port %s.",
                     existing_pid,
                     (health_where if requested_mcp_port is not None or not http_enabled
                      else f"ports {MCP_PORT_DEFAULT}-{MCP_PORT_LAST}"),
                     args.port)
        sys.exit(1)

    # We own the lock. For a restart, the old process's listen sockets close as it
    # exits; wait briefly for the ports to actually free before binding. /restart passes
    # the CURRENT --mcp-port, so waiting here is what brings the replacement back up on the
    # SAME port instead of walking past it.
    if args.wait_for_port:
        # The other half of the handover the outgoing server logged. Both processes write
        # to the same BROWSERPLUG_LOG_FILE, so these two lines bracket a restart in one file.
        logger.info("Restart: this process (PID %s) is the replacement — waiting for the "
                    "outgoing server to free ports %s and %s", os.getpid(), args.port,
                    mcp_port_to_check)
        pdeadline = _time.monotonic() + 10.0
        while _time.monotonic() < pdeadline:
            if not is_port_in_use(args.port) and not (http_enabled and is_port_in_use(mcp_port_to_check)):
                break
            await asyncio.sleep(0.2)

    # Backstop: we hold the lock but a process WITHOUT it still holds the WebSocket port
    # (a pre-lock server, or an unrelated app). That one is fatal; the Firefox link is a
    # singleton and cannot move. An occupied MCP HTTP port is NOT fatal: BrowserPlugServer walks
    # up the 2830-2942 range and clients seek it there.
    if is_port_in_use(args.port):
        existing_pid = pid_listening_on_port(args.port)
        health = (browserplug_health(mcp_port_to_check, timeout=2.0) or {"_error": "unreachable"}
                  if http_enabled else {"_error": "no HTTP MCP port"})
        logger.error(
            "Acquired the single-instance lock but port %s (WebSocket / Firefox extension) is "
            "still in use by another process (PID: %s). Not starting. Health at %s: %s",
            args.port, existing_pid, health_url, json.dumps(health, ensure_ascii=False))
        sys.exit(1)
    if http_enabled and is_port_in_use(mcp_port_to_check) and mcp_port_pinned:
        logger.error(
            "The pinned MCP HTTP port %s is in use by another process (PID: %s). A pin is "
            "never relocated; free it or unset %s. Not starting.",
            mcp_port_to_check, pid_listening_on_port(mcp_port_to_check), MCP_PORT_ENV)
        sys.exit(1)
    if http_enabled and is_port_in_use(mcp_port_to_check):
        logger.warning(
            "MCP HTTP port %s is in use by another process (PID: %s) — walking up the "
            "%s-%s range for a free one; clients rediscover the server by walking it too.",
            mcp_port_to_check, pid_listening_on_port(mcp_port_to_check),
            MCP_PORT_DEFAULT, MCP_PORT_LAST)

    server = server_factory(
        host=args.host,
        port=args.port,
        ws_host=ws_host,
        # None => the standard ascending search from MCP_PORT_DEFAULT; an explicit
        # --mcp-port / BROWSERPLUG_MCP_PORT is honoured (and only relocated if it is taken).
        mcp_port=requested_mcp_port,
        mcp_port_pinned=mcp_port_pinned,
        start_mcp=not args.no_mcp,
        use_stdio=args.stdio,
        # This is the process launchers proxy to: a dead MCP HTTP listener here is
        # unrecoverable for every client, so exit instead of lingering as a lock-holding
        # zombie (embedded/test servers keep the tolerant default).
        fatal_on_mcp_failure=True,
    )
    await server.start_server()


_HANG_FILE = None


def _open_hang_log():
    """The hang log (watchdog.py), opened once per process. faulthandler writes native
    crash dumps and the watchdog's stack dumps into it."""
    global _HANG_FILE
    if _HANG_FILE is None:
        log_file = resolve_log_path()
        if log_file:
            _HANG_FILE = watchdog.open_hang_log(log_file)
    return _HANG_FILE


def run(server_factory, argv=None):
    """The process entry point every way of starting the server ends in: set up logging,
    then serve. `python server/server.py` and `python -m server` both call this."""
    setup_logging()
    _open_hang_log()
    try:
        asyncio.run(main(server_factory, argv))
    except KeyboardInterrupt:
        logger.info("Server stopped by user")
    except Exception as e:
        # exc_info: without the traceback this logged one bare line ("Server error:
        # 'NoneType' object has no attribute 'x'") for a crash anywhere in startup;
        # no file, no line, nothing to act on. Also re-raise so the exit code is
        # non-zero: a launcher/scheduled task that reads exit status was told the
        # server had exited cleanly.
        logger.error("Server error: %s", e, exc_info=True)
        raise
