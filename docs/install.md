# Installing BrowserPlug

The README's quick start is the short path. This guide adds the alternatives, every client,
starting the server at login and troubleshooting. The store listings on addons.mozilla.org and
the Chrome Web Store are in review, so for now every install is from source.

## Prerequisites

- Python 3.11 or 3.12 for the server (its dependency block asks for `>=3.11,<3.13`; 3.13 and
  later are untested). The launcher, `control.py`, `build_xpi.py` and `gen_ws_token.py` use
  only the standard library and run on 3.10 or later, `preflight.py` on 3.9 or later.
- pip, inside a virtual environment. Upgrade it first: the pip that ships with Python 3.12 is
  too old for `--requirements-from-script`.
- Optional: [uv](https://docs.astral.sh/uv/getting-started/installation/), in place of the venv.
- Firefox Developer Edition 140 or later, or Chrome, Edge or Brave 138 or later. Zen and
  LibreWolf run the Firefox extension; [browsers.md](browsers.md#supported-versions) has the
  versions.

## The server's Python

### A venv

```powershell
# Windows (PowerShell), from the checkout
py -3.12 -m venv "$env:LOCALAPPDATA\browserplug\venv"
$py = "$env:LOCALAPPDATA\browserplug\venv\Scripts\python.exe"
& $py -m pip install --upgrade pip
& $py -m pip install --requirements-from-script server\server.py
```
```bash
# macOS / Linux, from the checkout
python3.12 -m venv ~/.local/state/browserplug/venv
py=~/.local/state/browserplug/venv/bin/python
"$py" -m pip install --upgrade pip
"$py" -m pip install --requirements-from-script server/server.py
```

Keep the venv outside the checkout: preflight warns about a client config that names
`<checkout>/.venv`. `--requirements-from-script` reads the dependency list from the PEP 723
block at the top of `server/server.py`. pip picks the newest releases inside its ranges, while
`server/server.py.lock` pins the versions the test suite runs on.

Then hand the interpreter to BrowserPlug as `BROWSERPLUG_SERVER_PYTHON`, in two places: each
MCP client's `env` block ([below](#registering-mcp-clients)), and your own environment, so
that `control.py` and a login task find it:

```powershell
# Windows: takes effect in new terminals
[Environment]::SetEnvironmentVariable('BROWSERPLUG_SERVER_PYTHON', "$env:LOCALAPPDATA\browserplug\venv\Scripts\python.exe", 'User')
```
```bash
# macOS / Linux: add to ~/.profile, ~/.zshrc or similar
export BROWSERPLUG_SERVER_PYTHON=~/.local/state/browserplug/venv/bin/python
```

After a `git pull` that changes the dependency block, stop the server, rerun the last `pip
install` line, and start it again ([server.md](server.md#which-python-runs-the-server)).

### uv instead

With uv installed and `BROWSERPLUG_SERVER_PYTHON` unset, uv builds the server's environment
from `server/server.py.lock` on the first start and keeps it current. Skip the venv and
register the launcher in one of the two uv forms in
[mcp-clients.md](mcp-clients.md#where-each-client-keeps-its-registration).

## Building the packages

```bash
python build_xpi.py
```

It writes `dist/firefox/browserplug@marcinwolny.net.xpi` and
`dist/chrome/browserplug-chromium.zip`, with `LICENSE`, `NOTICE` and `THIRD-PARTY-NOTICES`
inside each. Rebuild after every change to an extension tree.

```bash
python build_server_zip.py
```

writes `dist/packages/browserplug-server.zip`: `server/`, `launcher/` and `shared-py/` plus the
README and the three licence files, all under one `server-package/` folder. It leaves out
bytecode, logs, the managed Tor install and any local state. `make package` builds all three
files where `make` is installed.

A release carries the same zip as `browserplug-server-<version>.zip`. Its layout matches the
checkout's, so the paths in this guide apply under `server-package/`. It holds no tests,
extension sources or build scripts.

## Firefox

Use Firefox Developer Edition. A build from source is unsigned, and release and beta Firefox
lock `xpinstall.signatures.required` on, so they load it only as a temporary add-on that is gone
after a restart. Developer Edition, Nightly, ESR and unbranded builds honor the preference.
Download it from <https://www.mozilla.org/firefox/developer/>.

1. In `about:config`, accept the warning and set `xpinstall.signatures.required` to `false`.
2. In `about:addons`, open the gear menu, choose Install Add-on From File and pick
   `dist/firefox/browserplug@marcinwolny.net.xpi`.

The extension stays installed across restarts. For development, `about:debugging#/runtime/this-firefox`
▸ Load Temporary Add-on ▸ `extension-gecko/manifest.json` gives a Reload button and a console
until the next restart. Do not keep it next to the installed copy: both have the same ID.

## Chrome, Edge and Brave

1. Open `chrome://extensions` (`edge://extensions`, `brave://extensions`).
2. Turn on Developer mode.
3. Click Load unpacked and pick `extension-chromium/`, or a folder you unzipped
   `browserplug-chromium.zip` into. Chrome loads folders, not zips.

After a code change, press Reload on the extension's card.

`page_eval` needs "Allow user scripts" on the extension's Details page (Chrome 138 and later),
and only you can turn it on. Without it every other tool works and `page_eval` returns
`EVAL_UNAVAILABLE`. The popup then reads "Connected but scripts are blocked"; click "scripts
are blocked" to reach the switch.

## Verifying a download

A GitHub release lists the two extension packages, the source and server zips, `SHA256SUMS`
and a provenance bundle, `browserplug-<version>.intoto.jsonl`. These commands check a download against both, from the
folder holding the files:

```bash
sha256sum --check SHA256SUMS
gh attestation verify browserplug-<version>.xpi --repo MarcinWolnyEU/browserplug
```

The second command needs the [GitHub CLI](https://cli.github.com/) and confirms that GitHub
Actions built the file from this repository. To check offline against the bundle, add
`--bundle browserplug-<version>.intoto.jsonl`. The XPI on a release is the unsigned build; see
[Firefox](#firefox) for what that means.

## Registering MCP clients

Register `server/launcher.py`, not `server/server.py`. Most clients accept this entry, with the
venv's interpreter and the checkout by absolute path:

```jsonc
{
  "mcpServers": {
    "browserplug": {
      "command": "/abs/path/to/venv/bin/python",
      "args": ["/abs/path/to/browserplug/server/launcher.py"],
      "env": { "BROWSERPLUG_SERVER_PYTHON": "/abs/path/to/venv/bin/python" }
    }
  }
}
```

Clients with another schema take the same command, argument and variable in their stdio
option. [mcp-clients.md](mcp-clients.md) lists where each client keeps its entry.

### Claude Code

```
claude mcp add --scope user browserplug -e BROWSERPLUG_SERVER_PYTHON=/abs/path/to/venv/bin/python -- /abs/path/to/venv/bin/python /abs/path/to/browserplug/server/launcher.py
```

`claude mcp get browserplug` then reports it as connected.

### Codex

From a terminal on the same machine, with your paths:

```powershell
codex mcp add browserplug --env 'BROWSERPLUG_SERVER_PYTHON=C:\Users\YOUR_USERNAME\AppData\Local\browserplug\venv\Scripts\python.exe' -- 'C:\Users\YOUR_USERNAME\AppData\Local\browserplug\venv\Scripts\python.exe' 'C:\path\to\browserplug\server\launcher.py'
codex mcp get browserplug
```

`get` should show `enabled: true`, `transport: stdio`, the interpreter as `command`, the
launcher in `args` and the variable under `env`. Codex passes a stdio server only a short list
of variables, so the `--env` flag is what delivers the interpreter; one set in your shell does
not reach it. Running `add` with the same name replaces an existing entry. Start a new Codex
task to load the tools, or restart the app or CLI if they do not appear, then ask it to call
`server_info` and `tabs`.

On Windows, Codex's sandbox may block a checkout outside its workspace, and reading that
folder or writing the global Codex config can each need an approval. The launcher and the venv
must stay reachable whenever Codex starts the server, so register the real paths and approve
access to them. If your policy forbids that, use a copy inside the permitted workspace, or run
the commands yourself in a normal terminal. A sandboxed command that reports `failed to resolve
CODEX_HOME` or `Could not find home directory` needs the same: approved access to your user
environment, or your own terminal. `python server\preflight.py --json` checks the checkout
before you register; its client-config check may describe another client, so confirm Codex
with `codex mcp get browserplug` and a tool call.

### Direct HTTP

For a client that only speaks HTTP, or when you start the server yourself:

```jsonc
{
  "mcpServers": {
    "browserplug": {
      "type": "http",                      // some clients say "streamable-http" or "remote"
      "url": "http://localhost:2830/mcp"   // no trailing slash
    }
  }
}
```

`/mcp/` answers 307, and some clients (GitLab Duo among them) do not follow it. For Codex:
`codex mcp add browserplug --url http://localhost:2830/mcp`. An HTTP client does not follow
the server to another port; if 2830 is taken on your machine, see
[server.md](server.md#ports).

## Starting the server at login

The first client's launcher starts the server, so this is optional. It lets a browser connect
before any client runs. Run `server/control.py start` at login with `BROWSERPLUG_SERVER_PYTHON`
in its environment; it returns once the server answers, or at once if one is running. Keep
ports out of these scripts ([server.md](server.md#ports) says why).

- Windows: a Scheduled Task, triggered at log on, that runs a windowless wrapper:

  ```vbscript
  ' run-browserplug.vbs
  Set sh = CreateObject("WScript.Shell")
  sh.Environment("PROCESS")("BROWSERPLUG_SERVER_PYTHON") = "C:\Users\YOUR_USERNAME\AppData\Local\browserplug\venv\Scripts\python.exe"
  sh.Run """C:\Users\YOUR_USERNAME\AppData\Local\browserplug\venv\Scripts\python.exe"" ""C:\path\to\browserplug\server\control.py"" start", 0, False
  ```
  ```powershell
  $a = New-ScheduledTaskAction -Execute "wscript.exe" -Argument '"C:\path\to\browserplug\run-browserplug.vbs"'
  $t = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
  Register-ScheduledTask -TaskName "BrowserPlug Server" -Action $a -Trigger $t -Force
  Start-ScheduledTask -TaskName "BrowserPlug Server"
  ```

- macOS: a LaunchAgent in `~/Library/LaunchAgents/` with `RunAtLoad` true, running
  `/abs/path/to/venv/bin/python /abs/path/to/browserplug/server/control.py start`, and
  `BROWSERPLUG_SERVER_PYTHON` under `EnvironmentVariables`.
- Linux: a `systemd --user` unit running the same command, with
  `Environment=BROWSERPLUG_SERVER_PYTHON=/abs/path/to/venv/bin/python`, `Type=oneshot`,
  `RemainAfterExit=yes` (so systemd leaves the started server alone) and
  `WantedBy=default.target`.

## Verifying

- The plug on the extension's toolbar button is seated and green
  ([extension.md](extension.md#the-toolbar-button)). If not, open the popup and press
  Reconnect.
- `server_info` lists the connected browsers and every MCP client; `tabs` lists your open tabs.
- `python server/preflight.py` (add `--json` for JSON) checks the interpreter, the
  dependencies, client configs, the port, the server and the extension. It answers while the
  server is down, and each failure names its fix.

## Troubleshooting

| Symptom | Fix |
|---|---|
| A tool returns `No extension connection available` | No browser with the extension is connected. Open the browser and press Reconnect in the popup. |
| A client cannot reach `http://localhost:2830/mcp` | Check that the server runs (`python server/control.py status`), that the client's port matches `BROWSERPLUG_MCP_PORT`, and that the URL has no trailing slash. |
| Clients time out after about 30 s while `/health` answers on another port | A port set in an autostart script put the server outside 2830 to 2942. Remove it, run `python server/control.py shutdown`, then start the server again. |
| `claude mcp get browserplug` does not report it as connected | Run `python server/preflight.py`; it names the step that failed. |
| Firefox does not keep the extension after a restart | You are on release or beta Firefox. Use Developer Edition with `xpinstall.signatures.required` set to `false`. |
| `page_eval` returns `EVAL_UNAVAILABLE` on Chrome, Edge or Brave | Turn on "Allow user scripts" on the extension's Details page. |
| A `proxy` argument is refused | Proxy routing is off until you allow it ([proxy-and-tor.md](proxy-and-tor.md#turning-it-on)). |
| Tools act on the wrong browser | Several browsers are connected. `browsers(action="list")` shows them and `browsers(action="use", target=<n>)` switches. Tab ids and snapshot refs do not carry over: run `tabs` or `page_snapshot` again. |
| The server exits with `Another healthy BrowserPlug server already holds the instance lock` | A server is already running; use it. `python server/control.py restart` replaces it. |
| The server exits with `Refusing to start a second instance` | A stuck server holds the lock. `python server/preflight.py` names the process and how to end it. |
| The launcher's log shows `uv was not found` or `BROWSERPLUG_SERVER_PYTHON names …, which does not exist` | Set `BROWSERPLUG_SERVER_PYTHON` to the venv's python in the client's `env` block, then restart the client. |
| The server fails after an update, or preflight lists missing dependencies | Run `python server/control.py shutdown`, rerun the `pip install --requirements-from-script` line, then `python server/control.py start`. |
| With uv: `ServerEnvError`, a restart refused with `deps_changed`, or `server_env_damaged` in `/health` | Run `python server/control.py shutdown`, then `python server/control.py start`; the start rebuilds the environment. |
| You need the logs | `browserplug-server.log` in the checkout, or `BROWSERPLUG_LOG_FILE`; a failed start leaves `*.startup.log` files beside it ([server.md](server.md#logs)). |
