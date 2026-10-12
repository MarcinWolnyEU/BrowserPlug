# BrowserPlug icons

A browser window with a Schuko plug at its right edge. The plug carries the connection
state, which is the one thing the toolbar button exists to tell you at a glance: where it
sits says whether the socket is open, its colour says whether the server is answering.

| State | Plug | Painted for |
|---|---|---|
| `connected` | seated, green | connected, and the server answered its last health probe |
| `unhealthy` | seated, amber | connected, but the server is not answering health probes. Not drawn: `render.py` recolours the connected master, so the two cannot drift |
| `connecting` | pulled back one pin-length, amber | connecting, reconnecting, or the first probe still in flight |
| `disconnected` | pulled clear, red | disconnected, stopped by the user, or gave up retrying |

`background.js` collapses its six connection phases onto those four states inside
`updateActionIndicator()`, next to the badge colour it already computed, so the badge and
the icon can never disagree. Everything downstream belongs to `toolbar-icon.js`, which is
byte-identical in both extension trees.

## Everything is rendered from four masters

`render.py` only ever downscales; nothing here calls an image model.

| Master | Feeds |
|---|---|
| `masters/large-<state>.png` | the app icon — full window detail, survives 48 px and up |
| `masters/bold-<state>.png` | Chromium tile 32, heavier strokes for a small tile |
| `masters/mini-<state>.png` | Chromium tile 16, the window reduced to its edge, plus the plug; window detail is mud at 16 px |
| `masters/glyph-<state>.png` | the toolbar glyph in both browsers and the popup/options logo, navy, no tile. The white-ink twin is derived by `dark_twin()` rather than drawn, so the two sets cannot drift |

```
uv run --script assets/icons/render.py            # render + install into both trees (the block brings Pillow)
python assets/icons/render.py --check             # report drift; no Pillow needed, any Python 3.10+
```

`preview.png` is the contact sheet the render writes: both sets, every state, every size,
each on the toolbar it is meant for, zoomed nearest-neighbour so the pixel grid is visible.

## Light and dark toolbar sets

A toolbar icon is drawn **on** the toolbar, and a theme can make that surface any colour.
Both extensions therefore ship two glyph sets, navy ink for a light toolbar and white ink for
a dark one, and pick between them at runtime. Until something answers they show the light set.

Firefox asks itself: `theme.getCurrent()` first, and `prefers-color-scheme` when that returns
an object with no `colors`, which is what the default theme does, "System theme: auto"
included. It re-picks on `theme.onUpdated` and on the media query's own `change` event.
Neither read needs the `theme` permission; that gates `theme.update()`/`reset()`, which
nothing here calls.

Chrome's service worker has neither `matchMedia` nor a theme API, so it is told. An offscreen
document (`offscreen.html`, reason `MATCH_MEDIA`, kept alive by `scheme-probe.js`) runs the
media query and messages the answer to `toolbar-icon.js`'s `setTheme()`. That reports the
browser's colour mode. A custom Chrome theme cannot be read by any extension API, so under
one the icon can pick the wrong set.

The layout is the same in both trees, and `render.py`'s `SHIP` map and `toolbar-icon.js`'s
path builder are the two halves of it:

```
extension-{gecko,chromium}/icons/toolbar/{light,dark}/<state>/icon-{16,24,32,48,64}.png
extension-{gecko,chromium}/icons/app/icon-{48,96,128}.png       (byte-identical; parity [shared])
extension-{gecko,chromium}/icons/app/logo-{light,dark}.png      (popup and options brand mark, 96 px)
extension-chromium/icons/tile/icon-{16,32}.png                  (static dark tile, manifest `icons`)
```

The logo is the connected glyph, navy on light and white on dark, chosen by a `<picture>`
source with `prefers-color-scheme: dark`. The tile is the only place the `mini` and `bold`
masters are still used: browsers draw the manifest `icons` key on surfaces this code does not
control, and a tile with its own ground survives all of them.

`tests/unit/test_icon_assets.py` walks the whole cross product against the files on disk.
It has to: a wrong icon path raises nothing anywhere. The button just stays blank, which
looks exactly like a connection state that never moved.

## Sizes, and what each one is for

Shipped in the extension packages:

| Size | Where |
|---|---|
| 16, 24, 32, 48, 64 | `toolbar/` (1×–4× of a 16 px slot). Firefox uses 16/32, Chrome 16/24/32; the rest are HiDPI |
| 48, 96, 128 | `app/`, the manifest `icons` key. Chromium also files the 16 and 32 **tiles** under `icons`, because the tile is drawn for a small slot and the app icon is not |
| 96 | `app/logo-{light,dark}.png`, the popup and options brand mark (shown at 24 to 28 CSS px) |

Not shipped — listing and documentation art, this directory only:

| Size | Where |
|---|---|
| 128, 256, 512 | addons.mozilla.org and Chrome Web Store listings |
| 400 | Cline's MCP marketplace entry |
| 1024 | the render master; anything new crops from here |

Adding a size means adding it to `TOOLBAR` or `APP` in `render.py`. Adding a shipped one
also means adding it to `SIZES` in `toolbar-icon.js` (the unit test reads that array, so it
will tell you) and to the manifests if it belongs in a static key.
