#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ['psutil>=7.2.2; sys_platform == "darwin"']
# ///
# Stdlib only: proc_facts reads the /health owner natively on Windows and Linux. macOS has no
# native probe, so a `uv run --script` registration there gets psutil for it. The block is
# the same in server/launcher.py (tests/unit/test_declared_dependencies.py).
"""
BrowserPlug stdio launcher: register THIS as the `browserplug` MCP server (type: stdio).

Each Client (e.g. Codex, Claude Code) instance spawns its own copy of this launcher. On startup it:
  1. FINDS the shared server: probes /health on 2830, then 2831, … up to 2942, and
     takes the first port whose /health identifies itself as BrowserPlug (`service`).
     BROWSERPLUG_MCP_PORT pins a port and skips the seek entirely.
  2. if no BrowserPlug server is anywhere in that range, spawns server.py as a DETACHED
     process so it outlives this launcher (and Claude Code), then seeks again,
     because a server whose default port was taken relocates upward.
  3. carries MCP over stdio <-> the shared HTTP server.

Step 3 has two implementations and the default is `launcher/stdio_relay.py`, a stdlib byte
relay: read that module's docstring before changing anything about the transport. The
proxy transport comes back with BROWSERPLUG_LAUNCHER_RELAY=0: `serve_via_proxy` runs
server/proxy/ (an MCP SDK server on stdio that forwards to the shared server) as a child
on this process's own stdin and stdout, with the server environment's python. The relay is
the default because of memory: the proxy measured about three times the relay's ~30 MB RSS,
nearly all of it import graph, and every MCP client on the box spawns a launcher. So this
module imports the standard library and browserplug_shared and nothing else, at module
level or inside a function, and never anything from server/
(tests/unit/test_import_boundaries.py and test_stdio_relay.py fail if that slips).

The seek is what makes a relocated server findable: the server walks the range in
exactly the same ascending order (mcp_port.py owns both halves), so "server moved" is
never "client broken".

Because server.py refuses to start a duplicate (it owns the single Firefox
WebSocket on :8772), the start race between simultaneously-launched instances is
safe: one server wins the port, the others exit, and every launcher proxies to
the survivor. Closing Claude Code kills only the launcher/proxy, never the
detached server.

All diagnostics go to STDERR; stdout is the MCP JSON-RPC channel.
"""
import os
import subprocess
import sys
import time

# NB asyncio is deliberately NOT imported here: it costs 3.2 MB, and the relay runs on
# threads. The proxy transport's event loop lives in its own process (server/proxy/).

ROOT = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(ROOT)
SERVER = os.path.join(PROJECT_ROOT, "server", "server.py")

