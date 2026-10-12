# The extension: popup, options and toolbar button

## The toolbar button

The plug's position says whether the extension's connection to the server is open, and its
colour says whether the server answers:

| Plug | Meaning |
|---|---|
| seated, green | connected, and the server answered its last health check |
| seated, amber | connected, but the server is not answering health checks |
| pulled back, amber | connecting, reconnecting, or connected with the first health check running |
| pulled out, red | disconnected, stopped, or given up after too many attempts |

Hover for the detail, such as the server address or the retry countdown. The icon follows the
browser's light or dark theme (a custom Chrome theme is not detected). Right-click the button
and choose Options to open the options page. The popup and the options page are in English;
that right-click entry follows the browser's language, in 30 locales.

A left click opens the interface chosen under Options ▸ General ▸ Extension interface: Popup,
Detached Popup (a window that stays open), Sidebar (the side panel on Chromium) or Separate
tab.

## The popup

Status shows the connection line with a Stop, Reconnect or Re-authorize button, then the
Connections list: the server, each browser and each MCP client with its last activity (hover
a row for its working directory). Click a client for its sessions and recent tool calls.
Right-click a row, or press and hold it for three seconds, to copy its connection info or PID.

The line mirrors the toolbar tooltip, with two additions. `Connected but scripts are blocked`
(Chrome, Edge, Brave) means "Allow user scripts" is off, so `page_eval` fails; click "scripts
are blocked" to reach the switch. `Token rejected` means the server refuses the token this
browser had been using; Re-authorize fetches a new one when it can.

Permissions has a card for each capability you grant yourself: reading `file://` tabs, SOCKS
proxy routing ([proxy-and-tor.md](proxy-and-tor.md)) and, on Chrome, Edge and Brave, "Allow
user scripts". Usage shows the provider panels below and disappears when all are Disabled.

## Options

### General ▸ Server connection

Hostname, WebSocket Port (8772) and Server HTTP Port (2830) say where the server is. On the
server's machine the defaults are right and the extension fetches its token itself.

The Connection row's two dots differ on purpose: the WebSocket dot shows the saved connection,
the HTTP dot checks the hostname as typed. An unsaved new address shows "Reachable" next to
"Disconnected"; save, then press Reconnect.

Copy next to Connection token makes a device token for a browser on another machine, and works
only on the server's machine ([browsers.md](browsers.md#browsers-on-other-machines) covers
tokens).

The Local, LAN and WAN panels move the server's WebSocket off `localhost` so other machines
can connect; "Set & Restart" applies the choice. `BROWSERPLUG_WS_HOST` overrides and locks
them.

- LAN lists the server machine's addresses.
- WAN probes `http://<your address>:8772/reachable` through check-host.net when you show the
  panel, and reads your public address from 4.ident.me unless you type one. If the port is
  unreachable it diagnoses why (listener, Windows Firewall profile, gateway) and offers a
  firewall fix command.
- Test connection (LAN and WAN), run from the remote browser, opens and closes a WebSocket to
  the hostname and port on screen. It shows that the server runs and the network lets the
  connection through, not that the token is right.

### General ▸ Default browser

See [browsers.md](browsers.md#the-default-browser).

### General ▸ Captured bodies

`requests(action="get")` returns bodies as sent, so a login form's password reaches the agent.
"Censor secrets in captured bodies" (off by default) replaces the values of credential-looking
keys (`password`, `token`, `api_key`, `cookie` and similar) with `<redacted>` in JSON, form and
multipart bodies, and in `key: value` or `key=value` text.

With the switch on, "Custom patterns" takes up to 20 regular expressions of up to 200
characters, one per line; each match becomes `<redacted>`. They are case-sensitive (use
`(?i:…)`), and a pattern that could run away on a large body, such as `(a+)+`, is ignored.
Type the shape of a secret (`sk-[A-Za-z0-9]{20,}`), never a secret: the switch and patterns
sync to your other devices. The Test box previews a pasted sample, which stays on this device.

The setting also covers traffic already captured. Credential headers (`Authorization`,
`Cookie`, `Set-Cookie`, API-key, session and CSRF headers) are always `<redacted>`.

### Advanced

"Advanced connection settings" holds Retry Interval (5000 ms), Max Retry Attempts (-1, no
limit) and Ping Timeout (5000 ms). "Reset all settings to Defaults" asks first, then clears
the extension's settings, including a typed Connection token, and deletes the server's
`config.json` (network mode, default browser). A server bound beyond `localhost` restarts onto
the default bind. The server's master token stays. The provider cards are also here.

### Health

The connection map shows each tool call's path from MCP client to browser, with per-hop
timings. "Server health" has the health report and a "Restart server…" button.

## AI provider usage panels

The popup can show how much of a provider's allowance is left, next to the client that spends
it, using the profile's logged-in session. Each provider's card under Options ▸ Advanced
offers "Disabled", "Visible only in Usage tab" and "Visible in Usage tab and below client".
Disabled stops the requests too.

| Provider | Shows | Default |
|---|---|---|
| Anthropic | Claude session and weekly usage, on Claude Code rows | below client |
| OpenAI | ChatGPT/Codex 5-hour and weekly windows, on Codex CLI rows | disabled |
| GitHub Copilot | monthly quotas, on GitHub Copilot Chat rows | below client |
| DeepSeek | a peak and off-peak price clock, on DSH rows (no request) | disabled |

Signed out of a provider's site, its panel shows nothing (Anthropic keeps its peak-hours
clock). If the browser's GitHub account differs from VS Code's, the Copilot row says "other
account". A reading is cached for the card's refresh rate (5 minutes). "Query every N min to
update the usage progress bar" (off by default) refreshes it with nothing on screen, at one
request per interval even when signed out.

Kilo CLI, pi and GitHub Copilot Chat rows also show a badge with the session's provider and
model, read by the server from those clients' log files. Clear "Load Kilo session details from
its logs" (Advanced ▸ Kilo sessions) to stop that. [PRIVACY.md](../PRIVACY.md) lists every host
the extension contacts.
