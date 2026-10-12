#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Check that a release tag and the four version carriers agree.

    python scripts/check_release_version.py                 # the carriers agree with each other
    python scripts/check_release_version.py --tag v1.0.14    # ... and with this tag
    python scripts/check_release_version.py --tag v1.0.14rc1 --github-output "$GITHUB_OUTPUT"

The carriers are the two manifest.json files, shared-py/browserplug_shared/version.py and
package.json. They are read with the helpers parity invariant H uses, so this check and
that test cannot disagree about where a version lives.

Browsers accept only dotted integers in a manifest version, so a pre-release tag cannot
be copied into the carriers. A tag such as v1.0.14rc1 carries 1.0.14 in the carriers and
adds the rc1 suffix on top; the suffix (aN, bN, rcN or .devN) marks the GitHub release as a
pre-release. The full tag version names the release assets.

With --github-output the script appends version, base_version and prerelease to that file,
in the key=value form a GitHub Actions step output takes.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests" / "parity"))

import parity_lib as P  # noqa: E402  (stdlib-only; needs the path line above)

# 1.0.14, 1.0.14.1 and a pre-release suffix: a1, b2, rc3, .dev4 (PEP 440 spelling).
_TAG_RE = re.compile(r"^v(?P<base>\d+\.\d+\.\d+(?:\.\d+)?)(?P<suffix>(?:a|b|rc)\d+|\.dev\d+)?$")


def parse_tag(tag: str) -> tuple[str, str]:
    """Split a tag such as v1.0.14rc1 into ('1.0.14', 'rc1'). Raises ValueError on a bad shape."""
    m = _TAG_RE.match(tag)
    if not m:
        raise ValueError(
            f"tag {tag!r} is not vMAJOR.MINOR.PATCH with an optional aN, bN, rcN or .devN suffix")
    return m.group("base"), (m.group("suffix") or "")


def carriers() -> dict[str, str]:
    """The four version carriers, by the path a maintainer edits."""
    return {
        "extension-gecko/manifest.json": P.parse_manifest(ROOT / "extension-gecko")["version"],
        "extension-chromium/manifest.json": P.parse_manifest(ROOT / "extension-chromium")["version"],
        "shared-py/browserplug_shared/version.py": P.parse_python_version(),
        "package.json": P.parse_package_json_version(),
    }


def check(tag: str | None) -> tuple[str, str, bool, list[str]]:
    """Return (version, base_version, prerelease, problems). `problems` empty means pass."""
    found = carriers()
    problems: list[str] = []
    if len(set(found.values())) != 1:
        problems.append("the version carriers disagree: "
                        + ", ".join(f"{path}={v}" for path, v in found.items()))
    base = next(iter(found.values()))
    suffix = ""
    if tag is not None:
        try:
            tag_base, suffix = parse_tag(tag)
        except ValueError as exc:
            problems.append(str(exc))
        else:
            if tag_base != base:
                problems.append(f"tag {tag} is for {tag_base} but the carriers say "
                                + ", ".join(f"{path}={v}" for path, v in found.items()))
    return base + suffix, base, bool(suffix), problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tag", help="release tag to check against, for example v1.0.14")
    parser.add_argument("--github-output", metavar="FILE",
                        help="append version, base_version and prerelease to this step-output file")
    args = parser.parse_args(argv)

    version, base, prerelease, problems = check(args.tag)
    if problems:
        for problem in problems:
            print(f"ERROR: {problem}", file=sys.stderr)
        return 1
    print(f"version {version} (carriers {base}, prerelease={str(prerelease).lower()})")
    if args.github_output:
        with open(args.github_output, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(f"version={version}\nbase_version={base}\nprerelease={str(prerelease).lower()}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
