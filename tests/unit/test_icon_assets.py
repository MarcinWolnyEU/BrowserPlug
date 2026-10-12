"""Every icon path the extensions name must resolve to a real PNG of the right size.

A wrong icon path is not an exception anywhere. `browser_action.default_icon` with a bad
path leaves the toolbar button blank; `setIcon()` fails silently on gecko and rejects into
a warning on chromium; the manifest `icons` key just falls back. Every one of those looks
like "the connection state never changed", which is the symptom the icon exists to rule out.
So the paths are checked here, against the files on disk, in the fast suite.

Three things have to agree, and none of them can see the other two at runtime:

  assets/icons/render.py   renders the art and installs the shipped subset
  toolbar-icon.js          builds "icons/toolbar/[<theme>/]<state>/icon-<size>.png"
  the two manifests        name a static subset of the same tree

Both trees ship the same two glyph sets (light and dark ink) and pick between them at
runtime: Firefox from its theme, Chromium from an offscreen document's matchMedia. Chromium
alone also carries a static tile for its manifest `icons` key. The popup and options logos
name two more files, which live in icons/app/.
"""

import json
import re
import struct
from pathlib import Path

import pytest

import test_imports  # noqa: F401

REPO_ROOT = Path(__file__).resolve().parents[2]
TREES = {"gecko": REPO_ROOT / "extension-gecko", "chromium": REPO_ROOT / "extension-chromium"}
THEME_SEGMENTS = ("light", "dark")


def png_size(path: Path) -> tuple[int, int]:
    """(width, height) from the IHDR chunk. Stdlib only -- no image library to declare."""
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", f"{path} is not a PNG"
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def toolbar_icon_source() -> str:
    return (TREES["gecko"] / "toolbar-icon.js").read_text(encoding="utf-8")


def js_list(name: str) -> list[str]:
    """The SIZES / STATES arrays out of toolbar-icon.js, so this test cannot drift from it."""
    src = toolbar_icon_source()
    match = re.search(rf"const {name} = \[([^\]]+)\];", src)
    assert match, f"{name} not found in toolbar-icon.js -- the declaration was reshaped"
    return [item.strip().strip("'\"") for item in match.group(1).split(",")]


SIZES = [int(s) for s in js_list("SIZES")]
STATES = js_list("STATES")


def manifest(tree: str) -> dict:
    return json.loads((TREES[tree] / "manifest.json").read_text(encoding="utf-8"))


def declared_icon_paths(tree: str) -> dict[str, str]:
    """{where it was declared: relpath} for every icon path in a manifest."""
    data = manifest(tree)
    out = {}
    for size, rel in data.get("icons", {}).items():
        out[f"icons[{size}]"] = rel
    action_key = "browser_action" if tree == "gecko" else "action"
    for size, rel in data.get(action_key, {}).get("default_icon", {}).items():
        out[f"{action_key}.default_icon[{size}]"] = rel
    return out


def test_the_parsed_declarations_are_not_vacuous():
    assert SIZES == [16, 24, 32, 48, 64], SIZES
    assert STATES == ["connected", "unhealthy", "connecting", "disconnected"], STATES


@pytest.mark.parametrize("tree", sorted(TREES))
def test_every_path_toolbar_icon_js_can_build_exists(tree):
    """The full cross product the shared path builder can produce, for THIS tree's layout."""
    missing = []
    for segment in THEME_SEGMENTS:
        for state in STATES:
            for size in SIZES:
                rel = f"icons/toolbar/{segment}/{state}/icon-{size}.png"
                if not (TREES[tree] / rel).is_file():
                    missing.append(rel)
    assert not missing, (
        f"extension-{tree}/ is missing {len(missing)} icon(s) toolbar-icon.js can name, "
        f"starting with {missing[0]}. Re-run: uv run --script assets/icons/render.py"
    )


@pytest.mark.parametrize("tree", sorted(TREES))
def test_the_tree_ships_no_theme_segment_it_cannot_use(tree):
    """A directory toolbar-icon.js cannot address is dead weight in the package."""
    shipped = {
        p.relative_to(TREES[tree] / "icons" / "toolbar").parts[0]
        for p in (TREES[tree] / "icons" / "toolbar").rglob("*.png")
    }
    assert shipped == set(THEME_SEGMENTS), (
        f"extension-{tree}/icons/toolbar/ top level is {sorted(shipped)}, expected "
        f"{sorted(THEME_SEGMENTS)}. render.py's SHIP map and toolbar-icon.js disagree about the "
        f"directory layout."
    )


