"""Importing a server module must not touch logging or the disk (review A5).

server.py used to configure the root logger and attach the rotating file handler when it
was imported, and server/__init__.py imported server.py. So importing any pure policy
module through the package attached a handler to the live log, and running server.py as a
script (where it is `__main__`) plus a `from server import ...` elsewhere loaded a second
copy that attached a second handler to the same file, doubling lines. Logging setup now
lives in lifecycle.setup_logging(), called by the entry points only.

Each check runs in a fresh interpreter, because an already-imported module cannot show what
its import did.
"""

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import test_imports  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]
SERVER_DIR = ROOT / "server"


def _module_name(path):
    """`server/web/http_auth.py` -> "web.http_auth"; a package's __init__ -> "web"."""
    parts = list(path.relative_to(SERVER_DIR).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)   # "" for the server package itself


# Every module of the server, sub-packages included, as a dotted name under `server`; the
# package itself is added by the tests as "__init__".
ALL_MODULES = sorted(
    name for name in (_module_name(p) for p in SERVER_DIR.rglob("*.py")
                      if "__pycache__" not in p.parts)
    if name)
# The top-level names a `from server import x` may take: entry modules and sub-packages.
TOP_LEVEL = sorted(name for name in ALL_MODULES if "." not in name)

# Imports every module in turn, in one process, and reports what each one changed.
_PACKAGE_PROBE = r"""
import importlib, json, logging, os, sys
sys.path[:0] = [{root!r}, {shared!r}]
root = logging.getLogger()
report = {{}}
for name in {modules!r}:
    before = list(root.handlers)
    level = root.level
    try:
        importlib.import_module("server." + name if name != "__init__" else "server")
    except BaseException as e:            # a failed import is a failure of its own
        report[name] = "import failed: %r" % (e,)
        continue
    added = [type(h).__name__ for h in root.handlers if h not in before]
    problems = []
    if added:
        problems.append("added root handlers " + repr(added))
    if root.level != level:
        problems.append("changed the root level")
    report[name] = problems
print(json.dumps(report))
"""

# `python server/server.py` runs the file as a script: server/ is first on sys.path and the
# bootstrap at the top of server.py swaps it for the repo root. run_path under another
# run_name executes that module body (bootstrap and every import) without calling main().
_SCRIPT_PROBE = r"""
import importlib, json, logging, runpy, sys
sys.path[:0] = [{server_dir!r}]
root = logging.getLogger()
report = {{}}
before = list(root.handlers)
runpy.run_path({script!r}, run_name="server_script")
report["server.py"] = [type(h).__name__ for h in root.handlers if h not in before]
for name in ("server.runtime.lifecycle", "server.reporting.net_addresses",
             "server.reporting.wan_diagnosis", "server.web.http_endpoints"):
    before = list(root.handlers)
    importlib.import_module(name)
    report[name] = [type(h).__name__ for h in root.handlers if h not in before]
print(json.dumps(report))
"""


def _run(probe, tmp_path):
    env = dict(os.environ)
    env.pop("BROWSERPLUG_LOG_FILE", None)          # unset: the default is the LIVE log
    env.pop("BROWSERPLUG_DETACHED", None)
    env["BROWSERPLUG_STATE_DIR"] = str(tmp_path / "state")
    work = tmp_path / "cwd"
    work.mkdir()
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True,
                         env=env, cwd=str(work), timeout=180)
    assert out.returncode == 0, out.stderr
    assert not list(work.iterdir()), f"an import created files in the cwd: {list(work.iterdir())}"
    assert not (tmp_path / "state").exists() or not list((tmp_path / "state").iterdir()), (
        "an import wrote into the state directory")
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_importing_any_server_module_leaves_the_root_logger_alone(tmp_path):
    names = ["__init__"] + ALL_MODULES
    probe = _PACKAGE_PROBE.format(root=str(ROOT), shared=str(ROOT / "shared-py"), modules=names)
    report = _run(probe, tmp_path)
    offenders = {name: problem for name, problem in report.items() if problem}
    assert not offenders, f"imports with side effects on logging: {offenders}"
    assert set(report) == set(names)


def test_running_server_py_as_a_script_does_not_attach_a_handler(tmp_path):
    report = _run(_SCRIPT_PROBE.format(server_dir=str(SERVER_DIR),
                                       script=str(SERVER_DIR / "server.py")), tmp_path)
    assert report == {name: [] for name in report}, report


def test_the_package_init_imports_nothing():
    tree = ast.parse((SERVER_DIR / "__init__.py").read_text(encoding="utf-8"))
    assert not [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]


def _imports_server_py(tree):
    """Names of imports in `tree` that load server.py (not just another module of the
    server package)."""
    siblings = set(TOP_LEVEL) - {"server"}
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names = {a.name for a in node.names}
            if node.level >= 1 and (node.module == "server" or (
                    node.module is None and "server" in names)):
                found.append(ast.dump(node))
            elif node.level == 0 and node.module == "server.server":
                found.append(ast.dump(node))
            elif node.level == 0 and node.module == "server" and (names - siblings):
                found.append(ast.dump(node))
        elif isinstance(node, ast.Import):
            if any(a.name in ("server", "server.server") for a in node.names):
                found.append(ast.dump(node))
    return found


def test_only_the_entry_point_imports_server_py():
    """Dependency direction: server.py composes the other modules and none of them may
    reach back for it (that is how wan_diagnosis once loaded a second copy of it)."""
    offenders = {}
    for path in sorted(SERVER_DIR.rglob("*.py")):
        if path.name in ("server.py", "__main__.py"):
            continue
        hits = _imports_server_py(ast.parse(path.read_text(encoding="utf-8")))
        if hits:
            offenders[path.name] = hits
    assert not offenders, f"modules importing server.py: {offenders}"
