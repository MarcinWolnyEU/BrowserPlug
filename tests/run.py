#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11,<3.13"
# dependencies = [
#     # The server's dependencies, each with the SAME specifier as the block in
#     # server/server.py (tests/unit/test_declared_dependencies.py enforces it, and that the
#     # two locks agree on every shared version), so the suite runs on what the server runs.
#     "mcp>=2.2.0,<3",
#     "mcp-types>=2.2.0,<3",
#     "websockets>=17.1",
#     "uvicorn>=0.54.0",
#     "pydantic>=2.13.5",
#     "psutil>=7.2.2",
#     "cryptography>=48",
#     "anyio>=4.15.1",
#     "httpx2>=2.13.1",
#     # The test tools, pinned: the suite's own behaviour (timeouts, asyncio mode, the ruff
#     # gate) depends on them.
#     "pytest==9.1.1",
#     "pytest-asyncio==1.4.0",
#     # Hang guard: pytest.ini sets `timeout`, an unknown ini key without it.
#     "pytest-timeout==2.4.0",
#     # The parallel phase of `--integration` (pytest -n).
#     "pytest-xdist==3.8.0",
#     # Screenshot decoding in test_agent_experience.py (missing, those checks would skip).
#     "pillow>=12.3.0",
#     # The static gate (tests/unit/test_static_analysis.py) and `--ruff`.
#     "ruff==0.16.9",
# ]
# ///
"""Run the test suite, or ruff, in the environment this file declares.

    uv run --locked --script tests/run.py [pytest args]
    uv run --locked --script tests/run.py --ruff check server launcher shared-py tests
    uv run --locked --script tests/run.py --integration [--workers N] [--log-dir DIR] [pytest args]
    uv run --locked --script tests/run.py --help        (this text, then pytest's own help)

pytest runs from tests/ whatever the caller's cwd, with the repo root and shared-py/ on
sys.path (tests/conftest.py expects both) and the repo root on PYTHONPATH for the processes
tests spawn, which is what `cd tests && PYTHONPATH=.. python -m pytest` did. A test path
given relative to the caller's cwd (`tests/unit/test_x.py` from the repo root) is made
absolute, so both spellings work. `--ruff` runs ruff from this same environment, in the
caller's cwd.

BROWSERPLUG_SERVER_PYTHON is set to this interpreter (unless already set): every server a
test spawns, and every launcher a test starts, then runs on this environment instead of
building or touching the user's real server environment (shared-py/browserplug_shared/
server_env.py). `--locked` matters: a plain `uv run --script` silently rewrites
tests/run.py.lock when the block and the lock disagree.

`--integration` runs the browser suite in two phases, each a fresh pytest process:

  1. parallel: `-n N --dist load -m "not timing"`, N from --workers (default 4). Every test
     still gets its own server and browser; N of them run at once.
  2. serial: `-n 0 -m timing`, the tests that assert a wall-clock bound or race the browser
     on purpose (the `timing` marker in pytest.ini), with no other browser using the CPU.

With no test path it runs integration/. A `-m EXPR` is combined with each phase's own
expression; pass --workers, not -n. Both phases get `-p no:cacheprovider`, since parallel
workers would race on tests/.pytest_cache. BROWSERPLUG_TEST_BROWSERS defaults to both
engines and is set before either phase starts, because conftest.py reads it at import in
every worker. FIREFOX_PATH must be set for the Firefox half, as for any integration run.

Each phase writes a JUnit file and an INFO log into --log-dir (default: a new
browserplug-integration-* directory in the system temp dir, kept after the run), one log
per xdist worker (`parallel-gw0.log`, ...); a reused --log-dir is appended to. The run
ends with one summary of both phases: wall time, counts and the failing tests. The exit
status is nonzero when either phase failed. Run one integration invocation at a time on a
machine: the browsers of two runs compete for the CPU and push the timing tests over their
bounds.
"""

import os
import subprocess
import sys
import tempfile
import time

TESTS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS)
SHARED = os.path.join(ROOT, "shared-py")


