# Running and configuring the server

One BrowserPlug server serves every MCP client and every connected browser. The first client's
launcher starts it in the background, and it outlives that client. Installation is in
[install.md](install.md).

## Controlling it

```
python server/control.py start      # start it, unless one is already running
python server/control.py status     # print the /health report
python server/control.py restart    # replace the server, on the same port
python server/control.py shutdown   # stop it
```

`control.py` needs only the standard library: any Python 3.10 or later runs it.

`start` does what an MCP client's launcher does: it finds a running server or starts one
(updating uv's environment first if needed) and returns once the server answers. Use it in a
login task ([install.md](install.md#starting-the-server-at-login)). It prints one line of JSON: `"status"` is
`"running"` or `"started"`, with `port` and `server_pid`.

`restart` and `shutdown` sign their request with the token in the state directory; the server
refuses unsigned ones, so a bare `curl -X POST …/restart` gets 403. `shutdown` returns once
the server has exited, and fails if it still answers after 10 seconds. A restart picks up code
changes and keeps the port, and browsers reconnect within seconds. If the replacement fails to
start, the old server keeps running and `status` prints why (`last_restart`).

Exit codes: 0 done; 1 the server refused, answered badly or did not start; 2 no server or
token found, or bad arguments.

### Which Python runs the server

- `BROWSERPLUG_SERVER_PYTHON` names an interpreter: that one runs it. This is the venv from
  [install.md](install.md#a-venv), and BrowserPlug neither checks nor changes its packages.
- The variable is unset and uv is installed: uv builds `server-env` in the state directory from
  `server/server.py` and its lock file, and keeps it current.

With neither, no server starts; the launcher's log says uv was not found.

A restart never changes the environment under the running server. After a dependency change (a
`git pull` that touched the PEP 723 block in `server/server.py`), run `shutdown`, update the
packages (for a venv, the `pip install` line in [install.md](install.md#a-venv); with uv,
`start` does it and a restart is refused with `deps_changed`), then `start` or start any MCP
client.

On Windows the environment's `python.exe` is a stub, and the Python it starts owns the ports
(`/health` reports it as `server_pid`). Stop both with `control.py shutdown`.

## Ports

| Port | Used for | Moves? |
|---|---|---|
| 8772 | the browser extensions' WebSocket | never; a second server refuses to start while it is taken |
| 2830, up to 2942 | MCP over HTTP (`/mcp`) and the endpoints below | if 2830 is taken, the server takes the next free port, up to 2942 |

Launchers search the same range, so stdio clients find the server wherever it landed. An HTTP
client has a fixed URL: if something else uses 2830, pin a port with `BROWSERPLUG_MCP_PORT` and
use it in every HTTP client. A pinned port never moves; if it is taken, the server exits and
names the holder.

`--mcp-port` only sets where the search starts. A restart keeps the current port, so keep
ports out of autostart scripts unless every client uses the same one: a server outside 2830 to
2942 stays healthy on a port no launcher searches, and every stdio client times out.

A launcher adopts a server only when the process named in `/health` is this checkout's server,
runs as your user and owns the socket that answered. Anything else in the range is skipped
(the launcher's log says "ignoring a BrowserPlug-looking /health") and the launcher starts its
own. Behind Windows Firewall a down server looks like timeouts, not refusals.

## Configuration

### Getting a variable to the server

The server reads its environment once, at start, and every restart copies it. A variable you
add later needs a `shutdown` and a fresh start.

A launcher that starts the server passes on only `BROWSERPLUG_*`, the proxy variables
(`HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY`, `ALL_PROXY`), `PYTHON*` except `PYTHONHOME` and
`PYTHONEXECUTABLE`, `LC_*`, a few uv settings and operating-system basics such as `PATH`. API
keys and the rest stay out. A server you start directly gets your whole environment.

### Environment variables

| Variable | What it does | Default |
|---|---|---|
| `BROWSERPLUG_MCP_PORT` | pins the MCP port and turns off the search | search 2830 to 2942 |
| `BROWSERPLUG_WS_HOST` | bind address of the extension WebSocket, such as `0.0.0.0` for browsers on other machines. It locks the Options page's Local/LAN/WAN choice; `--ws-host` overrides it | `localhost` |
| `BROWSERPLUG_ALLOWED_HOSTS` | extra names the HTTP port accepts in the `Host` header, such as a dynamic DNS name; separate with `,` or `;` | none |
| `BROWSERPLUG_WS_TOKEN` | your own master token (`gen_ws_token.py` makes one) | generated |
| `BROWSERPLUG_WS_TOKEN_FILE` | where the generated token is kept | `ws-token` in the state dir |
| `BROWSERPLUG_WS_AUTH_TIMEOUT` | seconds a new browser has to authenticate | 15 (2 to 60) |
| `BROWSERPLUG_SERVER_PYTHON` | the interpreter that runs the server; set it in each MCP client's `env` block and wherever `control.py start` runs. A missing path is an error | unset: uv builds `server-env` |
| `BROWSERPLUG_UV` | full path of uv, when it is not on `PATH`; ignored when `BROWSERPLUG_SERVER_PYTHON` is set | `UV`, then `PATH`, then `~/.local/bin` |
| `BROWSERPLUG_STATE_DIR` | moves the state directory, `server-env` included | see below |
| `BROWSERPLUG_LOG_FILE` | the server's log file | `browserplug-server.log` in the checkout |
| `BROWSERPLUG_MCP_ERA` | which MCP protocol versions the server speaks ([mcp-clients.md](mcp-clients.md#protocol-versions)) | `all` |
| `BROWSERPLUG_MAX_CONCURRENT_REQUESTS`, `BROWSERPLUG_MAX_QUEUE_DEPTH`, `BROWSERPLUG_MAX_QUEUE_WAIT` | calls one browser runs at once; calls that may wait for it before new ones are refused as busy; seconds a call may wait | 3, 24, 60 |
| `BROWSERPLUG_RECONNECT_GRACE` | seconds a read waits for a dropped browser to return before it fails | 8 |
| `BROWSERPLUG_RESTART_DRAIN_SECONDS` | seconds a restart gives calls in flight to finish | 5 (at most 12) |
| `BROWSERPLUG_LAUNCHER_RELAY` | `0` switches the launcher to its alternative proxy transport | the relay |
| `BROWSERPLUG_WATCHDOG` | `0` turns off the watchdog | on |

`BROWSERPLUG_EXT_SCRIPTS`, `BROWSERPLUG_TOR_BIN`, `BROWSERPLUG_TOR_HOME` and
`BROWSERPLUG_BROWSER_LAUNCH_WAIT` are in [predefined-scripts.md](predefined-scripts.md),
[proxy-and-tor.md](proxy-and-tor.md) and [browsers.md](browsers.md#the-default-browser).

### Command-line flags

These are for running `server/server.py` by hand; clients and `control.py` start it through
the launcher.

| Flag | What it does |
|---|---|
| `--mcp-port N` | where the MCP port search starts; `BROWSERPLUG_MCP_PORT` pins one instead |
| `--port N` | the extensions' WebSocket port (8772) |
| `--ws-host ADDR` | bind address of the extension WebSocket; overrides `BROWSERPLUG_WS_HOST` |
| `--stdio` | serve MCP over stdin and stdout instead of HTTP |
| `--no-mcp` | the WebSocket only, no MCP |

`--stdio` and `--no-mcp` leave out the HTTP port and with it `/ws-token`, so a browser on the
same machine cannot fetch a token. Such a server lets local browsers connect without one,
unless `BROWSERPLUG_WS_TOKEN` is set. A client that runs `--stdio` starts a whole server of its
own, and while another server runs it exits at once.

### Where the state lives

The state directory is `%LOCALAPPDATA%\browserplug\` on Windows and
`$XDG_STATE_HOME/browserplug/` (`~/.local/state/browserplug/` when unset) on Linux and macOS.

- `config.json`: what the Options page changes on the server (the WebSocket bind and its LAN
  or WAN mode, the default browser and its startup arguments), plus `mcp.era` if you set it.
  Options ▸ "Reset all settings to Defaults" deletes it.
- `ws-token`: the master token, in its own file so a settings reset never touches it.
- `server-env/`: the uv-built Python environment, outside uv's cache. Deleting it while the
  server is stopped is safe; the next start rebuilds it.
- lock files that keep a second server from starting.

## Logs

The server writes a rotating log to `BROWSERPLUG_LOG_FILE`, or to `browserplug-server.log` at
the checkout root. Next to it, a server started in the background leaves `*.startup.log` files
(one per launcher spawn or restart; the newest five are kept) holding what it printed before
logging was set up. Look there when a server fails to start.

`browserplug-server.hang.log` belongs to the watchdog, which checks both event loops every 5
seconds. When a loop has been silent for 15 seconds on three checks in a row, it writes every
thread's stack to the hang log, restarts the server and logs a CRITICAL line. If the freeze
outlasts the restart by 30 seconds, it starts a new server, ends the old process and the new
server takes over the ports; a freeze that holds the whole interpreter is ended after 2
minutes by a timer outside Python, and then the next MCP client's launcher starts the server. Send the hang log with a bug report; past 512 KB, the next start moves it
to `browserplug-server.hang.log.old`. If the watchdog misfires, set `BROWSERPLUG_WATCHDOG=0`.

## HTTP endpoints

| Endpoint | Answers |
|---|---|
| `GET /health` | the full status as JSON: versions, browsers, clients, the last command, `last_restart`, and `version_warning` when the server, a launcher or an extension runs a different version (it names the one to update) |
| `GET /ready` | 200 while a browser is connected, 503 otherwise |
| `GET /doctor` | preflight's checks as JSON; localhost only |
| `/mcp` | the MCP endpoint; localhost only, whatever the WebSocket bind |

With the WebSocket bound beyond localhost (LAN or WAN mode), the MCP port listens there too,
and a remote caller needs a signed request for every endpoint except the two localhost-only
ones. Restart, shutdown and the Options page's own requests are signed even from localhost;
use `control.py` for those.

## When something is wrong

Run `python server/preflight.py` (Python 3.9 or later). It works while the server is down, and
each failed check names its fix, including the kill command for a stuck server (both ports
listening, `/health` silent) that the watchdog has not already restarted.
[install.md](install.md#troubleshooting) covers the common cases.
