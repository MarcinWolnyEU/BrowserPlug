#!/usr/bin/env bash
# Ensure the vendored Chrome for Testing browser exists at dist/chrome-for-testing.
#
# That binary is what the chromium half of the integration suite runs against when
# CHROME_PATH is unset (tests/chromium_test_utils.py, resolve_chrome_binary). It has
# to be Chrome for Testing specifically: branded Chrome 137+ removed --load-extension,
# so only CfT can load the unpacked MV3 tree the harness bakes. A missing binary is
# the ONE sanctioned skip in that suite, so losing it doesn't fail the run — it
# quietly drops ~92 chromium tests to "skipped". Hence this script.
#
# Downloads come from the official Chrome for Testing endpoint:
#   https://googlechromelabs.github.io/chrome-for-testing/
# Idempotent by default: an installed browser is left alone, so a re-run never
# upgrades the browser out from under a test session. Ask for that explicitly.
#
# Usage:
#   scripts/fetch-chrome-for-testing.sh              install if missing, else do nothing
#   scripts/fetch-chrome-for-testing.sh --check      report status only; exit 1 if missing
#   scripts/fetch-chrome-for-testing.sh --force      re-download even if present
#   scripts/fetch-chrome-for-testing.sh --version 151.0.7922.71
#   scripts/fetch-chrome-for-testing.sh --channel Beta
#   scripts/fetch-chrome-for-testing.sh --platform linux64
#   scripts/fetch-chrome-for-testing.sh --help
#
# Env:
#   CFT_CHANNEL    default channel when no --version is given (default: Stable)
#   CFT_PLATFORM   override platform detection (win64|win32|linux64|mac-x64|mac-arm64)

set -euo pipefail

usage() { sed -n '2,27p' "$0" | sed 's/^#\s\{0,1\}//'; }

MODE="ensure"
WANT_VERSION=""
CHANNEL="${CFT_CHANNEL:-Stable}"
PLATFORM="${CFT_PLATFORM:-}"

while [ $# -gt 0 ]; do
  case "$1" in
    --check)      MODE="check" ;;
    --force)      MODE="force" ;;
    --version)    WANT_VERSION="${2:-}"; shift
                  [ -n "$WANT_VERSION" ] || { echo "--version needs a value (e.g. 151.0.7922.71)" >&2; exit 2; } ;;
    --channel)    CHANNEL="${2:-}"; shift
                  [ -n "$CHANNEL" ] || { echo "--channel needs a value (Stable|Beta|Dev|Canary)" >&2; exit 2; } ;;
    --platform)   PLATFORM="${2:-}"; shift
                  [ -n "$PLATFORM" ] || { echo "--platform needs a value (win64|win32|linux64|mac-x64|mac-arm64)" >&2; exit 2; } ;;
    -h|--help)    usage; exit 0 ;;
    *)            echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

# Resolve repo root from this script's location (scripts/ -> repo root).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Detect the CfT platform slug unless one was given. Note the harness's dist fallback
# is win64-only (_DIST_CHROME); on POSIX the printed CHROME_PATH export is the route.
if [ -z "$PLATFORM" ]; then
  case "$(uname -s)" in
    MINGW*|MSYS*|CYGWIN*)  PLATFORM="win64" ;;
    Linux)                 PLATFORM="linux64" ;;
    Darwin)                case "$(uname -m)" in
                             arm64|aarch64) PLATFORM="mac-arm64" ;;
                             *)             PLATFORM="mac-x64" ;;
                           esac ;;
    *)  echo "cannot detect platform from uname '$(uname -s)'; pass --platform" >&2; exit 1 ;;
  esac
fi

