#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""
Sync the parity [shared] files from extension-gecko/ (source of truth) to
extension-chromium/ (design.md D2; the extension-common evaluation, model C1).

The parity.toml [shared] list IS the machine-readable manifest of exactly which files
must be byte-identical in both trees, so this is a thin loop over it. The shared set is
expanded with the SAME logic invariant C uses (parity_lib.expand_shared) — no duplicated
glob code — so the copy and the check can never disagree with the validator.

Usage (from the repo root, or anywhere — paths are cwd-independent):
    uv run --script tests/parity/sync_common.py            # copy gecko -> chromium
    uv run --script tests/parity/sync_common.py --check    # report drift, no copy

--check exits nonzero (listing the drifted files) if any shared file differs or is missing
in chromium; it copies nothing. It honors BROWSERPLUG_PARITY_TREE_ROOT exactly as parity_lib does
(via tree_dir), so it can be pointed at scratch copies of the trees.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # so `import parity_lib` works from any cwd
import parity_lib as P


def _shared_pairs():
    """[(relpath, gecko_path, chromium_path)] for the expanded [shared] set, sorted."""
    reg = P.load_registry()
    gecko_root = P.tree_dir(reg, "gecko")
    chromium_root = P.tree_dir(reg, "chromium")
    shared = P.expand_shared(reg, gecko_root)  # gecko is the source of truth
    return [(rel, gecko_root / rel, chromium_root / rel) for rel in sorted(shared)]


def check() -> int:
    drifted = []
    for rel, gp, cp in _shared_pairs():
        if not gp.is_file():
            drifted.append(f"{rel}: missing in extension-gecko/ (source)")
        elif not cp.is_file():
            drifted.append(f"{rel}: missing in extension-chromium/")
        elif P.sha256_file(gp) != P.sha256_file(cp):
            drifted.append(f"{rel}: sha256 differs")
    if drifted:
        print(f"DRIFT: {len(drifted)} shared file(s) out of sync (gecko -> chromium):")
        for line in drifted:
            print(f"  {line}")
        print("Re-sync with:  uv run --script tests/parity/sync_common.py")
        return 1
    print(f"OK: all {len(_shared_pairs())} shared files are byte-identical (gecko == chromium).")
    return 0


def sync() -> int:
    copied = 0
    for rel, gp, cp in _shared_pairs():
        if not gp.is_file():
            print(f"SKIP {rel}: missing in extension-gecko/ (source)")
            continue
        cp.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(gp, cp)
        print(f"copied {rel}")
        copied += 1
    print(f"Done: copied {copied} shared file(s) from extension-gecko/ to extension-chromium/.")
    return 0


def main(argv: list[str]) -> int:
    if argv and argv[0] in ("--check", "-c"):
        return check()
    if argv:
        print(f"unknown argument: {argv[0]!r} (use --check, or no args to copy)")
        return 2
    return sync()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
