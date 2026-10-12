#!/usr/bin/env python
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Run the server against the LOWEST versions its declared dependencies allow.

`tests/unit/test_declared_dependencies.py` only compares the installed versions with the
declared ranges, so it cannot notice a floor that is too low: the locks hold the newest
releases, which satisfy any floor. This script closes that gap. It reads the dependencies
from the PEP 723 block of tests/run.py (every server dependency with the server's own
specifier, plus the pinned test tools), resolves the lowest allowed version of every DIRECT
requirement (`uv pip compile --resolution lowest-direct`; transitive packages still resolve
to their newest compatible release), installs the result into a throwaway venv, and runs an
import check plus a unit subset there. A floor at which a test fails is too low: raise it in
server/server.py's block and tests/run.py's together (the identical-specifier test holds
them equal), then relock both.

    uv run --script tests/check_dependency_floors.py                 # floors, Python 3.12, subset
    uv run --script tests/check_dependency_floors.py --python 3.11   # uv fetches the interpreter
    uv run --script tests/check_dependency_floors.py --full          # the whole unit + parity suite
    uv run --script tests/check_dependency_floors.py --mode lock     # install tests/run.py.lock
    uv run --script tests/check_dependency_floors.py --full -- -x --ignore=unit/test_x.py   # extra pytest args

Everything lives in a scratch directory outside the repo (default: the system temp dir),
and nothing is installed with a bare `pip`. The scratch venv is reused between runs unless
`--fresh` is given. Exit status is 0 only when the import check and the tests both pass.
Needs `uv` and, on the first run, network access.
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SERVER_SCRIPT = REPO_ROOT / "server" / "server.py"
RUNNER_SCRIPT = REPO_ROOT / "tests" / "run.py"

# Chosen because each one runs code that lives inside a dependency's API surface:
# cryptography (ML-KEM, AES-GCM), pydantic and mcp (tool registration, the tools/list
# golden, the registry's validation, the low-level server), the SDK client, httpx2 and anyio
# through the proxy transport, the stdlib relay, starlette/uvicorn through the HTTP
# endpoints, websockets through the server, psutil through the probes and the process-facts
# cross-check (the launcher's stdlib readers are compared with it), and the declared
# dependency checks themselves. About 35 s where the full suite takes a few minutes.
SUBSET = [
    "unit/test_declared_dependencies.py",
    "unit/test_ws_crypto.py",
    "unit/test_ws_crypto_interop.py",
    "unit/test_request_signing.py",
    "unit/test_tool_registry.py",
    "unit/test_docstrings.py",
    "unit/test_tools_list_golden.py",
    "unit/test_server_facade.py",
    "unit/test_http_endpoints.py",
    "unit/test_http_auth.py",
    "unit/test_stdio_relay.py",
    "unit/test_probes.py",
    "unit/test_proc_facts.py",
    "unit/test_preflight.py",
    "unit/test_server_env.py",
    "unit/test_server.py",
    "unit/test_mcp_server.py",
    "unit/test_proxy_backend.py",
    "unit/test_proxy_front.py",
]

IMPORT_CHECK = r"""
import importlib, importlib.util, pkgutil, sys
failed = []
for top in ("server", "launcher", "browserplug_shared"):
    pkg = importlib.import_module(top)
    for info in pkgutil.walk_packages(pkg.__path__, top + "."):
        if info.name.endswith("__main__"):
            continue
        try:
            importlib.import_module(info.name)
        except Exception as exc:                  # report every failure, not the first
            failed.append(f"{info.name}: {type(exc).__name__}: {exc}")
if importlib.util.find_spec("fastmcp") is not None:
    failed.append("fastmcp is importable: the environment carries a package nothing declares")
for line in failed:
    print("IMPORT FAILED", line)
print(f"imported server.*, launcher.* and browserplug_shared.* with {len(failed)} failure(s) on Python {sys.version.split()[0]}")
sys.exit(1 if failed else 0)
"""

# PEP 723's reference regex.
_BLOCK = re.compile(r"(?m)^# /// (?P<type>[a-zA-Z0-9-]+)$\s(?P<content>(^#(| .*)$\s)+)^# ///$")


def script_dependencies(path):
    """The `dependencies` of `path`'s PEP 723 block."""
    text = path.read_text(encoding="utf-8")
    match = next(m for m in _BLOCK.finditer(text) if m.group("type") == "script")
    lines = match.group("content").splitlines(keepends=True)
    meta = tomllib.loads("".join(ln[2:] if ln.startswith("# ") else ln[1:] for ln in lines))
    return list(meta.get("dependencies", []))


def run(cmd, **kwargs):
    print("+", " ".join(str(c) for c in cmd), flush=True)
    return subprocess.run([str(c) for c in cmd], **kwargs)


