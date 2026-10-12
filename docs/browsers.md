# Browsers

## Supported versions

| | Firefox and other Gecko browsers | Chrome and other Chromium browsers |
|---|---|---|
| Minimum | 140  | 138 |
| Recommended | 153 | 138 |

Firefox for Android needs 142 or later ([firefox-android.md](firefox-android.md)). A browser
below the minimum refuses to install the extension.

## Firefox and Chromium differences

Both extensions expose the same tools. Where an engine cannot do something, the tool fails
with a message that says so.

| | Firefox and other Gecko browsers | Chrome and other Chromium browsers |
|---|---|---|
| `page_eval` | always available | needs "Allow user scripts" on the extension's Details page, which only you can turn on. Without it `page_eval` returns `EVAL_UNAVAILABLE` and everything else works |
| `proxy` routing | one tab | the whole browser ([proxy-and-tor.md](proxy-and-tor.md)) |
| Response bodies in `requests` | every request type | `fetch` and XHR only; documents, images, media and beacons report that no body was captured |
| Screenshots | any tab without switching to it | an inactive tab is switched to and back; full-page captures are stitched; `chrome://newtab` and similar pages cannot be captured |
| Request and console history per tab | kept while the browser runs | emptied when Chromium unloads an idle extension (the tools say so) |
| Reading `file://` tabs | before 153 always allowed; from 153 turn on "Access local files on your computer" in about:addons ▸ BrowserPlug ▸ Permissions | turn on "Allow access to file URLs" on the Details page |

The extension can read a `file://` tab you opened but never open one. The popup's Permissions
tab shows whether file access is on.

## Several browsers at once

Any mix of browsers can connect to one server, including one on another machine such as a
phone ([below](#browsers-on-other-machines)). While two or more are connected, agents get a
`browsers` tool: `browsers(action="list")` shows the roster and `browsers(action="use",
target=<n>)` switches. The choice belongs to one MCP client, so it never moves another
agent's work. Tab ids and `page_snapshot` refs belong to one browser. A switch takes effect
in the order the client's calls reach the server: calls already running finish in the
browser they started in, and calls that arrive after the switch wait for it.

Where a client's calls go:

1. To the browser the client picked, while it is connected.
2. If that browser left and two or more others are connected, calls fail with an error naming
   the browser that left and how to pick another. With one browser left, calls go to it.
3. With no pick, to the only connected browser or, with several, to the default browser.
4. With none connected, calls fail with `No extension connection available`. Connect a
   browser; the popup's Reconnect usually does it.

When calls move to another browser without the client asking, the next result starts with a
notice naming both, and a call that names a tab id, frame or snapshot ref is refused once,
since the same number usually exists in the new browser.

### The default browser

Options ▸ General ▸ Default browser sets where calls go when several browsers are connected
and the client has not picked one. "First started" (the default) is the first browser to
connect that is still connected. Picking a browser pins it: it gets the calls while connected,
and the first-started rule applies while it is not.

For a pinned browser on the server's machine, "Let the server start this browser when none is
connected" starts it when a call arrives and nothing is connected, then waits up to 25 seconds
(`BROWSERPLUG_BROWSER_LAUNCH_WAIT`) for it to connect. The startup arguments are read-only in
Options because they become a command line the server runs; edit `launch_args` under
`default_browser` in the server's `config.json` ([server.md](server.md#where-the-state-lives)).

## Browsers on other machines

A browser on the server's machine sets itself up: it fetches a device token from the server's
`/ws-token` endpoint, which answers only local callers. A browser on another machine, such as
a phone ([firefox-android.md](firefox-android.md)), needs a token handed to it:

1. Let the server accept network connections: pick LAN or WAN under Options ▸ General ▸
   Server connection in a browser on the server's machine
   ([extension.md](extension.md#general--server-connection)), or set `BROWSERPLUG_WS_HOST`
   (`--ws-host`) to `0.0.0.0`.
2. On the server's machine, make a device token with the Copy button next to Connection
   token, or with `python gen_ws_token.py --device`. Give each browser its own.
3. In the remote browser's Options ▸ General ▸ Server connection, set Hostname to the server's
   address and paste the token into Connection token.

Every device token is derived from the master token in the state directory
([server.md](server.md#where-the-state-lives)), which never leaves the server. A token never
crosses the network either: the handshake proves it and keys the encryption (X25519 and
ML-KEM-768). Each browser holds a different token, so one cannot pose as the server to
another. A browser that has not authenticated within 15 seconds is disconnected. With
`BROWSERPLUG_WS_TOKEN` set, browsers on the server's machine must authenticate too; they still
fetch their token themselves.

To revoke every device token, run `python gen_ws_token.py --write` and restart the server.
With `BROWSERPLUG_WS_TOKEN` set, change that value instead.

With the WebSocket bound beyond localhost, the HTTP port listens there too
([server.md](server.md#http-endpoints) says what a remote caller can reach). Anyone who can
reach the port can see that a BrowserPlug server answers there. Over the internet, a tunnel
(SSH, Tailscale) to a server bound to localhost exposes less.