# Run as a script, only this folder is on sys.path. The shared modules live in shared-py/
# (a folder, not a package: it holds the one package `browserplug_shared`) and the sibling
# modules import as `launcher.<name>` from the repo root; both paths come from __file__,
# never from the cwd, so a client that spawns this from anywhere finds them.
for _path in (PROJECT_ROOT, os.path.join(PROJECT_ROOT, "shared-py")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

# The port policy (default, range, the BROWSERPLUG_MCP_PORT pin, the /health identity check)
# is shared with server.py: one module, so the server and its clients can never walk
# different sequences.
from browserplug_shared.mcp_port import (CREATE_NO_WINDOW, DETACHED_ENV, LOG_FILE_ENV,  # noqa: E402
                                         MCP_PORT_DEFAULT, MCP_PORT_ENV, MCP_PORT_LAST,
                                         PROBED_PORT_KEY, SPAWN_ENV_DENIED, default_log_path,
                                         detached_spawn_kwargs, browserplug_health,
                                         connect_host, existing_server_python,
                                         health_url as mcp_health_url,
                                         pinned_mcp_port, resolve_server_python,
                                         seek_browserplug, server_pid_is_ours,
                                         restart_startup_log_glob, server_spawn_env,
                                         spawn_startup_log_path, startup_log_glob,
                                         startup_log_path)

# One version number for the whole Python side: the SAME module server.py imports, so a
# launcher and the server it proxies to can only ever disagree by being different builds
# on disk, which is exactly what the skew warning is for.
from launcher.client_pid import client_pid  # noqa: E402
from browserplug_shared.version import (HEALTH_IDENTITY_HEADER, HEALTH_IDENTITY_VALUE,  # noqa: E402
                                        LAUNCHER_PID_HEADER,
                                        LAUNCHER_VERSION_HEADER,
                                        __version__ as BROWSERPLUG_VERSION)


def log(*args):
    print("[browserplug-launcher]", *args, file=sys.stderr, flush=True)


def health_url(port):
    return mcp_health_url(port)


def launcher_identity_headers():
    """Which launcher process this is, and which build it runs.

    Stamped on the /health poll (not on the MCP payload path): /health is answered
    before any MCP session exists and keeps answering for the launcher's whole life,
    so this reports a version even for a launcher whose calls never reach a browser.

    The poll also asks for the identity-only reply. It reads server_pid and
    tools_signature and nothing else, and the full report costs the server process scans.
    """
    return {LAUNCHER_PID_HEADER: str(os.getpid()),
            LAUNCHER_VERSION_HEADER: BROWSERPLUG_VERSION,
            HEALTH_IDENTITY_HEADER: HEALTH_IDENTITY_VALUE}


def backend_url(port):
    """The shared server's MCP endpoint, for the proxy transport. `/mcp` with no trailing
    slash, as the relay posts it: `/mcp/` costs a 307 on every request."""
    return f"http://{connect_host('localhost')}:{port}/mcp"


def find_server_port(timeout=None, start_port=MCP_PORT_DEFAULT, last_port=MCP_PORT_LAST):
    """The port the shared BrowserPlug server is on, or None.

    A pin (BROWSERPLUG_MCP_PORT) is taken at its word: probe only that port, never seek; the
    user asked for that port and a server elsewhere is not the one they meant.

    Otherwise walk 2830 → 2942 in order and take the first port whose /health identifies
    as BrowserPlug. The identity check matters: a stranger's HTTP server on 2830 must not be
    mistaken for ours (we would proxy MCP into it), so we keep walking past it.

    The band is a parameter only so the seek can be exercised over a FREE window: on a
    machine where the always-on server owns 2830, tests pinned to the real band could only
    skip themselves, and a test that always skips is a test that does not exist.
    """
    pin = pinned_mcp_port()
    kw = {} if timeout is None else {"timeout": timeout}
    if pin is not None:
        health = browserplug_health(pin, **kw)
        return pin if health is not None and _is_our_server(health, pin) else None
    port, _health = seek_browserplug(start_port, last_port, accept=_is_our_server, **kw)
    return port


def _is_our_server(health, port=None):
    """A /health that says BrowserPlug is adopted only if its server_pid is this
    checkout's server run by this user and owns the socket that answered
    (mcp_port.server_pid_is_ours; the seek stamps the port it reached into `health`); else
    the port is someone else's and the walk goes on."""
    if port is None:
        port = health.get(PROBED_PORT_KEY)
    if server_pid_is_ours(health.get("server_pid"), PROJECT_ROOT, port=port):
        return True
    log(f"ignoring a BrowserPlug-looking /health from pid {health.get('server_pid')}: "
        f"not this checkout's server, or not this user's")
    return False


def spawn_detached_server():
    """Start server.py as a process detached from this launcher's lifetime.

    It is spawned on the pinned port if there is one (and fails if that is busy), else
    with no port at all, so the server starts at 2830 and walks up the range itself. That is why the caller
    re-SEEKS afterwards instead of watching one fixed port.
    """
    # An allowlisted copy, not the client's whole environment: the server outlives this
    # client and /restart carries the environment forward (mcp_port.server_spawn_env).
    env = server_spawn_env()
    # default_log_path(), not a literal join: the server falls back to the same function
    # when the variable is unset, so the two can never name different files.
    env.setdefault("BROWSERPLUG_LOG_FILE", default_log_path())
    # Pinned: tell the server so it never relocates. Unpinned: pass NO port, because an
    # explicit --mcp-port switches the server's own search off for a lock loser and reads
    # as a pin in its logs; it starts at 2830 and walks up by itself.
    args = [resolve_server_python(PROJECT_ROOT, warn=lambda m: log("WARNING:", m)), SERVER]
    pin = pinned_mcp_port()
    if pin is not None:
        args += ["--mcp-port", str(pin), "--mcp-port-pinned"]
    # Capture the child's stdout/stderr instead of DEVNULLing it: anything that kills
    # server.py BEFORE its logging is configured (bad interpreter, import error, a
    # syntax error in an edit) writes only to stderr, and with DEVNULL that crash left
    # ZERO trace anywhere while the launcher just reported a 30s connect timeout.
    #
    # It must NOT be BROWSERPLUG_LOG_FILE. The child's own RotatingFileHandler owns that
    # path, and handing the child a SECOND, life-long handle to it means the rename at
    # rollover fails forever (WinError 32); every subsequent record then emits a
    # ~30-line handler traceback to stderr, i.e. back into the same file. That is not
    # hypothetical: it is why browserplug-server.log reached 37 MB against a 2 MB cap with
    # 13k rotation-error blocks. One writer per file.
    #
    # One file per spawn: it holds one server's startup, not history, and the child drops
    # its stderr log handler once its file handler is up (BROWSERPLUG_DETACHED), so
    # nothing streams into it afterwards. The name is unique (launcher pid + epoch ms)
    # because two launchers spawning together would open one shared name with "w" and
    # truncate each other's capture.
    env[DETACHED_ENV] = "1"
    startup_path = spawn_startup_log_path(
        env["BROWSERPLUG_LOG_FILE"], f"{os.getpid()}-{int(time.time() * 1000)}")
    _prune_startup_logs(env["BROWSERPLUG_LOG_FILE"], keep=startup_path)
    try:
        child_out = open(startup_path, "w", buffering=1)
    except OSError:
        child_out = subprocess.DEVNULL
    kwargs = dict(
        stdin=subprocess.DEVNULL,
        stdout=child_out,
        stderr=child_out,
        env=env,
        cwd=PROJECT_ROOT,
    )
    # Windowless AND detached -- see detached_spawn_kwargs() for why the obvious
    # DETACHED_PROCESS is the flag that made a terminal window appear here.
    kwargs.update(detached_spawn_kwargs())
    try:
        # The handle is kept (and returned): ensure_server polls it so a child that dies
        # at once is noticed in a poll interval, not after the whole wait.
        child = subprocess.Popen(args, **kwargs)
    finally:
        # The child inherited its own handle; this launcher must not keep the log
        # file open for its whole lifetime.
        if child_out is not subprocess.DEVNULL:
            child_out.close()
    return SpawnedServer(child, startup_path if child_out is not subprocess.DEVNULL else None)


class SpawnedServer:
    """What spawn_detached_server started: the Popen handle and its startup capture
    (None when the capture file could not be opened)."""

    def __init__(self, child, startup_log):
        self.child = child
        self.startup_log = startup_log


STARTUP_LOGS_KEPT = 5


def _prune_startup_logs(log_file, keep):
    """Per-spawn capture files would otherwise pile up forever; keep the newest few."""
    import glob
    try:
        found = [f for f in glob.glob(startup_log_glob(log_file))
                 if os.path.abspath(f) != os.path.abspath(keep)
                 and os.path.abspath(f) != os.path.abspath(startup_log_path(log_file))]
        found.sort(key=os.path.getmtime, reverse=True)
        for old in found[max(STARTUP_LOGS_KEPT - 1, 0):]:
            try:
                os.remove(old)
            except OSError:
                pass  # held open by a live child, or already gone
    except OSError:
        pass


def _startup_tail(path, max_bytes=4096, max_lines=25):
    """The end of a startup capture, where a traceback's last frames and exception are."""
    if not path:
        return ""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(size - max_bytes, 0))
            data = f.read()
    except OSError:
        return ""
    lines = data.decode("utf-8", "replace").strip().splitlines()
    return "\n".join(lines[-max_lines:])


