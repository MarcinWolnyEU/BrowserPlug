"""Import boundaries between the launcher, the shared code and the server.

Three folders, one direction of dependency:

    launcher/                          stdlib + browserplug_shared
    shared-py/browserplug_shared/      stdlib + itself
    server/                            anything, but never launcher/; its sub-packages
                                       form an acyclic graph (test at the end)

The launcher runs under whatever Python an MCP client happens to start, with none of the
server's dependencies installed, and every client spawns one. That only works while these
lists stay honest, so they are enforced on the syntax tree rather than by convention.
The launcher's lazy-import allow-list below is empty: the proxy transport, the one part
that needs the MCP SDK, runs as its own process from server/proxy/ (launched, never
imported). A new entry has to be added here on purpose.
"""

import ast
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SHARED_PKG = ROOT / "shared-py" / "browserplug_shared"
LAUNCHER_DIR = ROOT / "launcher"
SERVER_DIR = ROOT / "server"
STUB = SERVER_DIR / "launcher.py"

STDLIB = set(sys.stdlib_module_names)

# Third-party (or server) imports a launcher module may make, and only INSIDE a function.
# Empty: {"launcher/<module>.py": {"<top-level name>", ...}} is the shape of an entry.
LAUNCHER_LAZY_ALLOWED = {}
# Third-party imports the shared package may make, lazily and guarded. proc_facts reads a
# pid's owner, argv and cwd natively on Windows and Linux and falls back to psutil only where
# it has no native backend (macOS); nothing else in the package may import it.
SHARED_LAZY_ALLOWED = {
    "browserplug_shared/proc_facts.py": {"psutil"},
}


