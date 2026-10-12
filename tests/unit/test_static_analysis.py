"""Static gate: ruff on server/, plus a scan for double-encoded UTF-8.

ruff is a hard requirement of the unit suite, not an optional tool: a missing binary fails
here with the command that provides it instead of skipping, because a skipped gate reads as
a pass. It comes from the environment running the suite (tests/run.py declares and pins
it), never from a fixed path in the checkout. The rule set lives in ruff.toml (pyflakes,
and ASYNC for blocking calls in async code).
"""
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _ruff_command():
    """ruff next to this interpreter (its environment's Scripts/ or bin/), else `python -m
    ruff` when the package is importable here; None when this environment has no ruff."""
    exe = "ruff.exe" if os.name == "nt" else "ruff"
    for directory in (Path(sys.prefix) / ("Scripts" if os.name == "nt" else "bin"),
                      Path(sys.executable).parent):
        if (directory / exe).is_file():
            return [str(directory / exe)]
    if importlib.util.find_spec("ruff") is not None:
        return [sys.executable, "-m", "ruff"]
    return None


RUFF = _ruff_command()

# UTF-8 bytes of an em dash, quote or arrow read back as cp1252 and re-encoded: the
# lead byte 0xE2 becomes "a circumflex" and the next byte becomes a euro sign or similar.
# Built from escapes so this file does not contain the marker itself.
MOJIBAKE_MARKERS = ("â€", "Ã©", "Â ")


def test_ruff_is_installed():
    assert RUFF, (
        f"ruff is not in the environment running the suite ({sys.prefix}). Run the suite "
        f"with `uv run --locked --script tests/run.py`, whose PEP 723 block declares it"
    )


def test_ruff_finds_nothing_in_the_python_source_folders():
    assert RUFF, "ruff is not installed; see test_ruff_is_installed"
    proc = subprocess.run(
        [*RUFF, "check", "server", "launcher", "shared-py", "--no-cache",
         "--output-format", "concise"],
        cwd=ROOT, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, f"ruff check server launcher shared-py failed:\n{proc.stdout}{proc.stderr}"


def test_no_mojibake_in_server_and_docs():
    offenders = []
    files = (sorted((ROOT / "server").rglob("*.py")) + sorted((ROOT / "launcher").rglob("*.py"))
             + sorted((ROOT / "shared-py" / "browserplug_shared").rglob("*.py"))
             + sorted((ROOT / "docs").glob("*.md")))
    assert files, "found no server/*.py or docs/*.md files to scan"
    for path in files:
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            if any(marker in line for marker in MOJIBAKE_MARKERS):
                offenders.append(f"{path.relative_to(ROOT)}:{lineno}")
    assert not offenders, (
        "double-encoded UTF-8 (text decoded as cp1252, then saved as UTF-8) at: "
        + ", ".join(offenders)
    )