def _lost_the_lock(code, tail):
    """A child that lost the single-instance lock to an incumbent that is still shutting
    down: it refused deliberately (exit 1, no traceback) and said so. Worth one respawn;
    a crash with a traceback is not, since the second one dies the same way."""
    return code == 1 and "Traceback" not in tail and (
        "single-instance lock" in tail or not tail)


# Where a /restart replacement's capture goes when the server ran with BROWSERPLUG_LOG_FILE
# unset (with it set, the replacement writes beside the launcher's own captures).
RESTART_STARTUP_HINT = restart_startup_log_glob(os.path.join(PROJECT_ROOT, "server"))

# Why ensure_server returned None, for main() to put in the error the client sees.
spawn_failure = None
# The server_env.ServerEnvError behind spawn_failure, when that is what stopped the spawn.
# serve_via_proxy refuses with it rather than run the same lookup a second time.
spawn_error = None
# What main()'s ensure_server() said: True when a server answered, False when it fell back
# to a port nothing answered on, None when main() has not run (serve_via_proxy called on
# its own). With a server running, the proxy's interpreter is looked up read-only.
server_found = None


# A seek is either one probe (server on 2830) or a skip-walk over empty ports; both are
# milliseconds. Anything near a second means ports are answering slowly again: the
# failure that used to eat the client's whole 30 s connect budget in total silence,
# because nothing was logged until AFTER the walk finished. Now it leaves a mark.
SLOW_SEEK_WARN_SECONDS = 1.0