def venv_python(venv):
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--python", default="3.12", help="interpreter for the scratch venv (default 3.12)")
    ap.add_argument("--mode", choices=("floors", "lock"), default="floors",
                    help="floors: lowest-direct of the declared dependencies; lock: tests/run.py.lock")
    ap.add_argument("--full", action="store_true", help="run all of unit/ and parity/ instead of the subset")
    ap.add_argument("--fresh", action="store_true", help="delete the scratch venv first")
    ap.add_argument("--scratch", type=Path, default=Path(tempfile.gettempdir()) / "browserplug-dep-check",
                    help="scratch directory; must not be inside the repo")
    ap.add_argument("pytest_args", nargs="*", help="after `--`: extra arguments for pytest")
    args = ap.parse_args(argv)

    uv = shutil.which("uv")
    if not uv:
        print("uv is not on PATH; install it first (https://docs.astral.sh/uv/)")
        return 2
    scratch = args.scratch.resolve()
    if REPO_ROOT in (scratch, *scratch.parents):
        print(f"refusing scratch dir {scratch}: it is inside the repository")
        return 2

    work = scratch / f"{args.mode}-py{args.python}"
    venv = work / "venv"
    if args.fresh and work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True, exist_ok=True)

    resolved = work / ("floors.txt" if args.mode == "floors" else "locked.txt")
    # uv treats an existing -o file as version preferences, which would keep a stale
    # resolution alive after a floor is lowered and hide the very failure this looks for.
    resolved.unlink(missing_ok=True)
    if args.mode == "floors":
        declared = work / "declared.in"
        declared.write_text("\n".join(script_dependencies(RUNNER_SCRIPT)) + "\n", encoding="utf-8")
        r = run([uv, "pip", "compile", declared, "--resolution", "lowest-direct",
                 "--python-version", args.python, "-o", resolved, "-q"], cwd=work)
        if r.returncode:
            print("could not resolve the floors: some declared range is unsatisfiable")
            return r.returncode
    else:
        # The runner's lock pins every server package at the server lock's version
        # (test_both_locks_agree_on_every_shared_package), plus the test tools.
        r = run([uv, "export", "--script", RUNNER_SCRIPT, "--locked", "--no-hashes",
                 "--no-header", "--no-annotate", "-o", resolved, "-q"], cwd=REPO_ROOT)
        if r.returncode:
            print(f"could not export {RUNNER_SCRIPT}.lock; run `uv lock --script tests/run.py`")
            return r.returncode

    if not venv_python(venv).exists():
        r = run([uv, "venv", venv, "--python", args.python, "-q"])
        if r.returncode:
            return r.returncode
    py = venv_python(venv)
    # sync, not install: the scratch venv is reused across runs, and an additive install
    # keeps packages the resolution no longer names, so the import check and the tests
    # could pass on a dependency that nothing declares any more.
    r = run([uv, "pip", "sync", "--python", py, "-q", resolved])
    if r.returncode:
        return r.returncode
    with open(work / "installed.txt", "w") as out:
        run([uv, "pip", "list", "--python", py, "--format", "freeze"], stdout=out)

    env = dict(os.environ)
    env.update({
        # The repo root makes `server` and `launcher` importable; shared-py/ holds `browserplug_shared`.
        "PYTHONPATH": os.pathsep.join([str(REPO_ROOT), str(REPO_ROOT / "shared-py")]),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONIOENCODING": "utf-8",
        "BROWSERPLUG_STATE_DIR": str(work / "state"),
        "BROWSERPLUG_LOG_FILE": str(work / "server.log"),
        # Launchers and servers the tests spawn run on the scratch venv's python, never on
        # a server environment uv would build (shared-py/browserplug_shared/server_env.py).
        "BROWSERPLUG_SERVER_PYTHON": str(py),
    })
    for name in ("BROWSERPLUG_WS_TOKEN", "BROWSERPLUG_WS_HOST", "BROWSERPLUG_MCP_PORT",
                 "VIRTUAL_ENV"):
        env.pop(name, None)

    print(f"\n== import check ({args.mode}, Python {args.python}) ==")
    r = run([py, "-c", IMPORT_CHECK], cwd=REPO_ROOT, env=env)
    import_rc = r.returncode

    print("\n== tests ==")
    targets = ["unit/", "parity/"] if args.full else SUBSET
    r = run([py, "-m", "pytest", *targets, "-q", "-p", "no:cacheprovider", *args.pytest_args],
            cwd=REPO_ROOT / "tests", env=env)
    tests_rc = r.returncode

    print(f"\ninstalled set: {work / 'installed.txt'}")
    verdict = "PASS" if import_rc == 0 and tests_rc == 0 else "FAIL"
    print(f"{verdict}: imports rc={import_rc}, tests rc={tests_rc} ({args.mode}, Python {args.python})")
    if verdict == "FAIL" and args.mode == "floors":
        print("A failure at the floors means a declared floor is too low, or a test relies on a "
              "newer release than the floor names. Read the failing test's traceback, then raise "
              "the floor in server/server.py's PEP 723 block and tests/run.py's together, and "
              "relock both (uv lock --script server/server.py; uv lock --script tests/run.py).")
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
