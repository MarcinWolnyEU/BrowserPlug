# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Runs tests/docker/tor_smoke.py inside a Linux container (.tmp/dev-docs/tor.md, "Testing on Linux").

    uv run --script tests/docker/run_tor_linux.py                       # linux/amd64
    uv run --script tests/docker/run_tor_linux.py --platform linux/386  # the i686 bundle

The repo is mounted read-only and the container is removed afterwards. The smoke test
downloads the real Tor bundle and talks to the real Tor network, so it takes a few minutes.
"""

import argparse
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--platform", default="linux/amd64")
    parser.add_argument("--image", default="python:3.12-slim")
    args = parser.parse_args()
    command = [
        "docker", "run", "--rm", "--platform", args.platform,
        "-v", f"{ROOT}:/repo:ro",
        "-e", "PYTHONDONTWRITEBYTECODE=1", "-e", "PYTHONUNBUFFERED=1",
        args.image, "python", "/repo/tests/docker/tor_smoke.py",
    ]
    print(" ".join(command), flush=True)
    sys.exit(subprocess.run(command).returncode)


if __name__ == "__main__":
    main()
