#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Build the source archive AMO asks for alongside a Firefox submission.

AMO requires sources whenever a submitted add-on contains code the reviewer cannot
read as shipped. Ours contains two such files: `vendor/defuddle.js`, an upstream bundle
with one small patch applied by `patch_defuddle.py`, and `vendor/mlkem.js`, an esbuild
bundle of `@noble/post-quantum` built from `vendor-src/mlkem/`. The "sources" are the
extension tree plus the builder that packs it, the patch script, the bundle's build
inputs, and a README telling the reviewer how to reproduce the bundles and the XPI.

  extension-gecko/ + build_xpi.py + patch_defuddle.py + vendor-src/mlkem/ + LICENSE
      ->  dist/firefox/browserplug-source-<version>.zip

The file list comes from the same walk `build_xpi.py` does, so the archive cannot
describe a tree that is not the tree.

Usage:  python build_source_zip.py [--out PATH]
"""
import argparse
import hashlib
import json
import os
import sys
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
GECKO_DIR = os.path.join(ROOT, "extension-gecko")
FIREFOX_OUT_DIR = os.path.join(ROOT, "dist", "firefox")

# Files outside the extension tree that a reviewer needs to reproduce the XPI.
ROOT_FILES = ("build_xpi.py", "patch_defuddle.py", "LICENSE", "NOTICE", "THIRD-PARTY-NOTICES",
              "vendor-src/mlkem/entry.js", "vendor-src/mlkem/package.json",
              "vendor-src/mlkem/package-lock.json", "vendor-src/mlkem/README.md")

# The one vendored bundle, pinned by its package version and TWO hashes: the upstream
# file as npm ships it, and the file we ship, which is that file after
# patch_defuddle.py (three innerHTML assignments rewritten to DOMParser; the script's
# docstring says why). A reviewer reproduces the shipped hash from the upstream one
# with the three commands in the README below.
#
# Identify the bundle by hash, not by its version string. Its one `version` constant
# reads "0.13.3", the version of the Temml math renderer bundled inside defuddle, so a
# reviewer comparing it against npm's defuddle version would find a mismatch. Move all
# three fields together when you upgrade defuddle, and note that _check_defuddle below
# refuses to build if the shipped hash disagrees with the file on disk, because the
# README's claim is what AMO review actually checks.
DEFUDDLE = {
    "path": "vendor/defuddle.js",
    "version": "0.19.4",
    "upstream_sha256": "af3405fbab6971f85ad9912f7b4dba4d0889900efe5d93638df7d3f23278ebc1",
    "sha256": "ea90c7c25dff64abc481303aa20f29277079c3515ecfede101574a52d3e9dc01",
}

README_TEMPLATE = """# BrowserPlug {version} — source for AMO review

The submitted XPI is the plain contents of `extension-gecko/` plus `LICENSE`, `NOTICE`
and `THIRD-PARTY-NOTICES` from this archive's root, zipped with `build_xpi.py` (stdlib
zipfile, manifest.json at the archive root). No
transpiler, bundler, minifier or obfuscator runs over any file we wrote.

## Reproducing the submitted package

    python build_xpi.py     # writes dist/firefox/browserplug@marcinwolny.net.xpi

Requires Python 3.10 or later and nothing else. This archive holds the Firefox tree only,
so the script prints that it skipped the Chromium package and exits 0.

## The one minified file: vendor/defuddle.js

`extension-gecko/vendor/defuddle.js` is the npm package `defuddle`, version
{defuddle_version}, file `dist/index.full.js`, with one patch applied by
`patch_defuddle.py` (included): three `innerHTML` assignments in the helpers
`decodeHTMLEntities` and `parseHTML` are rewritten to `DOMParser`. Nothing else in the
bundle is changed, and we do not build or minify it.

    upstream sha256  {defuddle_upstream_sha256}
    shipped  sha256  {defuddle_sha256}

Reproduce the shipped file with:

    npm pack defuddle@{defuddle_version}
    tar -xzf defuddle-{defuddle_version}.tgz package/dist/index.full.js
    sha256sum package/dist/index.full.js          # the upstream hash above
    python patch_defuddle.py package/dist/index.full.js --out defuddle.js
    sha256sum defuddle.js                         # the shipped hash above

The patch script prints both hashes, and refuses to run if any of its anchors does
not match exactly once, so an upstream file that moved the patched code is rejected
rather than half-patched.

