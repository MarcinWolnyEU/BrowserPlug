#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Control the BrowserPlug server from a shell: start, restart, shutdown, status.

    python server/control.py start
    python server/control.py restart
    python server/control.py shutdown
    python server/control.py status

`start` is what a server starter (a logon task, a shortcut) runs. When this checkout's server
already answers /health it says so and exits. Otherwise it runs launcher/launcher.py as a
process with nothing on its stdin, so the launcher does exactly what it does for an MCP
client: it seeks the server, builds or updates the server environment while nothing runs from
it (shared-py/browserplug_shared/server_env.py), spawns server/server.py detached and waits for
/health, then reads EOF and exits. Its log streams to this process's stderr. `start` returns
once the server answers. It needs only the standard library: run it with any Python 3.10 or
later, the venv's from the README's step 1 for one. The server it starts runs on
BROWSERPLUG_SERVER_PYTHON when that is set in this process's environment, otherwise on the
environment uv builds.

/restart and /shutdown demand a signed request (X-BrowserPlug-Auth), so a bare `curl -X
POST` no longer works and neither does any web page. This script signs with a device token
derived from the master token, read the way the server reads it: BROWSERPLUG_WS_TOKEN, else
the `ws-token` file in the state directory (BROWSERPLUG_STATE_DIR relocates it). The port
is found the way every client finds it: BROWSERPLUG_MCP_PORT pins one, otherwise the seek
walks 2830..2942 and takes the first BrowserPlug.

The launcher's /timing report signs through `signed_headers` in
shared-py/browserplug_shared/local_auth.py, so there is one place that knows how a local
caller proves the token. Stdlib only, no import of the server.

Exit status: 0 done, 1 the server refused, answered badly or did not start, 2 no server or no
token.
"""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

# The signing helpers live in shared-py/, which is not on the path when this file runs as a
# script. Computed from __file__, never from the cwd.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SHARED_PY = os.path.join(_ROOT, "shared-py")
if _SHARED_PY not in sys.path:
    sys.path.insert(0, _SHARED_PY)

from browserplug_shared import mcp_port, proc_facts, ws_token  # noqa: E402
from browserplug_shared.local_auth import (LABEL, master_token,  # noqa: E402,F401
                                           signed_headers)

# What `start` runs, by path and as a process of its own: server/ never imports the launcher
# (tests/unit/test_import_boundaries.py), and running it reuses its seek and its spawn.
LAUNCHER = os.path.join(_ROOT, "launcher", "launcher.py")

# A server told to stop keeps answering /health for the fraction of a second before it
# exits. `shutdown` waits for the process to be gone (up to this long); `start` re-reads
# /health after this pause before it calls a server running.
SHUTDOWN_WAIT_SECONDS = 10.0
START_SETTLE_SECONDS = 0.5


def find_port():
    """The MCP port of the running server, or None."""
    pinned = mcp_port.pinned_mcp_port()
    if pinned is not None:
        return pinned if mcp_port.browserplug_health(pinned) is not None else None
    port, _health = mcp_port.seek_browserplug()
    return port


def find_our_server():
    """(port, health) of this checkout's server run by this user, else (None, None).

    The launcher's own acceptance test (mcp_port.server_pid_is_ours), so `start` never takes
    another checkout's server, or a stranger imitating /health, for this one."""
    def ours(health, port=None):
        # The seek stamps the port it reached into the health it hands over.
        port = port or health.get(mcp_port.PROBED_PORT_KEY)
        return mcp_port.server_pid_is_ours(health.get("server_pid"), _ROOT, port=port)
    pinned = mcp_port.pinned_mcp_port()
    if pinned is not None:
        health = mcp_port.browserplug_health(pinned)
        return (pinned, health) if health is not None and ours(health, pinned) else (None, None)
    return mcp_port.seek_browserplug(accept=ours)


def _started(state, port, health):
    return json.dumps({"status": state, "port": port, "server_pid": health.get("server_pid"),
                       "server_version": health.get("server_version")})