# pytest options (and the plugins this file declares) that take their value as the NEXT
# argument. That value is an expression, a marker, a plugin name, a number..., never a test
# path, so it passes as given: `-k launcher` from the repo root must not become
# `-k C:\dr\myproject\launcher` just because ./launcher exists.
_VALUE_OPTIONS = frozenset({
    "-k", "-m", "-p", "-o", "-W", "-r", "--tb", "--maxfail", "--override-ini",
    "--pythonwarnings", "--durations", "--durations-min", "--capture", "--import-mode",
    "--color", "--code-highlight", "--show-capture", "--log-level", "--log-format",
    "--log-date-format", "--log-cli-level", "--log-cli-format", "--log-cli-date-format",
    "--log-file-level", "--log-file-format", "--log-file-date-format", "--junit-prefix",
    "--doctest-glob", "--doctest-report", "--pdbcls", "--verbosity", "--asyncio-mode",
    "--timeout", "--timeout-method", "--timeout_method", "--lfnf", "--last-failed-no-failures",
    "-n", "--numprocesses", "--dist", "--maxprocesses", "--max-worker-restart",
})
# Options whose value IS a path: rewritten by the same rule as a positional test path.
_PATH_OPTIONS = frozenset({
    "--ignore", "--ignore-glob", "--deselect", "-c", "--config-file", "--rootdir",
    "--confcutdir", "--basetemp", "--junitxml", "--junit-xml", "--log-file",
})


def _looks_like_a_path(arg):
    return "::" in arg or arg.endswith(".py") or "/" in arg or os.sep in arg


def _rebase(arg, caller_cwd):
    """`arg` made absolute when it is a test path that exists relative to the caller's cwd
    but not relative to tests/; otherwise unchanged."""
    if not _looks_like_a_path(arg):
        return arg
    path, sep, rest = arg.partition("::")
    if (path and not os.path.isabs(path) and not os.path.exists(os.path.join(TESTS, path))
            and os.path.exists(os.path.join(caller_cwd, path))):
        return os.path.abspath(os.path.join(caller_cwd, path)) + sep + rest
    return arg


def _from_caller(args, caller_cwd):
    """Test paths that exist relative to the caller's cwd but not relative to tests/ become
    absolute; options, option values and node ids under tests/ pass as given."""
    out = []
    pending = None          # the option whose separate value is the next argument
    for arg in args:
        if pending is not None:
            out.append(_rebase(arg, caller_cwd) if pending in _PATH_OPTIONS else arg)
            pending = None
            continue
        if arg.startswith("-"):
            name, eq, value = arg.partition("=")
            if eq and name in _PATH_OPTIONS:
                arg = name + eq + _rebase(value, caller_cwd)
            elif not eq and (name in _VALUE_OPTIONS or name in _PATH_OPTIONS):
                pending = name
            out.append(arg)
            continue
        out.append(_rebase(arg, caller_cwd))
    return out


def _ruff(args):
    return subprocess.call([sys.executable, "-m", "ruff", *args])


def _pytest(args):
    caller_cwd = os.getcwd()
    args = _from_caller(args, caller_cwd)
    os.environ.setdefault("BROWSERPLUG_SERVER_PYTHON", sys.executable)
    existing = os.environ.get("PYTHONPATH")
    os.environ["PYTHONPATH"] = os.pathsep.join([ROOT, existing] if existing else [ROOT])
    for path in (ROOT, SHARED):
        if path not in sys.path:
            sys.path.insert(0, path)
    os.chdir(TESTS)
    import pytest
    return pytest.main(args)


# The two phases of --integration: (name, distribution arguments, marker expression). The
# parallel phase's arguments depend on --workers, so None stands in for them here.
_PHASES = (
    ("parallel", None, "not timing"),
    ("serial", ["-n", "0"], "timing"),
)
_DEFAULT_WORKERS = 4
_LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def _has_test_path(args):
    """Whether `args` name a test path (a positional argument, not an option's value)."""
    pending = False
    for arg in args:
        if pending:
            pending = False
            continue
        if arg.startswith("-"):
            name, eq, _ = arg.partition("=")
            pending = not eq and (name in _VALUE_OPTIONS or name in _PATH_OPTIONS)
            continue
        return True
    return False


def _take_marker(args):
    """(`args` without -m, the last -m expression or None)."""
    out, expr, it = [], None, iter(args)
    for arg in it:
        if arg == "-m":
            expr = next(it, None)
        elif arg.startswith("-m") and len(arg) > 2 and not arg.startswith("--"):
            expr = arg[2:]
        else:
            out.append(arg)
    return out, expr


def _junit_summary(path):
    """({passed, failed, error, skipped}, [(outcome, test id)]) from a JUnit file, or
    (None, []) when the phase wrote none (it stopped before pytest started)."""
    import xml.etree.ElementTree as ET
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return None, []
    counts = dict.fromkeys(("passed", "failed", "error", "skipped"), 0)
    failing = []
    for case in root.iter("testcase"):
        kinds = {child.tag for child in case}
        test_id = f"{case.get('classname', '')}::{case.get('name', '')}"
        if "failure" in kinds:
            counts["failed"] += 1
            failing.append(("FAILED", test_id))
        elif "error" in kinds:
            counts["error"] += 1
            failing.append(("ERROR", test_id))
        elif "skipped" in kinds:
            counts["skipped"] += 1
        else:
            counts["passed"] += 1
    return counts, failing