def ensure_server():
    """Return the port of a healthy shared server, spawning one if none is running."""
    started = time.monotonic()
    port = find_server_port()
    elapsed = time.monotonic() - started
    if elapsed >= SLOW_SEEK_WARN_SECONDS:
        log(f"WARNING: seeking the shared server over "
            f":{MCP_PORT_DEFAULT}-{MCP_PORT_LAST} took {elapsed:.1f}s "
            f"(expected well under {SLOW_SEEK_WARN_SECONDS:.0f}s). If this approaches the "
            f"client's connect timeout, the launcher is killed mid-seek and never reaches "
            f"the spawn below — see _seek_skippable in shared-py/browserplug_shared/mcp_port.py.")
    if port is not None:
        return port
    pin = pinned_mcp_port()
    where = f":{pin}" if pin is not None else f":{MCP_PORT_DEFAULT}-{MCP_PORT_LAST}"
    log(f"no BrowserPlug server found on {where}; spawning a detached instance")
    global spawn_failure, spawn_error
    spawn_failure = spawn_error = None
    spawned = _spawn_logged()
    if spawned is None:
        return _seek_after_failed_spawn()
    respawned = False
    # ~20s in total (40 polls of 0.5s), shared across a respawn. Seek each time: if the
    # port it was asked for was taken, it comes up further along the range.
    for _ in range(40):
        port = find_server_port()
        if port is not None:
            return port
        code = spawned.child.poll() if spawned is not None else None
        if code is not None:
            # The child is gone: waiting on it is pointless. A peer's server may have
            # answered in the meantime (a lock loser exits 0 then), so seek once more.
            port = find_server_port()
            if port is not None:
                return port
            tail = _startup_tail(spawned.startup_log)
            if not respawned and _lost_the_lock(code, tail):
                # The incumbent is still shutting down (/restart handover) and holds the
                # lock for a moment yet. Give it a moment, then try once more.
                log(f"spawned server exited with code {code} after losing the instance "
                    f"lock; respawning once")
                respawned = True
                time.sleep(2.0)
                port = find_server_port()
                if port is not None:
                    return port
                spawned = _spawn_logged()
                if spawned is None:
                    return _seek_after_failed_spawn()
                continue
            spawn_failure = (
                f"the spawned server exited with code {code} before it bound a port. "
                f"Startup log {spawned.startup_log or '(none captured)'}"
                + (f", last lines:\n{tail}" if tail else " was empty"))
            log("ERROR:", spawn_failure)
            return None
        time.sleep(0.5)
    if spawned is not None and spawned.startup_log:
        spawn_failure = (f"the spawned server was still not answering after ~20s. "
                         f"Startup log: {spawned.startup_log}")
    return None


def _seek_after_failed_spawn():
    """The answer when no server could even be spawned: one more seek (a peer launcher's
    server may have bound meanwhile), never the ~20 s wait for a child that does not
    exist. spawn_failure already says why."""
    global spawn_failure, spawn_error
    port = find_server_port()
    if port is not None:
        spawn_failure = spawn_error = None
    return port


