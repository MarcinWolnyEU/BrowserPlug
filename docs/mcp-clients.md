# MCP clients

[install.md](install.md#registering-mcp-clients) shows how to register BrowserPlug. This page
adds where clients keep it, the protocol versions the server speaks, and how clients are named.

## Where each client keeps its registration

Each client has its own config; adding BrowserPlug to one does not add it to the others. On
Windows:

| Client | Config file | Key | Entry |
|---|---|---|---|
| Claude Code | `%USERPROFILE%\.claude.json` | `mcpServers` | `{"type":"stdio","command":"<python>","args":["<checkout>\\server\\launcher.py"],"env":{"BROWSERPLUG_SERVER_PYTHON":"<python>"}}` |
| VS Code, GitHub Copilot | `%APPDATA%\Code\User\mcp.json` | `servers` | the same stdio entry |
| GitLab Duo | `%APPDATA%\GitLab\duo\mcp.json` | `mcpServers` | `{"type":"http","url":"http://localhost:2830/mcp","approvedTools":true}` |
| pi | `%USERPROFILE%\.pi\agent\mcp.json` | `mcpServers` | `{"url":"http://localhost:2830/mcp","lifecycle":"keep-alive"}` |
| Codex | `%USERPROFILE%\.codex\config.toml` | `[mcp_servers.browserplug]` | written by `codex mcp add` ([install.md](install.md#codex)) |

`<python>` is the venv's interpreter ([install.md](install.md#a-venv)), by absolute path. The `env`
entry hands the same path to the launcher as `BROWSERPLUG_SERVER_PYTHON`, and it has to sit in
the client's own `env` block: some clients (Codex) start servers with a short list of
environment variables, so one set in your shell does not reach them. The launcher itself runs
on any Python 3.10 or later. On macOS and the BSDs it also needs psutil, which the venv's
interpreter has and a bare one lacks.

With uv installed you can skip the venv and the variable, and the launcher registers one of
two ways:

- **uv's managed interpreter**, by the path of its minor-version link, for example
  `C:\Users\you\AppData\Roaming\uv\python\cpython-3.12-windows-x86_64-none\python.exe`, with
  `<checkout>\server\launcher.py` as the argument and no `env` block. Preflight prints the path
  for your machine, as does `uv python find --managed-python 3.12`. Use the link, not a patch
  path (`cpython-3.12.14-…`), which breaks when uv removes that release. Windows and Linux
  only: elsewhere the bare interpreter has no psutil.
- **`uv run --script`**: command `<uv>`, args `run`, `--script` and the launcher path. Give
  uv's absolute path (`where uv`), since a client resolving a bare `uv` can pick up a `uv.exe`
  in the project folder on Windows. uv supplies psutil on macOS only; on the BSDs use the venv.

Stdio clients follow the server to another port; HTTP clients cannot, so if something else
uses 2830, pin a port with `BROWSERPLUG_MCP_PORT` ([server.md](server.md#ports)). Write the URL
as `/mcp`, without a trailing slash: `/mcp/` answers 307, and some clients do not follow it.

Preflight (`python server/preflight.py`) reads these configs, and a few others, and reports a
client that is registered but has not loaded the entry yet: restart that client.

GitLab Duo works best over HTTP: its stdio launcher gives the child a nonexistent working
directory, and the `spawn … ENOENT` error blames the command. When Duo fails to connect, look
in the GitLab Language Server's output in VS Code (search `[MCP]`).

## Protocol versions

The server speaks the versions with an `initialize` handshake (2024-11-05 through 2025-11-25),
which every current client uses, and the stateless 2026-07-28 version. If a client has trouble
with 2026-07-28, set `BROWSERPLUG_MCP_ERA` (or `era` under `mcp` in `config.json`):

| Value | Serves 2026-07-28 |
|---|---|
| `all` (default) | everywhere |
| `direct` | to HTTP clients and clients on the launcher's proxy transport (`BROWSERPLUG_LAUNCHER_RELAY=0`); clients behind the default stdio relay stay on the handshake versions |
| `handshake` | nowhere; such a request is refused with error -32022, and a client that cannot fall back cannot connect |

Restart the server after changing it, and restart any stdio client that already negotiated
2026-07-28: its launcher keeps the setting it started with.

## How clients are named

The popup's Connections list, `server_info` and the Options ▸ Health map name each MCP client
from its process tree: Claude Code, Codex CLI, GitHub Copilot Chat, Visual Studio Code, GitLab
Duo CLI, Kilo CLI, Mistral Vibe, pi, Reasonix and DSH (DeepSeek Harness). Clients send no
readable session name, so two instances of one client are told apart by working directory.

If a client shows up under the wrong name, right-click its row in the popup (or press and hold
it), choose "Copy connection info" and paste it into an issue. Key-, token- and
password-looking values in the command line it includes are masked.
