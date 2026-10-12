# BrowserPlug privacy policy

Last updated: 2026-10-05. Applies to the BrowserPlug browser extension (Firefox, Chrome, Edge,
Brave) and the BrowserPlug server that runs on your own machine.

## Summary

- BrowserPlug has no account and no service of its own. The author runs no server that
  receives your data and cannot see it.
- What the extension reads goes to the BrowserPlug server on your computer, which passes it to
  the MCP client you connected (your coding agent, editor or CLI). That client's privacy
  policy governs what happens next.
- The extension makes a few requests of its own, listed below. Three use your logged-in
  session (`claude.ai`, `chatgpt.com`, `github.com`) and each has an off switch. The
  `chatgpt.com` one is off until you turn it on.
- The server downloads and runs the Tor client, but only when a tool call asks for `proxy="tor"`.

## What the extension can reach

BrowserPlug lets an AI tool drive the browser you are already signed in to, so its reach is
wide. On a tool call from your MCP client, the extension can:

- read the text, HTML and article content of any tab, and list tabs with URLs and titles;
- take screenshots, navigate, click, type into forms and select options;
- record a page's network requests (method, URL, status, timing, headers) and, when a capture
  is armed, response bodies;
- collect `console` output and uncaught errors (a content script runs in every page for this);
- run JavaScript your agent supplies (Chrome, Edge and Brave also need the per-extension
  "Allow user scripts" toggle, which only you can turn on).

Any page open in your browser can be read, signed in or not. `Authorization`, `Cookie`,
`Set-Cookie` and the common API-key and CSRF headers are replaced with `<redacted>` in
captured requests. Bodies are reported as sent unless you turn on Options ▸ General ▸ Captured
bodies ▸ Censor secrets in captured bodies. Nothing else is filtered.

## Where it goes

To the BrowserPlug server over a WebSocket, `localhost:8772` by default, and from there to
the MCP client that asked. The socket is encrypted with a device token that belongs to that
browser. The handshake proves the token without sending it. Remote debug logging is compiled
off.

If you point the extension at another host, or bind the server to a LAN or internet address
(to drive a phone, say), page data travels to that host over the encrypted socket. That bind is
off by default, and Options says when it is on. The tool surface (`/mcp`) stays loopback-only
either way, so a leaked token exposes the server's status, not your pages.

The server keeps these records locally:

- `browserplug-server.log` (beside the server, or at `BROWSERPLUG_LOG_FILE`): wire action
  names, request ids, connection events, versions and error locations, never tool arguments
  or page content. It rotates at 2 MB and keeps two older files.
- `/health` and the popup: each connected MCP client's name, working directory and command line
  (values after secret-looking flags masked), and recent tool calls reduced to shape: names,
  lengths, and the host and path of URLs.

To name MCP clients the server inspects local processes. For Kilo, pi and Copilot Chat badges
it also reads their local logs and session stores for the provider, model and account. None of
this leaves your machine.

## Connections the extension makes on its own

None goes to the author.

| Host | Purpose | When | Cookies sent |
|---|---|---|---|
| `claude.ai` | Claude usage percentages for the Claude Code row (an organization lookup, then usage) | When the popup or Options asks and the cache is older than the refresh interval (5 minutes by default); in the background only if you tick "Query every N min". On by default | Yes |
| `chatgpt.com` | ChatGPT and Codex limit windows (a short-lived token, then usage) | As for `claude.ai`, after you enable the panel | Yes |
| `github.com` | Copilot quotas and plan, plus the GitHub login they belong to (two requests), to flag a mismatch with VS Code's account | As for `claude.ai`. On by default | Yes |
| `models.dev` | Readable model and provider names for Kilo, pi and Copilot Chat badges; a public catalog, nothing about you is sent | When a badge needs a name the cache lacks | No |
| `4.ident.me`, `4.tnedi.me` | Your public IPv4 address, for the Options WAN panel | Each time Options opens while a server answers on this machine and no public address is saved | No |
| `check-host.net` | Whether `http://<your public address>:<WS port>/reachable` answers from the internet | Whenever the WAN panel is showing and an address is known, and on its Refresh button. A custom check URL in that panel replaces this host | No |