def _spawn_logged():
    """spawn_detached_server(), or None when nothing was started.

    The reason goes into spawn_failure at once: with no child there is nothing to wait for,
    and main() puts it in the ERROR the client's log shows. For a server_env.ServerEnvError
    (no uv, a stale lock, a broken environment) that text names the remedy, and the error
    itself is kept in spawn_error."""
    global spawn_failure, spawn_error
    try:
        return spawn_detached_server()
    except Exception as e:
        from browserplug_shared.server_env import ServerEnvError
        spawn_failure = f"the server could not be started: {e}"
        spawn_error = e if isinstance(e, ServerEnvError) else None
        log("ERROR: spawn failed:", e)
        return None


RELAY_ENV = "BROWSERPLUG_LAUNCHER_RELAY"


def relay_enabled():
    """Whether to serve stdio with the stdlib relay (the default) or the proxy transport.

    Set BROWSERPLUG_LAUNCHER_RELAY=0 to get the proxy back. The escape hatch is here
    because the relay owns the transport every tool call rides on, and a bug in it has no
    workaround from the client side; flipping one variable is a faster recovery than
    reinstalling a build. Anything other than an explicit off value means the relay.
    """
    return (os.environ.get(RELAY_ENV) or "").strip().lower() not in ("0", "false", "no")


def main():
    log(f"launcher v{BROWSERPLUG_VERSION}")
    relay = relay_enabled()
    port = ensure_server()
    global server_found
    server_found = port is not None
    if port is not None:
        log(f"shared server healthy on :{port}, "
            f"{'relaying' if relay else 'proxying'} stdio")
        # Say it once, here, where a human debugging a weird call is already looking:
        # /health carries the same finding for agents (version_warning).
        try:
            server_version = (browserplug_health(port) or {}).get("server_version")
            if server_version and server_version != BROWSERPLUG_VERSION:
                log(f"WARNING: version mismatch: this launcher is v{BROWSERPLUG_VERSION}, the "
                    f"shared server is v{server_version}. They came from different "
                    f"checkouts; restart this client (or the server) to match.")
        except Exception:
            pass
    else:
        # Nothing answered anywhere in the range, and ensure_server already spawned one
        # and waited ~20s on it. Both transports still start, aimed at the port we would
        # have used, and both fail the client's initialize: the relay with a -32603 naming
        # the port, the proxy with its handshake gate's error naming the backend URL
        # (server/proxy/front.py). A session that opened empty would look connected and
        # stay empty, because the tool watch takes its baseline from the first /health it
        # reads and announces no server's first appearance.
        #
        # Retrying the spawn here is NOT the fix — the wait above already cost ~20s
        # against a client connect budget of ~30s, and a second one gets this launcher
        # killed mid-wait, which is the failure mode that leaves no log at all. So start
        # the transport anyway (the client sees the same failed connect either way) and
        # spend the remaining time saying, in the one place a human is already looking,
        # that the session is going to fail and what actually broke.
        port = pinned_mcp_port() or MCP_PORT_DEFAULT
        startup_log = startup_log_glob(os.environ.get(LOG_FILE_ENV) or default_log_path())
        detail = (f"{spawn_failure} " if spawn_failure else "")
        where = (f"{detail}read the newest {startup_log} (a stale mtime there means the "
                 f"spawn was never even reached; a /restart's replacement writes beside "
                 f"it, or to {RESTART_STARTUP_HINT} when the server it replaced ran with "
                 f"{LOG_FILE_ENV} unset), then run `{sys.executable} "
                 f"{os.path.join(PROJECT_ROOT, 'server', 'preflight.py')}`.")
        if relay:
            # The relay forwards the client's initialize to :{port}. With nothing
            # listening it answers that initialize with a -32603 naming the port, so the
            # handshake fails here too, unless a server binds before the relay reads it.
            log(f"ERROR: no healthy BrowserPlug server found (checked "
                f"{MCP_PORT_ENV} pin or :{MCP_PORT_DEFAULT}-{MCP_PORT_LAST}) after "
                f"spawning one and waiting. Relaying to :{port} anyway; this client's MCP "
                f"session will fail to initialize unless a server answers there first, "
                f"and needs a reconnect once one does. The server died before it could "
                f"bind, or never started: {where}")
        else:
            log(f"ERROR: no healthy BrowserPlug server found (checked "
                f"{MCP_PORT_ENV} pin or :{MCP_PORT_DEFAULT}-{MCP_PORT_LAST}) after "
                f"spawning one and waiting. Proxying to :{port}, but this client's MCP "
                f"session will FAIL to initialize rather than start empty, because the "
                f"proxy's handshake gate requires a reachable backend. The server died "
                f"before it could bind, or never started: {where}")

    if relay:
        return serve_via_relay(port)
    return serve_via_proxy(port)