Upstream: https://github.com/kepano/defuddle - it extracts the readable body of a
page, used only for the `content.read` markdown path.

## The bundled file: vendor/mlkem.js

`extension-gecko/vendor/mlkem.js` is ML-KEM-768 from the npm package
`@noble/post-quantum` 0.7.1, bundled by esbuild into one readable (not minified) script
that sets `globalThis.browserplugMlKem768`. `ws-crypto.js` uses it to encrypt the
extension's WebSocket where the browser has no native ML-KEM. Rebuild it from
`vendor-src/mlkem/` (included; the lock file pins every package):

    cd vendor-src/mlkem
    npm ci
    npx esbuild entry.js --bundle --format=iife --target=es2020 \
        --outfile=mlkem.js --legal-comments=eof
    sha256sum mlkem.js                            # equals vendor/mlkem.js

Note: the bundle's one internal `version` string reads "0.13.3". It belongs to the
Temml math renderer that defuddle bundles, not to defuddle itself. The sha256 above
identifies the build.
"""


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _check_defuddle():
    """Refuse to ship a README whose defuddle claim the tree does not support.

    Returns the hash on success. A mismatch means someone upgraded or re-patched the
    bundle without updating DEFUDDLE above, so the archive would tell a reviewer to
    `npm pack` and patch a version whose hash does not match the file beside it — the
    one thing in this archive a reviewer is guaranteed to check by hand.
    """
    path = os.path.join(GECKO_DIR, *DEFUDDLE["path"].split("/"))
    if not os.path.exists(path):
        print(f"ERROR: {DEFUDDLE['path']} is missing from extension-gecko/", file=sys.stderr)
        return None
    actual = _sha256(path)
    if actual != DEFUDDLE["sha256"]:
        print(f"ERROR: {DEFUDDLE['path']} does not match the pin in build_source_zip.py.\n"
              f"  recorded (v{DEFUDDLE['version']}): {DEFUDDLE['sha256']}\n"
              f"  on disk:                    {actual}\n"
              "Update DEFUDDLE's version AND sha256 together, then rebuild.",
              file=sys.stderr)
        return None
    return actual


def collect_files():
    """(abs_path, arcname) pairs, ordered, mirroring build_xpi.py's own walk."""
    files = []
    for name in ROOT_FILES:
        files.append((os.path.join(ROOT, name), name))
    for dirpath, _dirs, filenames in os.walk(GECKO_DIR):
        for name in filenames:
            if name.endswith(".log"):
                continue  # same skip as build_xpi.py
            abs_path = os.path.join(dirpath, name)
            rel = os.path.relpath(abs_path, GECKO_DIR).replace(os.sep, "/")
            files.append((abs_path, f"extension-gecko/{rel}"))
    files.sort(key=lambda pair: pair[1])
    return files


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", help="output path (default: "
                                      "dist/firefox/browserplug-source-<version>.zip)")
    args = parser.parse_args()

    with open(os.path.join(GECKO_DIR, "manifest.json"), encoding="utf-8") as fh:
        version = json.load(fh)["version"]

    defuddle_sha = _check_defuddle()
    if defuddle_sha is None:
        return 1

    missing = [n for n in ROOT_FILES if not os.path.exists(os.path.join(ROOT, n))]
    if missing:
        print(f"ERROR: missing from the repo root: {', '.join(missing)}", file=sys.stderr)
        return 1

    out_path = args.out or os.path.join(FIREFOX_OUT_DIR,
                                        f"browserplug-source-{version}.zip")
    readme = README_TEMPLATE.format(version=version,
                                    defuddle_version=DEFUDDLE["version"],
                                    defuddle_upstream_sha256=DEFUDDLE["upstream_sha256"],
                                    defuddle_sha256=defuddle_sha)

    files = collect_files()
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    if os.path.exists(out_path):
        os.remove(out_path)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        # Generated, not read off disk: it carries the version and hash, so keeping a
        # copy in the tree would be one more thing to forget to update.
        zf.writestr("README-SOURCE.md", readme)
        for abs_path, arcname in files:
            zf.write(abs_path, arcname)

    size = os.path.getsize(out_path)
    print(f"BrowserPlug source archive: version={version}, defuddle={DEFUDDLE['version']}")
    print(f"Packed {len(files) + 1} files -> {out_path} ({size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
