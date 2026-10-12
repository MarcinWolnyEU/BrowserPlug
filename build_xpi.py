#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Build the BrowserPlug browser extensions into loadable packages.

Stdlib-only replacement for the Makefile's `make package` step, which needs
`make` and `zip`; neither is present on Windows. Builds both engine trees:

  extension-gecko/     -> dist/firefox/<gecko-id>.xpi     (Firefox MV2; name from manifest)
  extension-chromium/  -> dist/chrome/browserplug-chromium.zip  (Chrome MV3)

One directory per browser, so "which file do I install" is answered by the path
rather than by knowing which extension is which. Each package zips the *contents*
of its tree (manifest.json at the archive root, forward-slash paths, deterministic
ordering) so the browser accepts it.

The Chromium package is skipped, with a printed line, when `extension-chromium/` is absent
(the AMO source archive carries only the Firefox tree); the exit status stays 0 then.

Usage:  python build_xpi.py
"""
import json
import os
import sys
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
FIREFOX_OUT_DIR = os.path.join(ROOT, "dist", "firefox")
CHROME_OUT_DIR = os.path.join(ROOT, "dist", "chrome")

# The MIT License makes every redistributed copy carry LICENSE, and the vendored MIT
# bundles need their notices shipped too. NOTICE rides along as the attribution line.
# They live at the repo root, so they are added to each package's root here instead of
# being copied into both trees.
LEGAL_FILES = ("LICENSE", "NOTICE", "THIRD-PARTY-NOTICES")


def pack(ext_dir, out_path, label):
    """Zip the contents of `ext_dir` into `out_path` (deterministic, manifest at root).

    Returns 0 on success, 1 if manifest.json is not at the archive root (which every
    browser rejects). Prints the file list.
    """
    manifest_path = os.path.join(ext_dir, "manifest.json")
    with open(manifest_path, encoding="utf-8") as fh:
        manifest = json.load(fh)
    version = manifest["version"]

    files = []
    for dirpath, _dirs, filenames in os.walk(ext_dir):
        for name in filenames:
            if name.endswith(".log"):
                continue  # a stray server log must never ship (local paths, client names)
            abs_path = os.path.join(dirpath, name)
            # Archive path relative to the tree root, always forward slashes.
            arcname = os.path.relpath(abs_path, ext_dir).replace(os.sep, "/")
            files.append((abs_path, arcname))
    for name in LEGAL_FILES:
        files.append((os.path.join(ROOT, name), name))

    files.sort(key=lambda pair: pair[1])  # deterministic ordering
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    if os.path.exists(out_path):
        os.remove(out_path)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for abs_path, arcname in files:
            zf.write(abs_path, arcname)

    size = os.path.getsize(out_path)
    print(f"BrowserPlug {label}: version={version}")
    print(f"Packed {len(files)} files -> {out_path} ({size} bytes)")
    for _abs, arc in files:
        print(f"  {arc}")
    # Guard: manifest must be at the archive root or the browser rejects the package.
    if not any(arc == "manifest.json" for _abs, arc in files):
        print(f"ERROR: manifest.json not at archive root in {out_path}", file=sys.stderr)
        return 1
    return 0


def main():
    # --- Firefox MV2 (gecko): file name derived from the gecko id -----------------------
    gecko_dir = os.path.join(ROOT, "extension-gecko")
    with open(os.path.join(gecko_dir, "manifest.json"), encoding="utf-8") as fh:
        gecko_manifest = json.load(fh)
    ext_id = gecko_manifest["browser_specific_settings"]["gecko"]["id"]
    gecko_out = os.path.join(FIREFOX_OUT_DIR, f"{ext_id}.xpi")
    rc = pack(gecko_dir, gecko_out, f"gecko extension (id={ext_id})")

    # --- Chrome MV3 (chromium): fixed zip name (MV3 has no gecko application id) --------
    print()
    chromium_dir = os.path.join(ROOT, "extension-chromium")
    if not os.path.isdir(chromium_dir):
        print("extension-chromium/ is not here (the AMO source archive carries only the "
              "Firefox tree): skipping the Chromium package")
        return rc
    # The chromium test harness bakes testMode into a per-session COPY of the tree; the
    # source config.js is what ships, and it must stay the release value or the Web Store
    # zip would answer test.* actions. Refuse rather than package it.
    with open(os.path.join(chromium_dir, "config.js"), encoding="utf-8") as fh:
        if "const BUILD_CONFIG = null;" not in fh.read():
            print("ERROR: extension-chromium/config.js is not the release value "
                  "(BUILD_CONFIG = null); refusing to package a test bake", file=sys.stderr)
            return 1
    chromium_out = os.path.join(CHROME_OUT_DIR, "browserplug-chromium.zip")
    rc |= pack(chromium_dir, chromium_out, "chromium extension")

    return rc


if __name__ == "__main__":
    sys.exit(main())