def _minutes(seconds):
    return f"{int(seconds // 60)}m{int(seconds % 60):02d}s"


def _integration(args):
    workers, log_dir, rest = _DEFAULT_WORKERS, None, []
    it = iter(args)
    for arg in it:
        if arg in ("-h", "--help"):
            print(__doc__)
            return 0
        name, eq, value = arg.partition("=")
        if name in ("--workers", "--log-dir"):
            value = value if eq else next(it, None)
            if value is None:
                print(f"{name} needs a value", file=sys.stderr)
                return 2
            if name == "--workers":
                workers = int(value)
            else:
                log_dir = value
            continue
        rest.append(arg)
    if workers < 1:
        print("--workers must be at least 1", file=sys.stderr)
        return 2
    rest, user_marker = _take_marker(rest)
    if not _has_test_path(rest):
        rest.append("integration/")

    # conftest.py reads the engine list at import, so every worker must see one value.
    os.environ.setdefault("BROWSERPLUG_TEST_BROWSERS", "firefox,chromium")
    # The harness prints emoji, which a cp1252 console cannot encode.
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    browsers = os.environ["BROWSERPLUG_TEST_BROWSERS"]
    firefox = os.environ.get("FIREFOX_PATH")
    if "firefox" in browsers and not (firefox and os.path.exists(os.path.expanduser(firefox))):
        print(f"WARNING: FIREFOX_PATH is {firefox!r}, so every Firefox test will be skipped")
    log_dir = os.path.abspath(log_dir or tempfile.mkdtemp(prefix="browserplug-integration-"))
    os.makedirs(log_dir, exist_ok=True)

    results = []
    for name, dist, expr in _PHASES:
        dist = dist or ["-n", str(workers), "--dist", "load"]
        marker = f"({user_marker}) and {expr}" if user_marker else expr
        junit = os.path.join(log_dir, f"{name}.xml")
        cmd = [sys.executable, os.path.abspath(__file__), *rest, *dist, "-m", marker,
               "-p", "no:cacheprovider", f"--junitxml={junit}",
               f"--log-file={os.path.join(log_dir, name + '.log')}",
               "--log-file-level=INFO", f"--log-file-format={_LOG_FORMAT}",
               # Append, so the file reopens: every test server's uvicorn.Config runs
               # logging.config.dictConfig, which closes all handlers, and a handler
               # opened with "w" stays closed and drops the rest of the session.
               "--log-file-mode=a"]
        started = time.strftime("%H:%M:%S")
        print(f"\n===== {name} phase: {' '.join(dist)} -m \"{marker}\" "
              f"(BROWSERPLUG_TEST_BROWSERS={browsers}), started {started} =====", flush=True)
        t0 = time.monotonic()
        rc = subprocess.call(cmd)
        wall = time.monotonic() - t0
        counts, failing = _junit_summary(junit)
        results.append((name, " ".join(dist), started, wall, rc, counts, failing))

    print(f"\n===== integration summary (logs and JUnit files in {log_dir}) =====")
    for name, dist, started, wall, rc, counts, _ in results:
        shown = (", ".join(f"{k} {v}" for k, v in counts.items()) if counts
                 else "no JUnit file written")
        print(f"{name:<9} {dist:<20} started {started}  {_minutes(wall):>7}  rc={rc}  {shown}")
    total = sum(r[3] for r in results)
    print(f"{'total':<9} {'':<20} {'':<16}  {_minutes(total):>7}")
    for name, *_, failing in results:
        for outcome, test_id in failing:
            print(f"{outcome} {test_id} ({name} phase)")

    # 5 is "no tests collected": a phase the selection left empty, not a failure.
    codes = [r[4] for r in results]
    failed = [rc for rc in codes if rc not in (0, 5)]
    if failed:
        return failed[0]
    return 5 if all(rc == 5 for rc in codes) else 0


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["--ruff"]:
        return _ruff(args[1:])
    if args[:1] == ["--integration"]:
        return _integration(args[1:])
    if args[:1] in (["--help"], ["-h"]):
        print(__doc__)
    return _pytest(args)


if __name__ == "__main__":
    sys.exit(main())
