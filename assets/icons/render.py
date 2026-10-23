# /// script
# requires-python = ">=3.10"
# # The same specifier as tests/run.py declares.
# dependencies = ["pillow>=13.0.0"]
# ///
"""Render the BrowserPlug icon set from the masters in ./masters, then install the shipped
subset into both extension trees (run: uv run --script assets/icons/render.py [--check]).

masters/large-<state>.png   1024 px tile + window + seated plug      -> app icon, 48 px and up
masters/bold-<state>.png    1024 px tile + heavy window + plug       -> Chromium static `icons` 32
masters/mini-<state>.png    1024 px tile + window edge + plug        -> Chromium static `icons` 16
masters/glyph-<state>.png   1024 px frame + plug, no tile (navy)     -> both toolbars, light theme, and the
                                                                       popup/options logo; the dark-theme
                                                                       twin is derived here (frame -> white,
                                                                       plug lifted)

Rendering needs Pillow. Only ever downscales; nothing here calls an image model.

Installing copies the shipped subset into extension-gecko/icons/ and extension-chromium/icons/,
laid out the way toolbar-icon.js and the two manifests address it (see ICON_README.md for the
directory contract). `--check` compares the trees against this directory and exits nonzero on
drift; it hashes files only, so it runs without Pillow.
"""

import filecmp
import os
import shutil
import sys

try:
    from PIL import Image, ImageDraw
except ImportError:      # --check only hashes files; rendering is what needs Pillow
    Image = ImageDraw = None

HERE = os.path.dirname(os.path.abspath(__file__))
M = os.path.join(HERE, "masters")
STATES = ("connected", "unhealthy", "connecting", "disconnected")
GREEN, AMBER = (67, 160, 71), (240, 173, 78)   # the plug in connected / unhealthy
TOOLBAR = (16, 24, 32, 48, 64)          # 1x, 1.5x, 2x, 3x, 4x of a 16 px slot
APP = (48, 96, 128, 256, 400, 512, 1024)  # store listings, manifest icons, Cline (400)
TILE_PICK = {16: "mini", 32: "bold"}    # Chromium's static manifest `icons` (chrome://extensions)
LOGO = 96                               # popup / options brand mark, shown at 24-28 CSS px (3x)


def load(name):
    return Image.open(os.path.join(M, name + ".png")).convert("RGBA")