def start():
    """`start`: report a running server, or run the launcher to start one. Exit status."""
    port, health = find_our_server()
    if port is not None:
        # One that was just told to stop still answers for a moment; call it running only if
        # it is still there afterwards.
        time.sleep(START_SETTLE_SECONDS)
        port, health = find_our_server()
    if port is not None:
        print(_started("running", port, health))
        return 0
    # Windowless, and its stderr piped and copied here: a starter without a console would
    # otherwise give the launcher a visible window of its own (mcp_port.detached_spawn_kwargs).
    flags = {"creationflags": mcp_port.CREATE_NO_WINDOW} if os.name == "nt" else {}
    with subprocess.Popen([sys.executable, LAUNCHER], stdin=subprocess.DEVNULL,
                          stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, cwd=_ROOT,
                          **flags) as child:
        # pythonw.exe, or a parent that gave us no handles, leaves sys.stderr None. The pipe
        # is still read to EOF: closing it early would kill the launcher on its next log line.
        relay = sys.stderr
        for line in child.stderr:
            if relay is None:
                continue
            try:
                # The launcher printed in text mode: its CRLF would show as a blank line here.
                relay.write(line.decode("utf-8", "replace").replace("\r\n", "\n"))
                relay.flush()
            except (OSError, ValueError):
                relay = None
    code = child.returncode
    port, health = find_our_server()
    if port is not None:
        print(_started("started", port, health))
        return 0
    print(f"start failed: no BrowserPlug server of {_ROOT} answers /health after the "
          f"launcher's spawn (launcher exit code {code}); its log above says why",
          file=sys.stderr)
    return 1


def wait_stopped(port, pid, timeout=None):
    """True once the server that answered on `port` as `pid` is gone: /health no longer
    answers as that pid and the process has exited, so its instance lock (held for the life
    of the process) is released too. False after `timeout` seconds (default
    SHUTDOWN_WAIT_SECONDS)."""
    give_up = time.monotonic() + (SHUTDOWN_WAIT_SECONDS if timeout is None else timeout)
    while True:
        health = mcp_port.browserplug_health(port, timeout=1.0)
        answering = health is not None and (pid is None or health.get("server_pid") == pid)
        if not answering and (pid is None or not proc_facts.pid_alive(pid)):
            return True
        if time.monotonic() >= give_up:
            return False
        time.sleep(0.1)


def call(action, port, timeout=30):
    """(status, decoded body) of one control request."""
    method, path = ("GET", "/health") if action == "status" else ("POST", "/" + action)
    headers = {} if action == "status" else signed_headers(method, path)
    req = urllib.request.Request(
        f"http://{mcp_port.connect_host('localhost')}:{port}{path}",
        data=None if method == "GET" else b"", method=method, headers=headers)
    # Never route a loopback call through HTTP_PROXY.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as r:
            status, raw = r.status, r.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read()
    try:
        return status, json.loads(raw.decode("utf-8", "replace") or "{}")
    except ValueError:
        return status, {"raw": raw[:200].decode("utf-8", "replace")}


def describe_last_restart(record):
    """One line for /health's `last_restart`, or None when the server never restarted."""
    if not isinstance(record, dict):
        return None
    line = (f"last restart: {record.get('trigger')} at {record.get('time')} -> "
            f"{record.get('outcome')}")
    if record.get("reason"):
        line += f" ({record['reason']})"
    return line


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1 or args[0] not in ("start", "restart", "shutdown", "status"):
        print(__doc__.split("\n\n")[0], file=sys.stderr)
        return 2
    action = args[0]
    if action == "start":
        return start()
    port = find_port()
    if port is None:
        print("no BrowserPlug server answers on the MCP port range", file=sys.stderr)
        return 2
    if action != "status" and not master_token():
        print(f"no token: set BROWSERPLUG_WS_TOKEN or create {ws_token.default_token_path()}",
              file=sys.stderr)
        return 2
    server_pid = None
    if action == "shutdown":
        server_pid = (mcp_port.browserplug_health(port, timeout=2.0) or {}).get("server_pid")
    try:
        status, body = call(action, port)
    except OSError as e:
        # A shutdown can drop the connection while it answers; the server is going either way.
        if action != "shutdown":
            print(f"{action} failed: {e}", file=sys.stderr)
            return 1
        status, body = 200, None
    if status == 200 and action == "shutdown" and not wait_stopped(port, server_pid):
        print(f"shutdown was accepted, but the server (pid {server_pid}) still answers on "
              f"port {port} after {SHUTDOWN_WAIT_SECONDS:.0f} s", file=sys.stderr)
        return 1
    if status == 200:
        print("shutdown requested" if body is None else json.dumps(body))
        if action == "status":
            line = describe_last_restart(body.get("last_restart"))
            if line:
                # stderr, so a script that parses the JSON on stdout is unaffected.
                print(line, file=sys.stderr)
        return 0
    print(f"{action} refused with HTTP {status}: {body.get('error', body)}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