@pytest.mark.parametrize("tree", sorted(TREES))
def test_every_manifest_icon_path_exists(tree):
    declared = declared_icon_paths(tree)
    assert declared, f"extension-{tree}/manifest.json declares no icons at all"
    for where, rel in declared.items():
        assert (TREES[tree] / rel).is_file(), (
            f"extension-{tree}/manifest.json {where} names {rel!r}, which does not exist. "
            f"A browser reports this by showing no icon, not by refusing to install."
        )


@pytest.mark.parametrize("tree", sorted(TREES))
def test_every_manifest_icon_is_the_size_it_is_filed_under(tree):
    """The size key IS the promise; a 48 px file under "16" is what the browser downscales."""
    for where, rel in declared_icon_paths(tree).items():
        declared_size = int(where[where.index("[") + 1:-1])
        assert png_size(TREES[tree] / rel) == (declared_size, declared_size), (
            f"extension-{tree}/manifest.json {where} -> {rel} is "
            f"{png_size(TREES[tree] / rel)}, not {declared_size}x{declared_size}."
        )


@pytest.mark.parametrize("tree", sorted(TREES))
def test_every_shipped_toolbar_icon_is_square_and_the_size_it_names(tree):
    wrong = []
    for path in sorted((TREES[tree] / "icons").rglob("icon-*.png")):
        want = int(path.stem.split("-")[1])
        if png_size(path) != (want, want):
            wrong.append(f"{path.relative_to(TREES[tree])} is {png_size(path)}")
    assert not wrong, "icons whose pixels disagree with their filename:\n  " + "\n  ".join(wrong)


@pytest.mark.parametrize("tree", sorted(TREES))
def test_the_default_icon_matches_the_theme_toolbar_icon_js_starts_on(tree):
    """Both engines paint before the theme can have answered, so the manifest's static
    default must name the same set the module seeds itself with -- otherwise the button is
    blank for however long the theme takes to resolve."""
    seed = re.search(r"let themeDir = '(\w+)';", toolbar_icon_source())
    assert seed, "the themeDir seed in toolbar-icon.js was reshaped"
    checked = 0
    for rel in declared_icon_paths(tree).values():
        if rel.startswith("icons/toolbar/"):
            checked += 1
            assert rel.split("/")[2] == seed.group(1), (
                f"the {tree} manifest's default_icon names {rel!r} but toolbar-icon.js seeds "
                f"itself with the {seed.group(1)!r} set."
            )
    assert checked, f"the {tree} manifest names no toolbar default icon"


@pytest.mark.parametrize("tree", sorted(TREES))
def test_the_popup_and_options_logos_exist(tree):
    """popup.html and options.html name the brand mark by path; a wrong one is a broken-image
    box in the header, not an error. Both themes, at the size render.py promises."""
    pages = {"popup/popup.html": "../", "options.html": ""}
    for page, prefix in pages.items():
        html = (TREES[tree] / page).read_text(encoding="utf-8")
        for theme in THEME_SEGMENTS:
            ref = f"{prefix}icons/app/logo-{theme}.png"
            assert ref in html, f"extension-{tree}/{page} does not name {ref}"
            path = TREES[tree] / "icons" / "app" / f"logo-{theme}.png"
            assert png_size(path) == (96, 96), f"{path.name} is {png_size(path)}"


def test_the_two_trees_share_the_app_icon_art():
    """icons/app/*.png is [shared] in parity.toml -- the manifest icon does not depend on the
    toolbar it is drawn on. This is the assertion that makes that classification true."""
    gecko_app = sorted((TREES["gecko"] / "icons" / "app").glob("*.png"))
    assert gecko_app, "no app icons shipped"
    for path in gecko_app:
        twin = TREES["chromium"] / "icons" / "app" / path.name
        assert twin.is_file(), f"{path.name} is missing from extension-chromium/icons/app/"
        assert path.read_bytes() == twin.read_bytes(), f"{path.name} differs between trees"


def test_render_py_agrees_with_what_the_trees_ship():
    """The renderer's own --check: nothing edited by hand, nothing orphaned by a rename."""
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location(
        "browserplug_icon_render", REPO_ROOT / "assets" / "icons" / "render.py")
    module = importlib.util.module_from_spec(spec)
    was_writing = sys.dont_write_bytecode
    sys.dont_write_bytecode = True  # keep a __pycache__ out of the asset directory
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = was_writing
    assert module.check() == 0, (
        "assets/icons/ and the extension trees disagree (details printed above). Re-run: "
        "uv run --script assets/icons/render.py"
    )