def relay_era_setting():
    """The mcp.era setting the relay runs under (handshake, direct or all).

    Only `all` makes the relay carry 2026-07-28 (launcher/relay_era.py). A setting that
    cannot be read leaves the relay on the handshake era, where every client works, rather
    than stopping the launcher."""
    try:
        from browserplug_shared import mcp_era
        setting = mcp_era.resolve()
    except Exception as e:
        log(f"WARNING: could not read the MCP era setting ({e}); the relay stays on the "
            f"handshake era")
        return "handshake"
    for skipped in setting.ignored:
        log(f"WARNING: ignoring {skipped}: not one of {', '.join(mcp_era.ERA_VALUES)}")
    log(f"MCP era {setting.era} ({setting.source})")
    return setting.era


def serve_via_relay(port):
    """Serve stdio with the stdlib relay. See launcher/stdio_relay.py for why it exists."""
    from launcher.stdio_relay import run_relay
    # ensure_server is the recovery: on a broken connection the relay seeks the server
    # again and respawns it if none answers, the same steps as at startup (L2).
    return run_relay(port, log=log, identity_headers=launcher_identity_headers(),
                     recover=ensure_server, recover_detail=lambda: spawn_failure,
                     era=relay_era_setting())


# The proxy transport's entry, run by absolute path: the child's cwd cannot place a
# relative one, and server/reporting/client_identity.py recognises the proxy hop by it.
PROXY_ENTRY = os.path.join(PROJECT_ROOT, "server", "proxy", "__main__.py")


def proxy_command(python, port):
    """The proxy child's argv. server/proxy/policy.parse_args reads it; this module may not
    import server/, so tests/unit/test_proxy_policy.py parses what this builds.

    The launcher passes what only it knows: the client that spawned it (the backend's
    client-pid header), and its own pid and build (the /health and /timing identity, so
    the server's map shows this launcher, not the proxy child)."""
    return [python, PROXY_ENTRY,
            "--backend-url", backend_url(port),
            "--client-pid", str(client_pid()),
            "--launcher-pid", str(os.getpid()),
            "--launcher-version", BROWSERPLUG_VERSION]


def _proxy_spawn_kwargs():
    """The child gets this process's stdin and stdout (the client's MCP session runs there)
    named explicitly, so the creation flags cannot swap them for a console's, and the
    checkout as its cwd rather than the client's project. On Windows CREATE_NO_WINDOW: a
    launcher spawned with no console would otherwise hand the child a visible one.

    Its environment is this one minus SPAWN_ENV_DENIED, as for the server: a PYTHONHOME
    that suits this launcher's interpreter would point the server environment's python at
    another installation's standard library, and server_env's import probe, which
    vouched for that python, ran without it."""
    kwargs = {"stdin": 0, "stdout": 1, "cwd": PROJECT_ROOT,
              "env": {name: value for name, value in os.environ.items()
                      if name.upper() not in SPAWN_ENV_DENIED}}
    if os.name == "nt":
        kwargs["creationflags"] = CREATE_NO_WINDOW
    return kwargs


