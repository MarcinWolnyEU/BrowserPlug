#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Build the BrowserPlug server package: the Python side, as one downloadable zip.

Stdlib-only, like build_xpi.py, so it runs on Windows without `make` or `zip`.

  server/ + launcher/ + shared-py/ + README.md + LICENSE + NOTICE + THIRD-PARTY-NOTICES
      ->  dist/packages/browserplug-server.zip   (everything under one `server-package/`)

What stays out: bytecode (`__pycache__`, `*.pyc`, `*.pyo`), logs (`*.log`, and the rotated
`*.log.N`), the managed Tor client that the server downloads at run time (`server/tor/runtime`),
and anything that looks like local state or a secret (`ws-token`, `config.json`, `.env*`).
`server/server.py.lock` ships: the launcher builds the server's environment from that lock.

The archive is reproducible. Entries are sorted, carry a fixed timestamp and fixed
permissions (executable only when the file starts with a shebang), so two builds from one
tree are byte-identical and the release workflow can attest the file.

Usage:  python build_server_zip.py [--out PATH]
"""
import argparse
import os
import re
import sys
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUT = os.path.join(ROOT, "dist", "packages", "browserplug-server.zip")

# Everything below this name inside the archive; unzipping gives one folder, not a spill.
TOP = "server-package"

TREES = ("server", "launcher", "shared-py")
# Files at the repo root that ride along. README.md is what the Makefile always shipped; the
# legal files are the same three build_xpi.py adds to the extension packages.
ROOT_FILES = ("README.md", "LICENSE", "NOTICE", "THIRD-PARTY-NOTICES")
# Files whose absence means the tree is not the tree: refuse rather than ship half a server.
REQUIRED = ("server/server.py", "server/server.py.lock", "launcher/launcher.py",
            "shared-py/browserplug_shared/version.py")

# Paths relative to the repo root that are not shipped, directories included.
EXCLUDED_DIRS = ("server/tor/runtime",)
EXCLUDED_DIR_NAMES = {"__pycache__"}
EXCLUDED_SUFFIXES = (".pyc", ".pyo", ".log")
# Rotated logs: browserplug-server.log.1. `*.log` alone does not match them.
ROTATED_LOG = re.compile(r"\.log\.\d+$")
# Local state and secrets that must never travel in a package (state dir contents, if one
# ever sits inside the checkout, and dotenv files).
EXCLUDED_NAMES = {"ws-token", "config.json"}
EXCLUDED_PREFIXES = (".env",)

# 1980-01-01 is the earliest time a zip can store, and a constant keeps the build reproducible.
FIXED_TIME = (1980, 1, 1, 0, 0, 0)

VERSION_LINE = re.compile(r'^__version__\s*=\s*["\']([^"\']+)["\']', re.MULTILINE)


def _excluded_file(name):
    return (name.endswith(EXCLUDED_SUFFIXES) or ROTATED_LOG.search(name) is not None
            or name in EXCLUDED_NAMES or name.startswith(EXCLUDED_PREFIXES))


def collect(root):
    """The (absolute path, archive path) pairs for `root`, sorted by archive path.

    Raises FileNotFoundError naming the first missing source, so a broken checkout
    fails loudly instead of producing a partial package.
    """
    for rel in (*REQUIRED, *ROOT_FILES):
        if not os.path.isfile(os.path.join(root, rel)):
            raise FileNotFoundError(f"{rel} is missing from {root}")

    files = []
    for tree in TREES:
        tree_dir = os.path.join(root, tree)
        if not os.path.isdir(tree_dir):
            raise FileNotFoundError(f"{tree}/ is missing from {root}")
        for dirpath, dirs, filenames in os.walk(tree_dir):
            rel_dir = os.path.relpath(dirpath, root).replace(os.sep, "/")
            dirs[:] = [d for d in dirs
                       if d not in EXCLUDED_DIR_NAMES and f"{rel_dir}/{d}" not in EXCLUDED_DIRS]
            for name in filenames:
                if _excluded_file(name):
                    continue
                rel = f"{rel_dir}/{name}"
                files.append((os.path.join(dirpath, name), f"{TOP}/{rel}"))
    for name in ROOT_FILES:
        files.append((os.path.join(root, name), f"{TOP}/{name}"))

    files.sort(key=lambda pair: pair[1])
    return files


def _mode(path):
    """0o755 for a file that starts with a shebang, else 0o644: decided by content, not by the
    build machine's file system, so Windows and Linux builds agree."""
    with open(path, "rb") as fh:
        return 0o755 if fh.read(2) == b"#!" else 0o644


def build(out_path, root=ROOT):
    """Write the package to `out_path`; returns the archive paths in order."""
    files = collect(root)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    if os.path.exists(out_path):
        os.remove(out_path)  # rebuilt from scratch: nothing stale can survive an update
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for abs_path, arcname in files:
            info = zipfile.ZipInfo(arcname, FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3  # Unix, so the permission bits below are honoured
            info.external_attr = (0o100000 | _mode(abs_path)) << 16
            with open(abs_path, "rb") as fh:
                zf.writestr(info, fh.read(), zipfile.ZIP_DEFLATED, 9)
    return [arc for _abs, arc in files]


def version_of(root=ROOT):
    path = os.path.join(root, "shared-py", "browserplug_shared", "version.py")
    with open(path, encoding="utf-8") as fh:
        match = VERSION_LINE.search(fh.read())
    return match.group(1) if match else "unknown"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", default=DEFAULT_OUT, help="where to write the zip")
    args = parser.parse_args(argv)
    try:
        members = build(args.out)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"BrowserPlug server package: version={version_of()}")
    print(f"Packed {len(members)} files -> {args.out} ({os.path.getsize(args.out)} bytes)")
    for arc in members:
        print(f"  {arc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