# Where the binary lands, per platform. The zip carries its own top-level directory
# (chrome-win64/, chrome-linux64/, …) so extracting into dist/chrome-for-testing
# reproduces exactly the layout resolve_chrome_binary expects.
DEST="$ROOT/dist/chrome-for-testing"
case "$PLATFORM" in
  win64|win32)        BIN="$DEST/chrome-$PLATFORM/chrome.exe" ;;
  linux64)            BIN="$DEST/chrome-linux64/chrome" ;;
  mac-x64|mac-arm64)  BIN="$DEST/chrome-$PLATFORM/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing" ;;
  *) echo "unknown platform '$PLATFORM' (want win64|win32|linux64|mac-x64|mac-arm64)" >&2; exit 2 ;;
esac
MARKER="$DEST/.cft-version"

installed_version() {
  if [ -f "$MARKER" ]; then
    cat "$MARKER"
    return
  fi
  # No marker: a hand-placed install predating this script. Windows CfT builds ship a
  # <version>.manifest next to chrome.exe, which is the same number.
  local m
  m="$(ls "$(dirname "$BIN")"/[0-9]*.manifest 2>/dev/null | head -1 || true)"
  if [ -n "$m" ]; then
    basename "$m" .manifest
  else
    echo "unknown"
  fi
}

if [ -f "$BIN" ] && [ "$MODE" != "force" ]; then
  HAVE="$(installed_version)"
  echo "Chrome for Testing is present: $BIN"
  echo "  version: $HAVE  platform: $PLATFORM"
  [ "$MODE" = "check" ] && exit 0
  # An explicit --version is a request to CHANGE the pin, so a mismatch downloads.
  # A channel is not: --channel Stable must not silently upgrade an install just
  # because upstream moved, which is the whole point of being idempotent.
  if [ -n "$WANT_VERSION" ] && [ "$WANT_VERSION" != "$HAVE" ]; then
    echo "  replacing it with $WANT_VERSION"
  else
    echo "  nothing to do (use --force to re-download, --version X to change it)"
    exit 0
  fi
fi

if [ "$MODE" = "check" ]; then
  echo "Chrome for Testing is MISSING (expected at $BIN)" >&2
  echo "  run: scripts/fetch-chrome-for-testing.sh" >&2
  exit 1
fi

# Only stdlib is used (urllib + zipfile), so any Python 3 does: uv's, as every other repo
# script runs, else a python3/python on PATH. The probe weeds out the Windows Store stub.
if command -v uv >/dev/null 2>&1; then
  PY=(uv run --no-project python)
elif command -v python3 >/dev/null 2>&1 && python3 -c "" >/dev/null 2>&1; then
  PY=(python3)
elif command -v python >/dev/null 2>&1 && python -c "" >/dev/null 2>&1; then
  PY=(python)
else
  echo "no python found (looked for uv, then python3 and python on PATH)" >&2; exit 1
fi

if [ -n "$WANT_VERSION" ]; then
  echo "Fetching Chrome for Testing: platform=$PLATFORM version=$WANT_VERSION"
else
  echo "Fetching Chrome for Testing: platform=$PLATFORM channel=$CHANNEL"
fi

DEST="$DEST" PLATFORM="$PLATFORM" CHANNEL="$CHANNEL" WANT_VERSION="$WANT_VERSION" "${PY[@]}" - <<'PY'
"""Resolve the download URL, fetch the zip, and swap it into place atomically."""
import json
import os
import shutil
import stat
import sys
import tempfile
import urllib.request
import zipfile

DEST = os.environ["DEST"]
PLATFORM = os.environ["PLATFORM"]
CHANNEL = os.environ["CHANNEL"]
WANT_VERSION = os.environ["WANT_VERSION"]

LAST_KNOWN = "https://googlechromelabs.github.io/chrome-for-testing/last-known-good-versions-with-downloads.json"
KNOWN = "https://googlechromelabs.github.io/chrome-for-testing/known-good-versions-with-downloads.json"


def fail(msg):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def get_json(url):
    try:
        with urllib.request.urlopen(url, timeout=30) as res:
            return json.load(res)
    except Exception as e:
        fail(f"could not read {url}\n  {type(e).__name__}: {e}")


