# Firefox for Android

The Firefox extension runs on Firefox for Android 142 or later. The phone is a remote browser:
the server runs on your computer and the phone connects to it over your network.

## Connecting the phone

1. On the computer, let the server accept browsers from the network. In a desktop browser,
   open Options ▸ General ▸ Server connection and pick LAN, or start the server with
   `--ws-host 0.0.0.0`. These panels are hidden on the phone because they configure the
   machine that runs the server.
2. On the computer, make a device token for the phone with the Copy button next to
   Connection token, or with `python gen_ws_token.py --device`. Each browser needs its own.
3. On the phone, open BrowserPlug's Options, set Hostname to the computer's LAN address and
   paste the token into Connection token.

[browsers.md](browsers.md#browsers-on-other-machines) explains tokens and encryption.

Firefox Sync copies the hostname, port, interface choice and a typed Connection token between
devices, so a syncing phone starts out dialling the desktop's `localhost`, and a change on one
device can overwrite the other's. Set the phone's hostname after Sync has run, then check the
desktop's settings.

## What works differently

- There are no windows. The `tabs` actions that need one (`window`, opening with
  `new_window` or `incognito`, closing by `window_id`) fail with `WINDOWS_UNSUPPORTED`. The
  other tab actions work.
- The interface is always the Overlay, whatever surface you picked on the desktop. The
  "Open Extension Overlay" button in Options opens the same page as a tab.
- `file://` does not work. Firefox for Android cannot open local files, so `tabs` and
  `navigate` refuse a `file://` URL with `FILE_URL_UNSUPPORTED`.
- Proxy routing works per tab ([proxy-and-tor.md](proxy-and-tor.md)). There is no about:addons
  switch, so grant the permission with "Grant proxy permission" on the popup's Permissions
  tab.
- Touch has no right-click or hover. Press and hold a connection row for three seconds to get
  "Copy connection info". The Options ▸ Health map pans by dragging and resets with a
  double-tap. It has no hover details and no wheel zoom.
