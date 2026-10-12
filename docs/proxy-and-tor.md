# Proxy routing and Tor

`tabs(action="open")` and `navigate` accept a `proxy` argument that sends a tab's traffic
through a SOCKS proxy, usually to reach `.onion` sites. `proxy="tor"` makes BrowserPlug
fetch and run Tor by itself.

⚠️ This is not a privacy tool. The routed tab belongs to your normal profile, with its cookies,
logins and fingerprint, and **the Tor Project advises against using Tor with any browser other**
**than [Tor Browser](https://download.torproject.org/).** Do not use it where being identified
would put you or anyone else at risk. ⚠️

## Turning it on

Routing is off until you allow it. Until then agents don't see `proxy`, and a request that
arrives anyway is refused, never loaded directly.

- Firefox: about:addons ▸ BrowserPlug ▸ Permissions ▸ "Control browser proxy settings". On
  Firefox for Android, press "Grant proxy permission" on the popup's Permissions tab.
- Chrome, Edge, Brave: press "Allow the browser-wide proxy" on the popup's Permissions tab.
  The same button turns it off. Read the Chrome section below first.

## Using it

`proxy` takes `socks5://host:port`, `socks5h://`, `socks4://`, `socks4a://` (treated as
`socks4`) or `socks://` (SOCKS5), or `tor`. `""` means direct. HTTP proxies, PAC scripts,
proxies that need a password and the system proxy are not supported. `tor` works on a normal
tab open, not with `new_window` or `incognito`. `tabs(action="routing")` reports whether a
tab uses *any* proxy (`isProxy`), if you need to confirm specifically if you are on TOR network
open https://check.torproject.org/ - it will check the state of TOR connection for you.

### Firefox: one tab

Only the routed tab uses the proxy, and each of its requests goes through it or fails, never
direct. A tab stays routed until a call sends `proxy=""`, an extension reload included.

After a full browser restart, restored tabs are not routed, because Firefox gives them new
ids. A restored `.onion` tab then fails at DNS, which sends the onion name to your normal
DNS server, and an ordinary site loads directly. Close routed tabs before quitting, or
navigate them again with `proxy`.

### Chromium browsers: the whole browser

The proxy setting **is browser-wide**, so `proxy` switches every tab, yours included, until it is
switched off. This is a limitation of Chromium, nothing I can do about it.
Only `localhost`, `127.0.0.1`, `[::1]` and the server's own host bypass it.

- The extension sets the proxy before the page loads. If another extension or a policy
  controls the setting, the call fails with `PROXY_NOT_CONTROLLABLE` and nothing loads.
- The agent is told when it switches the proxy on and is asked to switch it off: `proxy=""`
  on `tabs(action="open")` or `navigate`, or `tabs(action="routing", proxy="")`, which loads
  nothing.
- BrowserPlug also switches it off when the server connection closes (a server restart
  included), when the browser starts, and when you turn off "Allow the browser-wide proxy".
- Only SOCKS5 resolves names at the proxy. With `socks4` the browser resolves them locally.

## `proxy="tor"`

The your first ever call to TOR network will trigger download of the Tor Expert Bundle
from archive.torproject.org (19 to 32 MB), then it will do validation against SHA-256
pinned in BrowserPlug's source (this secures you against someone swapping the file),
unpacks it into `server/tor/runtime/` and starts it on a free local port.

Tor is not one of the server's Python dependencies, and nothing is fetched at install.
The first call takes about a minute, but after that initial setup you are ready to go.
A call the browser would refuse for a missing permission is refused before anything
is downloaded or started.

- Tor exits when the server does. If it dies earlier, the next call starts it on a new port,
  and tabs routed to the old port fail until you navigate them with `proxy="tor"` again.
- The call fails, and nothing loads, if Tor cannot be downloaded, verified or started, or has
  not bootstrapped after 3 minutes.
- To remove Tor, delete `server/tor/runtime/`. `BROWSERPLUG_TOR_HOME` puts it elsewhere,
  which you need when the checkout is not writable.
- `BROWSERPLUG_TOR_BIN` names a `tor` executable you installed. BrowserPlug downloads
  nothing and starts that binary itself; it does not attach to a running Tor service.
- Set both variables in the server's environment
  ([server.md](server.md#getting-a-variable-to-the-server)).

Bundles exist for Windows and Linux on x86_64 and i686 and for macOS on x86_64 and arm64.
I have tested only Windows x86_64 and Linux x86_64 (glibc, Debian).

To test, navigate to https://check.torproject.org/ with a proxy you run, such as
`socks5://127.0.0.1:9050`: the title reads "Congratulations. This browser is configured to
use Tor." Through a port where nothing listens it must fail, never load.