def _imports(path):
    """(top-level module name, full name, lineno, inside_function, relative) for every
    import in `path`."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []

    def walk(node, in_function):
        for child in ast.iter_child_nodes(node):
            inner = in_function or isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
            if isinstance(child, ast.Import):
                for alias in child.names:
                    found.append((alias.name.split(".")[0], alias.name, child.lineno,
                                  in_function, False))
            elif isinstance(child, ast.ImportFrom):
                if child.level:
                    found.append(("." * child.level + (child.module or ""),
                                  "." * child.level + (child.module or ""), child.lineno,
                                  in_function, True))
                else:
                    found.append((child.module.split(".")[0], child.module, child.lineno,
                                  in_function, False))
            walk(child, inner)

    walk(tree, False)
    return found


def _rel(path):
    return path.relative_to(ROOT).as_posix()


def test_shared_package_imports_only_stdlib_and_itself():
    offenders = []
    for path in sorted(SHARED_PKG.glob("*.py")):
        rel = path.relative_to(SHARED_PKG.parent).as_posix()
        for top, full, lineno, lazy, relative in _imports(path):
            if relative or top in STDLIB or top == "browserplug_shared":
                continue
            if lazy and top in SHARED_LAZY_ALLOWED.get(rel, set()):
                continue
            offenders.append(f"{rel}:{lineno} imports {full}")
    assert not offenders, "browserplug_shared must stay stdlib-only:\n" + "\n".join(offenders)


def test_launcher_imports_only_stdlib_and_browserplug_shared():
    offenders = []
    for path in sorted(LAUNCHER_DIR.glob("*.py")):
        rel = _rel(path)
        lazy_ok = LAUNCHER_LAZY_ALLOWED.get(rel, set())
        for top, full, lineno, lazy, relative in _imports(path):
            if relative:
                offenders.append(f"{rel}:{lineno} relative import {full}: the launcher "
                                 f"imports its own modules as launcher.<name>")
                continue
            if top in STDLIB or top in ("browserplug_shared", "launcher"):
                continue
            if lazy and top in lazy_ok:
                continue
            offenders.append(f"{rel}:{lineno} imports {full}"
                             + ("" if lazy else " at module level"))
    assert not offenders, "launcher/ import boundary broken:\n" + "\n".join(offenders)


def test_launcher_modules_import_no_server_code_at_module_level():
    """The relay path must not need the server checkout's dependencies, so the modules a
    relay imports have no module-level import beyond stdlib and browserplug_shared."""
    offenders = []
    for name in ("launcher.py", "stdio_relay.py", "__init__.py"):
        path = LAUNCHER_DIR / name
        for top, full, lineno, lazy, _relative in _imports(path):
            if lazy or top in STDLIB or top in ("browserplug_shared", "launcher"):
                continue
            offenders.append(f"launcher/{name}:{lineno} imports {full} at module level")
    assert not offenders, "\n".join(offenders)


def test_server_code_never_imports_the_launcher():
    launcher_names = {"launcher", "stdio_relay", "client_pid"}
    offenders = []
    for path in sorted(SERVER_DIR.rglob("*.py")):
        for top, full, lineno, _lazy, relative in _imports(path):
            bare = full.lstrip(".").split(".")[0]
            if bare in launcher_names and (relative or top == bare):
                offenders.append(f"{_rel(path)}:{lineno} imports {full}")
    assert not offenders, "server code imports launcher code:\n" + "\n".join(offenders)


def test_the_forwarding_stub_stays_tiny():
    lines = STUB.read_text(encoding="utf-8").splitlines()
    assert len(lines) < 15, (
        f"server/launcher.py is a forwarding stub and has {len(lines)} lines; the code "
        f"belongs in launcher/launcher.py")
    tree = ast.parse("\n".join(lines))
    assert not [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)], (
        "the stub defines no functions: it only hands control to launcher/launcher.py")


# The import graph is checked at run time as well: a blocking finder makes every name in
# BLOCKED fail to import, then the launcher and shared modules are imported under it. That
# is what a Python without the server's dependencies does, minus the need to have one.
_BLOCKED_PROBE = textwrap.dedent("""
    import importlib, importlib.abc, sys
    sys.path[:0] = [{root!r}, {shared!r}]
    BLOCKED = {blocked!r}

    class Blocker(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            if name.split('.')[0] in BLOCKED:
                raise ImportError('blocked in this probe: ' + name)
            return None

    sys.meta_path.insert(0, Blocker())
    for name in {modules!r}:
        importlib.import_module(name)
    loaded = sorted({{m.split('.')[0] for m in sys.modules}} & set(BLOCKED))
    server = sorted(m for m in sys.modules if m == 'server' or m.startswith('server.'))
    print(repr((loaded, server)))
""")

BLOCKED = ("fastmcp", "mcp", "mcp_types", "websockets", "psutil", "uvicorn", "starlette",
           "httpx", "httpx2", "httpcore", "httpcore2", "pydantic", "anyio", "watchfiles",
           "jsonschema")


def _run_probe(modules):
    probe = _BLOCKED_PROBE.format(root=str(ROOT), shared=str(ROOT / "shared-py"),
                                  blocked=BLOCKED, modules=modules)
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True,
                         timeout=120)
    assert out.returncode == 0, out.stderr
    return ast.literal_eval(out.stdout.strip().splitlines()[-1])


def test_launcher_modules_import_without_the_server_dependencies():
    loaded, server = _run_probe(["launcher.launcher", "launcher.stdio_relay"])
    assert loaded == [], f"the launcher imported {loaded} at module level"
    assert server == [], f"the launcher imported server modules at module level: {server}"


_PROXY_PROBE = textwrap.dedent("""
    import importlib, importlib.abc, sys
    sys.path[:0] = [{root!r}, {shared!r}]
    BLOCKED = {blocked!r}

    class NoServer(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            if name == 'server' or name.startswith('server.'):
                raise ImportError('server/ is absent in this probe: ' + name)
            if name.split('.')[0] in BLOCKED:
                raise ImportError('blocked in this probe: ' + name)
            return None

    sys.meta_path.insert(0, NoServer())
    launcher = importlib.import_module('launcher.launcher')
    command = launcher.proxy_command(sys.executable, 2830)
    launcher._proxy_spawn_kwargs()
    assert command[1] == launcher.PROXY_ENTRY
    server = sorted(m for m in sys.modules if m == 'server' or m.startswith('server.'))
    loaded = sorted({{m.split('.')[0] for m in sys.modules}} & set(BLOCKED))
    print(repr((server, loaded)))
""")


def test_launcher_proxy_path_imports_without_the_server():
    """The proxy transport's launcher half builds the child's command and spawn options with
    server/ AND the server's dependencies unimportable: the proxy runs in its own process,
    so the launcher imports nothing of it."""
    probe = _PROXY_PROBE.format(root=str(ROOT), shared=str(ROOT / "shared-py"),
                                blocked=BLOCKED)
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True,
                         timeout=120)
    assert out.returncode == 0, out.stderr
    server, loaded = ast.literal_eval(out.stdout.strip().splitlines()[-1])
    assert server == [], f"the launcher's proxy path imported server modules: {server}"
    assert loaded == [], f"the launcher's proxy path imported {loaded}"


def test_shared_modules_import_without_the_server_dependencies():
    modules = ["browserplug_shared." + p.stem for p in sorted(SHARED_PKG.glob("*.py")) if p.stem != "__init__"]
    loaded, server = _run_probe(modules)
    assert loaded == [], f"browserplug_shared imported {loaded} at module level"
    assert server == [], f"browserplug_shared imported server modules: {server}"


# The launcher's one third-party use was psutil, for "is the process answering /health this
# checkout's server?". This is the glue test for its replacement: with psutil unimportable
# the check still tells a server.py of this checkout from a same-user stranger, so a launcher
# on uv's bare interpreter adopts the real server and refuses a squatter.
_OWNERSHIP_PROBE = textwrap.dedent("""
    import importlib.abc, sys
    sys.path[:0] = [{root!r}, {shared!r}]
    BLOCKED = {blocked!r}

    class Blocker(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            if name.split('.')[0] in BLOCKED:
                raise ImportError('blocked in this probe: ' + name)
            return None

    sys.meta_path.insert(0, Blocker())
    from browserplug_shared import mcp_port
    ours, squatter = int(sys.argv[1]), int(sys.argv[2])
    print(repr((mcp_port.server_pid_is_ours(ours, {root!r}),
                mcp_port.server_pid_is_ours(squatter, {root!r}),
                'psutil' in sys.modules)))
""")


@pytest.mark.skipif(not (sys.platform == "win32" or sys.platform.startswith("linux")),
                    reason="proc_facts has a native backend on Windows and Linux only")
def test_the_ownership_check_works_with_no_psutil_anywhere():
    sleeper = "import time; time.sleep(60)"
    children = [subprocess.Popen([sys.executable, "-c", sleeper, *extra])
                for extra in ([str(SERVER_DIR / "server.py")], ["some_other_tool.py"])]
    try:
        probe = _OWNERSHIP_PROBE.format(root=str(ROOT), shared=str(ROOT / "shared-py"),
                                        blocked=BLOCKED)
        out = subprocess.run([sys.executable, "-c", probe, *(str(c.pid) for c in children)],
                             capture_output=True, text=True, timeout=120)
        assert out.returncode == 0, out.stderr
        ours, squatter, psutil_loaded = ast.literal_eval(out.stdout.strip().splitlines()[-1])
        assert ours is True, "a same-user process running this checkout's server.py is ours"
        assert squatter is False, "a same-user stranger must be refused, not trusted"
        assert psutil_loaded is False
    finally:
        for child in children:
            child.kill()
            child.wait()


# Entry points compose everything, so they sit outside the package graph: nothing imports
# them (test_only_the_entry_point_imports_server_py) and they may import any package.
SERVER_ENTRY_FILES = {"server.py", "__main__.py", "launcher.py"}


def _module_level_imports(path):
    """Dotted targets of every import that runs when `path` is imported: function bodies
    are skipped, since a lazy import cannot form an import-time cycle."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    targets = []
    stack = list(tree.body)
    while stack:
        node = stack.pop()
        if isinstance(node, ast.Import):
            targets += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            targets.append(node.module)
            # `from server import web` names a package, not an attribute of `server`.
            targets += [f"{node.module}.{a.name}" for a in node.names]
        for child in ast.iter_child_nodes(node):
            if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                stack.append(child)
    return targets


def _package_graph():
    """{package: {packages it imports at import time}} over server/'s sub-packages, with
    the non-entry top-level files (preflight.py, control.py) as the node "(top)"."""
    packages = {p.name for p in SERVER_DIR.iterdir()
                if p.is_dir() and (p / "__init__.py").exists()}
    top_level = {p.stem for p in SERVER_DIR.glob("*.py")} - {"__init__"}
    graph = {name: set() for name in packages | {"(top)"}}
    for path in sorted(SERVER_DIR.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        parts = path.relative_to(SERVER_DIR).parts
        if len(parts) == 1 and (path.name in SERVER_ENTRY_FILES or path.name == "__init__.py"):
            continue
        source = parts[0] if len(parts) > 1 else "(top)"
        for target in _module_level_imports(path):
            bits = target.split(".")
            if bits[0] != "server" or len(bits) < 2:
                continue
            dest = bits[1] if bits[1] in packages else (
                "(top)" if bits[1] in top_level and bits[1] not in
                {n.removesuffix(".py") for n in SERVER_ENTRY_FILES} else None)
            if dest and dest != source:
                graph[source].add(dest)
    return graph


def _find_cycle(graph):
    state = {}

    def visit(node, trail):
        state[node] = "open"
        for nxt in sorted(graph[node]):
            if state.get(nxt) == "open":
                return trail[trail.index(nxt):] + [nxt] if nxt in trail else [node, nxt]
            if nxt not in state:
                found = visit(nxt, trail + [nxt])
                if found:
                    return found
        state[node] = "done"
        return None

    for node in sorted(graph):
        if node not in state:
            found = visit(node, [node])
            if found:
                return found
    return None


def test_server_subpackages_have_no_import_cycle():
    graph = _package_graph()
    assert len(graph) > 5, f"the sub-package scan found too little: {sorted(graph)}"
    cycle = _find_cycle(graph)
    assert cycle is None, "import-time cycle between server sub-packages: " + " -> ".join(cycle)


def test_the_cycle_finder_sees_a_cycle():
    assert _find_cycle({"a": {"b"}, "b": {"c"}, "c": {"a"}}) == ["a", "b", "c", "a"]
    assert _find_cycle({"a": {"b"}, "b": set(), "c": {"b"}}) is None