def square_crop(im, margin=0.04):
    x0, y0, x1, y1 = im.getchannel("A").getbbox()
    side = max(x1 - x0, y1 - y0)
    pad = int(side * margin)
    side += 2 * pad
    cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
    out = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    out.paste(im, (side // 2 - cx, side // 2 - cy))
    return out


def dark_twin(glyph, lift=0.22):
    """Frame (low saturation) -> white; plug (saturated) lifted toward white."""
    out = glyph.copy()
    px = out.load()
    w, h = out.size
    for y in range(h):
        for x in range(w):
            r, g, b, a = px[x, y]
            if a == 0:
                continue
            if max(r, g, b) - min(r, g, b) < 60:
                px[x, y] = (255, 255, 255, a)
            else:
                px[x, y] = (int(r + (255 - r) * lift), int(g + (255 - g) * lift), int(b + (255 - b) * lift), a)
    return out


def recolour(im, target, source=GREEN):
    """The plug in another colour, by linear un-mixing so anti-aliased edges follow.

    `unhealthy` is connected with an amber plug: same seat, same geometry, so it is
    derived here rather than drawn, and cannot drift from connected."""
    out = im.copy()
    px = out.load()
    w, h = out.size
    navy_sat, full_sat = 24, max(source) - min(source)
    for y in range(h):
        for x in range(w):
            r, g, b, a = px[x, y]
            if a == 0:
                continue
            k = (max(r, g, b) - min(r, g, b) - navy_sat) / (full_sat - navy_sat)
            if k <= 0:
                continue
            k = min(1.0, k)
            px[x, y] = (int(round(r + k * (target[0] - source[0]))),
                        int(round(g + k * (target[1] - source[1]))),
                        int(round(b + k * (target[2] - source[2]))), a)
    return out


def master(variant, state):
    if state == "unhealthy":
        return recolour(load(f"{variant}-connected"), AMBER)
    return load(f"{variant}-{state}")


def save(im, *parts):
    path = os.path.join(HERE, *parts)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    im.save(path, optimize=True)
    return path


def render():
    rendered = {}
    # app icon: connected state only, margin kept (stores want ~12% padding)
    large = load("large-connected")
    for s in APP:
        rendered[("app", "connected", s)] = large.resize((s, s), Image.LANCZOS)
        save(rendered[("app", "connected", s)], "app", f"icon-{s}.png")
    # The logo is the connected glyph, ink matched to the page: navy on light, white on dark.
    glyph = square_crop(load("glyph-connected"), 0.03)
    for theme, src in (("light", glyph), ("dark", dark_twin(glyph))):
        save(src.resize((LOGO, LOGO), Image.LANCZOS), "app", f"logo-{theme}.png")
    # Static tile for the two places a browser draws the extension icon on a surface this
    # code does not control (chrome://extensions, the store): the dark tile survives both.
    for s, variant in TILE_PICK.items():
        src = square_crop(master(variant, "connected"))
        save(src.resize((s, s), Image.LANCZOS), "tile", f"icon-{s}.png")
    for st in STATES:
        light = square_crop(master("glyph", st), 0.03)
        dark = dark_twin(light)
        for s in TOOLBAR:
            for setname, src in (("light", light), ("dark", dark)):
                im = src.resize((s, s), Image.LANCZOS)
                rendered[(setname, st, s)] = im
                save(im, "toolbar", setname, st, f"icon-{s}.png")
    contact_sheet(rendered)


def contact_sheet(rendered):
    """Both toolbar sets, every state, every size, on the toolbar each is meant for; zoomed
    nearest-neighbour so the pixel grid is visible."""
    zoom = {16: 6, 24: 5, 32: 4, 48: 3, 64: 2}
    pad, cellw, rowh = 16, 150, 140
    rows = [(setname, st, bg) for st in STATES for setname, bg in
            (("light", (246, 246, 247, 255)), ("dark", (43, 42, 51, 255)))]
    W, H = pad + cellw * len(TOOLBAR), pad + rowh * len(rows)
    sheet = Image.new("RGBA", (W, H))
    d = ImageDraw.Draw(sheet)
    y = pad // 2
    for setname, st, bg in rows:
        d.rectangle([0, y, W, y + rowh], fill=bg)
        x = pad
        for s in TOOLBAR:
            big = rendered[(setname, st, s)].resize((s * zoom[s], s * zoom[s]), Image.NEAREST)
            sheet.alpha_composite(big, (x, y + (rowh - big.height) // 2))
            x += cellw
        y += rowh
    save(sheet, "preview.png")




# --------------------------------------------------------------------------- install

REPO = os.path.dirname(os.path.dirname(HERE))
MANIFEST_SIZES = (48, 96, 128)  # the manifest "icons" key; the larger APP sizes are listing art

# What each extension tree ships, as {tree-relative destination dir: source dir here}.
# Both get BOTH glyph sets and pick between them at runtime (toolbar-icon.js): Firefox from
# its theme or prefers-color-scheme, Chromium from an offscreen document that can ask
# matchMedia what the service worker cannot. Chromium alone also carries the static tile.
SHIP = {
    "extension-gecko": {
        "icons/toolbar/light": "toolbar/light",
        "icons/toolbar/dark": "toolbar/dark",
    },
    "extension-chromium": {
        "icons/toolbar/light": "toolbar/light",
        "icons/toolbar/dark": "toolbar/dark",
    },
}
# Files that sit directly in a tree's icons/ (no per-state directories), tree -> {dest: src}.
SHIP_FLAT = {
    "extension-chromium": {
        f"icons/tile/icon-{s}.png": f"tile/icon-{s}.png" for s in TILE_PICK
    },
}
LOGOS = ("logo-light.png", "logo-dark.png")   # shared: icons/app/, byte-identical in both trees


def shipped_files():
    """[(source abs path, tree, tree-relative destination)] for every file both trees ship.

    The app icons are byte-identical in the two trees on purpose: parity classifies
    icons/app/*.png as [shared], so sync_common.py keeps them honest.
    """
    out = []
    for tree, dirmap in SHIP.items():
        for size in MANIFEST_SIZES:
            out.append((os.path.join(HERE, "app", f"icon-{size}.png"),
                        tree, f"icons/app/icon-{size}.png"))
        for name in LOGOS:
            out.append((os.path.join(HERE, "app", name), tree, f"icons/app/{name}"))
        for dest, src in SHIP_FLAT.get(tree, {}).items():
            out.append((os.path.join(HERE, src), tree, dest))
        for dest_dir, src_dir in dirmap.items():
            for state in STATES:
                for size in TOOLBAR:
                    out.append((os.path.join(HERE, src_dir, state, f"icon-{size}.png"),
                                tree, f"{dest_dir}/{state}/icon-{size}.png"))
    return sorted(out, key=lambda row: (row[1], row[2]))


def install():
    """Replace each tree's icons/ with what shipped_files() says belongs there.

    The destination is rebuilt rather than merged so a renamed state or size cannot leave
    an orphan behind — an orphan would ship in the XPI and fail parity's file classification
    long after the rename is forgotten.
    """
    for tree in SHIP:
        dest_root = os.path.join(REPO, tree, "icons")
        if os.path.isdir(dest_root):
            shutil.rmtree(dest_root)
    for src, tree, rel in shipped_files():
        dest = os.path.join(REPO, tree, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy2(src, dest)
    print(f"Installed {len(shipped_files())} icon files into "
          + " and ".join(f"{t}/icons/" for t in SHIP))
    return 0


def check():
    """Report drift between this directory and what the two trees carry. Exits nonzero."""
    problems = []
    expected = {}
    for src, tree, rel in shipped_files():
        expected.setdefault(tree, set()).add(rel)
        dest = os.path.join(REPO, tree, rel.replace("/", os.sep))
        if not os.path.isfile(src):
            problems.append(f"{rel}: source missing ({os.path.relpath(src, REPO)}) — re-render")
        elif not os.path.isfile(dest):
            problems.append(f"{tree}/{rel}: missing — run `uv run --script assets/icons/render.py`")
        elif not filecmp.cmp(src, dest, shallow=False):
            problems.append(f"{tree}/{rel}: differs from {os.path.relpath(src, REPO)}")
    for tree in SHIP:  # orphans: shipped by a tree but no longer produced here
        root = os.path.join(REPO, tree, "icons")
        for dirpath, _dirs, names in os.walk(root):
            for name in names:
                rel = os.path.relpath(os.path.join(dirpath, name), os.path.join(REPO, tree))
                rel = rel.replace(os.sep, "/")
                if rel not in expected[tree]:
                    problems.append(f"{tree}/{rel}: orphan — not produced by render.py")
    if problems:
        print(f"DRIFT: {len(problems)} icon file(s) out of sync:")
        for line in problems:
            print(f"  {line}")
        return 1
    print(f"OK: all {len(shipped_files())} shipped icon files match assets/icons/.")
    return 0


def main(argv):
    if argv and argv[0] in ("--check", "-c"):
        return check()
    if argv:
        print(f"unknown argument: {argv[0]!r} (use --check, or no args to render + install)")
        return 2
    if Image is None:
        print("Pillow is required to render: run `uv run --script assets/icons/render.py`, "
              "which installs it, or use --check.", file=sys.stderr)
        return 2
    render()
    return install()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