The account requests read usage numbers, plan tier, reset times and, for GitHub, your login,
never conversations. To stop one, set its card to Disabled under Options ▸ Advanced (Anthropic,
OpenAI or GitHub Copilot sessions). Disabled stops the request, not just the display, and so
does signing out of the site in this profile. For `models.dev`, untick Kilo sessions ▸ "Clarify
model & provider names". The WAN requests have no switch: saving your own public address skips
`ident.me`, and not opening the WAN panel skips `check-host.net`. The DeepSeek panel is a local
clock and fetches nothing.

## Connections the server makes

- Tor. The first tool call with `proxy="tor"` makes the server download the Tor Expert Bundle
  (19 to 32 MB) from `archive.torproject.org`, check it against a SHA-256 pinned in the code,
  unpack it into `server/tor/runtime/` and run it as a loopback SOCKS client. Tor then talks to
  the Tor network, and the tab's traffic goes through it. Nothing downloads at startup, and
  `BROWSERPLUG_TOR_BIN` points at a `tor` you installed yourself instead. A `proxy` argument
  can also name any other SOCKS address.
- Installing the server's Python dependencies contacts PyPI, and `uv` may fetch a Python
  interpreter. That is your package manager's traffic.

## What is stored, and where

In your browser profile:

- `storage.sync`, which Firefox Sync or Chrome Sync copies to your other devices through your
  browser vendor: server host and ports, a connection token if you typed one in, retry and
  timeout values, interface and provider-panel options, the censoring switch and patterns, and
  the WAN panel's public address and check URL. No page content.
- `storage.local`, this device only: the instance id, the device token fetched from your local
  server, cached usage numbers, usage history per account (keyed by claude.ai organization id,
  ChatGPT account id and GitHub login, up to six accounts per provider), cached model names,
  proxy routing state, and any sample you paste into the censoring Test box.
- Memory, until the tab or browser closes: per-tab rings of recent requests, console lines and
  navigations. On Chrome, Edge and Brave a service worker restart clears them.

The server keeps its settings, master token and Python environment in
`%LOCALAPPDATA%\browserplug\` (Linux and macOS: `$XDG_STATE_HOME/browserplug`, else
`~/.local/state/browserplug`), plus the log above.

## Permissions

`tabs` (list, open, close, activate; read URL and title) · `storage` · `alarms` (re-dialling
the server) · `menus` or `contextMenus` (entries that open the extension's own pages) ·
`webRequest` and `webNavigation` (the request log; landed versus failed navigations) ·
`webRequestBlocking` (Firefox: reading a response body during a capture you armed) ·
`clipboardWrite` (the Copy buttons) · `scripting` and `userScripts` (Chrome, Edge, Brave: reads,
clicks and fills) · `sidePanel` (Chrome, Edge, Brave) · `offscreen` (Chrome, Edge, Brave:
detecting a light or dark theme for the toolbar icon) · `proxy` · all sites (your agent picks
the page at call time) · `127.0.0.1` and `localhost` (your own server). Firefox declares the
data categories browsing activity, website activity and website content.

Two capabilities stay off until you enable them. Proxy routing: on Firefox `proxy` is optional
and routes one tab. Chrome, Edge and Brave cannot make it optional, so the extension holds it
but routes nothing until you press "Allow the browser-wide proxy" in the popup, which then
proxies the whole browser. Reading `file://` pages needs a per-extension switch on Chrome,
Edge, Brave and Firefox 153 and later; earlier Firefox allows it through the all-sites
permission.

## Children

BrowserPlug is a developer tool, not directed at children, and collects nothing from anyone. An
AI agent given a browser can act online in ways nobody intended, so switch BrowserPlug off
before a child uses your computer.

## Changes

Material changes update the date at the top and appear in the release notes.

## Contact

Open an issue at <https://github.com/MarcinWolnyEU/browserplug/issues>.