def refuse_stdio(message, stdin=None, out=None):
    """Answer every request on stdin with a JSON-RPC error carrying `message`, until EOF.

    For a proxy transport that cannot start: a client whose launcher just exits sees a
    closed connection and nothing else, while this fails its initialize with the reason,
    as the proxy's handshake gate does for a backend that is not there. Returns 1."""
    import json
    stream = stdin if stdin is not None else sys.stdin.buffer
    out = out if out is not None else sys.stdout.buffer
    for line in stream:
        try:
            frame = json.loads(line)
        except ValueError:
            continue
        if not isinstance(frame, dict) or "method" not in frame or frame.get("id") is None:
            continue
        reply = {"jsonrpc": "2.0", "id": frame["id"],
                 "error": {"code": -32603, "message": message}}
        out.write(json.dumps(reply).encode("utf-8") + b"\n")
        out.flush()
    return 1


def _refuse_proxy(detail):
    message = (f"BrowserPlug launcher: the proxy transport ({RELAY_ENV}=0) could not "
               f"start: {str(detail).rstrip('.')}. Unset {RELAY_ENV} to use the default "
               f"relay.")
    log("ERROR:", message)
    return refuse_stdio(message)


# Finding the proxy's interpreter, against a client's ~30 s connect budget. With a server
# running it is two read-only uv calls; with none, ensure_server's spawn has just built or
# checked the same environment.
PROXY_PYTHON_SECONDS = 10.0

# A proxy child that exits non-zero sooner than this died at startup (an import error, a
# PYTHONHOME, a bad argument), before it could answer the client's initialize, which is
# then still unread on stdin.
PROXY_STARTUP_SECONDS = 15.0


def _proxy_python():
    """The proxy child's interpreter: the server environment's python.

    When main() found a server, it runs from that environment, so the lookup is read-only
    (existing_server_python): ensure_server_python would sync the environment under it.
    When it found none, ensure_server_python as a spawn runs it, unless the spawn already
    failed on it: that ServerEnvError (spawn_error) stands and is not looked up again."""
    deadline = time.monotonic() + PROXY_PYTHON_SECONDS
    if server_found:
        return existing_server_python(PROJECT_ROOT, deadline=deadline)
    if spawn_error is not None:
        raise spawn_error
    return resolve_server_python(PROJECT_ROOT, warn=lambda m: log("WARNING:", m),
                                 deadline=deadline)


def _wait_interruptibly(child):
    """child.wait() in half-second slices. On Windows a wait with no timeout is one
    WaitForSingleObject(INFINITE), which a Ctrl-C does not interrupt: the KeyboardInterrupt
    would surface only after the child had ended by itself."""
    while True:
        try:
            return child.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            continue


def serve_via_proxy(port):
    """Serve stdio with the proxy transport: server/proxy/ as a child on this stdio.

    The child is the server environment's python (server_env, the same interpreter the
    server runs on): the proxy needs the MCP SDK, which this launcher's interpreter does
    not have. Its exit code is the launcher's, except that a child that dies non-zero
    within PROXY_STARTUP_SECONDS gets the client's pending requests refused with the
    reason, like a child that could not be found or started. The backend port is fixed for
    the child's life, as it was for the in-process proxy: /restart brings the replacement
    back on the same port, and every request opens a session of its own, so a restart costs
    nothing here. The relay, not this path, re-seeks a server that moved or died.
    """
    try:
        python = _proxy_python()
    except Exception as e:      # server_env.ServerEnvError: its text names the remedy
        return _refuse_proxy(f"no python for it: {e}")
    command = proxy_command(python, port)
    started = time.monotonic()
    try:
        child = subprocess.Popen(command, **_proxy_spawn_kwargs())
    except OSError as e:
        return _refuse_proxy(f"{command[0]} did not start: {e}")
    try:
        code = _wait_interruptibly(child)
    except KeyboardInterrupt:
        # With a console of its own the child never saw the Ctrl-C. Ending the server
        # environment's trampoline ends its interpreter too (the trampoline's job object).
        child.terminate()
        child.wait()
        return 130
    lived = time.monotonic() - started
    if code not in (0, 130) and lived < PROXY_STARTUP_SECONDS:
        return _refuse_proxy(f"its process ({command[0]}) exited with code {code} "
                             f"{lived:.1f} s after it started; its traceback is in this "
                             f"launcher's stderr log")
    return code


if __name__ == "__main__":
    sys.exit(main())