def pick(downloads, version):
    """The chrome download for our platform, out of one version's download table."""
    for entry in downloads.get("chrome") or []:
        if entry.get("platform") == PLATFORM:
            return entry["url"]
    have = ", ".join(sorted(e.get("platform", "?") for e in downloads.get("chrome") or []))
    fail(f"version {version} has no chrome build for platform '{PLATFORM}' (has: {have})")


if WANT_VERSION:
    # An exact pin: every known-good version stays in the bucket, so old pins keep
    # working. This file is large, hence only read when a pin is actually asked for.
    data = get_json(KNOWN)
    for entry in data.get("versions") or []:
        if entry.get("version") == WANT_VERSION:
            version, url = WANT_VERSION, pick(entry.get("downloads") or {}, WANT_VERSION)
            break
    else:
        fail(f"version {WANT_VERSION} is not in the Chrome for Testing known-good list")
else:
    data = get_json(LAST_KNOWN)
    channels = data.get("channels") or {}
    # Accept any casing for the channel; the API's keys are Stable/Beta/Dev/Canary.
    match = next((k for k in channels if k.lower() == CHANNEL.lower()), None)
    if match is None:
        fail(f"unknown channel '{CHANNEL}' (have: {', '.join(sorted(channels))})")
    version = channels[match]["version"]
    url = pick(channels[match].get("downloads") or {}, version)

print(f"  version {version}")
print(f"  {url}")

tmp_dir = tempfile.mkdtemp(prefix=".cft-", dir=os.path.dirname(DEST) or ".")
zip_path = os.path.join(tmp_dir, "chrome.zip")
staging = os.path.join(tmp_dir, "unpacked")
try:
    # Download to a temp file, unpack to a temp dir, and only then swap: a failed or
    # interrupted fetch must never leave the suite with a half-installed browser,
    # which resolve_chrome_binary would happily hand to a test.
    # urlretrieve calls the hook per 8 KB block — ~24 000 times for a 190 MB zip. Print
    # only when the whole percent changes, so a captured (non-TTY) log gets 100 lines
    # rather than 24 000 of them.
    last_pct = [-1]

    def progress(done, block, total):
        if total <= 0:
            return
        pct = min(100, done * block * 100 // total)
        if pct != last_pct[0]:
            last_pct[0] = pct
            print(f"\r  downloading… {pct}% of {total // (1024 * 1024)} MB", end="", file=sys.stderr)

    urllib.request.urlretrieve(url, zip_path, reporthook=progress)
    print("", file=sys.stderr)

    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(staging)

    # zipfile drops the executable bit; restore it from the archive's own mode bits
    # so the extracted browser is runnable on POSIX (a no-op on Windows).
    if os.name != "nt":
        with zipfile.ZipFile(zip_path) as zf:
            for info in zf.infolist():
                mode = info.external_attr >> 16
                if mode & stat.S_IXUSR:
                    target = os.path.join(staging, info.filename)
                    if os.path.exists(target):
                        os.chmod(target, os.stat(target).st_mode | 0o755)

    with open(os.path.join(staging, ".cft-version"), "w", encoding="utf-8") as fh:
        fh.write(version + "\n")

    if os.path.exists(DEST):
        shutil.rmtree(DEST)
    shutil.move(staging, DEST)
finally:
    shutil.rmtree(tmp_dir, ignore_errors=True)

print(f"Installed Chrome for Testing {version} ({PLATFORM}) -> {DEST}")
PY

[ -f "$BIN" ] || { echo "error: download finished but $BIN is not there" >&2; exit 1; }

echo "Binary: $BIN"
case "$PLATFORM" in
  win64) ;;
  *) echo "Note: the test harness's dist fallback is win64-only, so on this platform export:"
     echo "  export CHROME_PATH=\"$BIN\"" ;;
esac
