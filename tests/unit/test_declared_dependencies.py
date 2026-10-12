"""The declared dependencies: the PEP 723 blocks, their locks, and what is installed.

Where they are declared:

* server/server.py: the server's dependencies, in its PEP 723 block, locked in
  server/server.py.lock (`uv lock --script server/server.py`).
* tests/run.py: the suite's environment. Every server dependency with the SAME specifier,
  plus the test tools, locked in tests/run.py.lock. The suite runs on what the server runs.
* server/launcher.py and launcher/launcher.py: psutil on macOS only, with the server's
  specifier. Elsewhere the launcher reads who answers /health with the standard library
  (shared-py/browserplug_shared/proc_facts.py), so its block installs nothing there.

What this file does: parse every block, pin the invariants between them, check both locks
against their blocks and against each other, ask uv whether each lock is current, compare
the installed versions with the declared ranges, check the mcp ceiling, and check that
every third-party module the server, launcher or shared code imports is declared (and every
declared one imported).

What it cannot do: notice a floor that is too low. The locks hold the newest releases, and
the newest release satisfies any floor, so a stale `websockets>=12.0` passes every test
here. That check needs a second environment built from the LOWEST allowed versions, which
`tests/check_dependency_floors.py` does (`uv pip compile --resolution lowest-direct` into a
scratch venv outside the repo, then an import check and a unit subset). Run it after
touching a floor, a ceiling or a lock; .tmp/dev-docs/dependencies.md has the procedure.

Two rots this file catches, both of which the repo has carried:

* A declaration that drifts away from the environment, or a declared Python floor that
  the dependencies outgrew: websockets 17 moved its own floor to 3.11 under a README that
  still said 3.10+, and nothing said so until a newcomer installed.

* A test dependency that is declared, absent, and silently routed around. pillow is
  imported inside a try/except that turns a missing install into a `pytest.skip`, so
  losing it costs coverage without costing a single red test, the lying-skip shape
  AGENTS.md calls out. Asserting it imports turns that into a failure in the unit loop
  instead of a quiet 's' in a 37-minute sweep.
"""

import ast
import importlib.metadata
import importlib.util
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement          # pytest depends on packaging
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import Version

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVER_SCRIPT = REPO_ROOT / "server" / "server.py"
RUNNER_SCRIPT = REPO_ROOT / "tests" / "run.py"
LAUNCHER_SCRIPTS = (REPO_ROOT / "server" / "launcher.py", REPO_ROOT / "launcher" / "launcher.py")
SERVER_LOCK = REPO_ROOT / "server" / "server.py.lock"
RUNNER_LOCK = REPO_ROOT / "tests" / "run.py.lock"
# Stdlib-only scripts: a block with requires-python and no dependencies, so `uv run --script`
# runs them anywhere. render.py declares pillow, the runner's specifier.
STDLIB_SCRIPTS = ("build_xpi.py", "build_source_zip.py", "build_server_zip.py", "gen_ws_token.py",
                  "patch_defuddle.py",
                  "server/control.py", "server/preflight.py", "tests/parity/sync_common.py",
                  "tests/check_dependency_floors.py")
SOURCE_TREES = ("server", "launcher", "shared-py")
FIRST_PARTY = {"server", "launcher", "browserplug_shared"}
# Imported today and not declared, because they are on their way out. Each name here must
# still be imported somewhere (test_the_pending_removals_are_still_pending), so the set is
# emptied as the imports go rather than lingering. Empty since the last pending one went
# with the launcher's old proxy (the proxy transport is server/proxy/ now).
PENDING_REMOVAL = set()

# PEP 723's reference regex.
_BLOCK = re.compile(r"(?m)^# /// (?P<type>[a-zA-Z0-9-]+)$\s(?P<content>(^#(| .*)$\s)+)^# ///$")


def script_metadata(path):
    """The parsed PEP 723 `script` block of `path`."""
    text = Path(path).read_text(encoding="utf-8")
    blocks = [m for m in _BLOCK.finditer(text) if m.group("type") == "script"]
    assert len(blocks) == 1, f"{path} must carry exactly one PEP 723 script block"
    lines = blocks[0].group("content").splitlines(keepends=True)
    return tomllib.loads("".join(ln[2:] if ln.startswith("# ") else ln[1:] for ln in lines))


def declared(path):
    return [Requirement(d) for d in script_metadata(path).get("dependencies", [])]


def by_name(requirements):
    return {canonicalize_name(r.name): r for r in requirements}


SERVER_DEPS = declared(SERVER_SCRIPT)
RUNNER_DEPS = declared(RUNNER_SCRIPT)
RUNNER_ONLY = [r for r in RUNNER_DEPS if canonicalize_name(r.name) not in by_name(SERVER_DEPS)]


def locked(path):
    """{canonical name: Version} of every package a uv script lock pins."""
    data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    return {canonicalize_name(p["name"]): Version(p["version"])
            for p in data.get("package", []) if "version" in p}


def _installed_version(dist_name):
    try:
        return importlib.metadata.version(dist_name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _ids(requirements):
    return [r.name for r in requirements]


# ---- the blocks and the invariants between them -------------------------------------------

@pytest.mark.parametrize("script", [str(SERVER_SCRIPT), str(RUNNER_SCRIPT),
                                    *map(str, LAUNCHER_SCRIPTS),
                                    *(str(REPO_ROOT / s) for s in STDLIB_SCRIPTS),
                                    str(REPO_ROOT / "assets" / "icons" / "render.py")],
                         ids=lambda p: Path(p).relative_to(REPO_ROOT).as_posix())
def test_every_script_block_parses_and_names_its_python(script):
    meta = script_metadata(script)
    assert SpecifierSet(meta["requires-python"]), f"{script}: empty requires-python"
    assert isinstance(meta.get("dependencies"), list), f"{script}: no dependencies list"
    for dep in meta["dependencies"]:
        Requirement(dep)


@pytest.mark.parametrize("script", STDLIB_SCRIPTS)
def test_the_stdlib_scripts_declare_nothing(script):
    assert script_metadata(REPO_ROOT / script)["dependencies"] == []


def test_the_server_and_the_suite_run_on_the_same_python():
    """One tested interpreter line; it also stops uv's discovery from picking another."""
    server = script_metadata(SERVER_SCRIPT)["requires-python"]
    assert server == script_metadata(RUNNER_SCRIPT)["requires-python"] == ">=3.11,<3.13"


def test_the_suite_declares_every_server_dependency_with_the_identical_specifier():
    runner = by_name(RUNNER_DEPS)
    for req in SERVER_DEPS:
        key = canonicalize_name(req.name)
        assert key in runner, f"tests/run.py does not declare the server dependency {req}"
        assert str(runner[key]) == str(req), (
            f"tests/run.py declares {runner[key]} but server/server.py declares {req}: the "
            f"suite would run on something the server does not")


def test_the_launcher_declares_psutil_for_macos_only_with_the_servers_specifier():
    """proc_facts reads the /health owner natively on Windows and Linux, so the launcher
    runs on a bare interpreter there and its block installs nothing. macOS has no native
    probe: a `uv run --script` registration there gets psutil, with the server's specifier,
    or the launcher cannot tell its own server from a /health squatter."""
    psutil = by_name(SERVER_DEPS)["psutil"]
    blocks = [script_metadata(script) for script in LAUNCHER_SCRIPTS]
    assert blocks[0]["dependencies"] == blocks[1]["dependencies"]
    for script, meta in zip(LAUNCHER_SCRIPTS, blocks):
        [req] = declared(script)
        assert canonicalize_name(req.name) == "psutil", script
        assert req.specifier == psutil.specifier, script
        assert str(req.marker) == 'sys_platform == "darwin"', script
        for platform in ("win32", "linux"):
            assert not req.marker.evaluate({"sys_platform": platform}), (script, platform)
        assert SpecifierSet(meta["requires-python"]) == SpecifierSet(">=3.10"), script


def test_render_declares_pillow_with_the_suites_specifier():
    assert [str(r) for r in declared(REPO_ROOT / "assets" / "icons" / "render.py")] == \
        [str(by_name(RUNNER_DEPS)["pillow"])]


def test_mcp_keeps_a_major_version_ceiling():
    """A clean install must not resolve an mcp major the suite has never run against.

    The server is built on the SDK's low-level API (server/tools/mcp_server.py): a major
    can move it, and the install would still succeed. The ceiling must sit no higher than
    the next major above the floor.
    """
    spec = by_name(SERVER_DEPS)["mcp"].specifier
    floors = [Version(s.version) for s in spec if s.operator == ">="]
    ceilings = [Version(s.version) for s in spec if s.operator == "<"]
    assert floors and ceilings, f"mcp is declared '{spec}'; it needs a >= floor and a < ceiling"
    assert min(floors) >= Version("2.2.0"), f"mcp is declared '{spec}'; the floor is 2.2.0"
    next_major = Version(str(max(floors).major + 1))
    assert min(ceilings) <= next_major, (
        f"mcp is declared '{spec}': the ceiling must be <{next_major} or tighter")


# ---- the locks -------------------------------------------------------------------------------

@pytest.mark.parametrize("script,lock,deps", [
    (SERVER_SCRIPT, SERVER_LOCK, SERVER_DEPS), (RUNNER_SCRIPT, RUNNER_LOCK, RUNNER_DEPS)],
    ids=["server", "runner"])
def test_each_lock_pins_a_stable_release_inside_each_declared_range(script, lock, deps):
    """A prerelease pin is a decision, not an accident of what the resolver saw that day."""
    pins = locked(lock)
    for req in deps:
        key = canonicalize_name(req.name)
        assert key in pins, f"{req.name} is declared in {script.name} but not pinned in {lock.name}"
        assert req.specifier.contains(pins[key], prereleases=True), (
            f"{lock.name} pins {req.name}=={pins[key]}, outside the declared '{req}'; "
            f"run `uv lock --script {script.relative_to(REPO_ROOT).as_posix()}`")
        assert not pins[key].is_prerelease, f"{lock.name} pins the prerelease {req.name}=={pins[key]}"


def test_both_locks_agree_on_every_shared_package():
    """The suite must test the exact versions the server runs, transitive ones included."""
    server, runner = locked(SERVER_LOCK), locked(RUNNER_LOCK)
    missing = sorted(set(server) - set(runner))
    assert not missing, f"tests/run.py.lock lacks server packages: {missing}"
    differ = {n: (str(server[n]), str(runner[n])) for n in server if server[n] != runner[n]}
    assert not differ, (f"the locks disagree (server, runner): {differ}; relock both with "
                        f"`uv lock --script server/server.py` and `uv lock --script tests/run.py`")


def test_no_lock_carries_fastmcp():
    for lock in (SERVER_LOCK, RUNNER_LOCK):
        assert "fastmcp" not in lock.read_text(encoding="utf-8").lower(), lock


@pytest.mark.parametrize("script", [SERVER_SCRIPT, RUNNER_SCRIPT], ids=["server", "runner"])
def test_the_lock_is_current(script):
    """uv's own answer. Runtime spawns pass --locked, so a stale lock is a failed spawn."""
    from browserplug_shared.server_env import find_uv
    uv = find_uv()
    assert uv, "uv is required (it builds the server's environment) and was not found"
    rel = script.relative_to(REPO_ROOT).as_posix()
    proc = subprocess.run([uv, "lock", "--check", "--script", rel], cwd=REPO_ROOT,
                          capture_output=True, text=True, timeout=120,
                          stdin=subprocess.DEVNULL)
    assert proc.returncode == 0, (f"{rel}.lock is stale: run `uv lock --script {rel}`\n"
                                  f"{proc.stderr}")


# ---- what is installed -------------------------------------------------------------------------

@pytest.mark.parametrize("requirement", SERVER_DEPS, ids=_ids(SERVER_DEPS))
def test_installed_server_dependency_satisfies_its_declared_range(requirement):
    """server/server.py's block is what a fresh environment resolves; it must describe THIS."""
    installed = _installed_version(requirement.name)
    assert installed is not None, (
        f"{requirement.name} is declared in server/server.py but is not installed here; run "
        f"the suite with `uv run --locked --script tests/run.py`")
    assert requirement.specifier.contains(Version(installed), prereleases=True), (
        f"installed {requirement.name}=={installed} does not satisfy the declared "
        f"'{requirement}'. Either the declaration is stale or the environment is.")


@pytest.mark.parametrize("requirement", RUNNER_ONLY, ids=_ids(RUNNER_ONLY))
def test_installed_test_dependency_satisfies_its_declared_range(requirement):
    installed = _installed_version(requirement.name)
    assert installed is not None, (
        f"{requirement.name} is declared in tests/run.py but is not installed here; run "
        f"the suite with `uv run --locked --script tests/run.py`")
    assert requirement.specifier.contains(Version(installed), prereleases=True), (
        f"installed {requirement.name}=={installed} does not satisfy the declared "
        f"'{requirement}'.")


@pytest.mark.parametrize("module, declared_as, used_by", [
    ("PIL", "pillow", "tests/integration/test_agent_experience.py"),
])
def test_optional_looking_test_dependency_is_actually_present(module, declared_as, used_by):
    """Each of these is imported behind a try/except that degrades to a skip.

    The skip is the hazard: with the package missing, the tests in `used_by` report a
    bare 's' and the sweep still exits 0, so a whole class of assertions can disappear
    without anything going red. Nothing here needs the package's API — presence is the
    entire claim.
    """
    assert importlib.util.find_spec(module) is not None, (
        f"{declared_as} is declared in tests/run.py but not importable, so {used_by} will "
        f"SKIP rather than fail. Run the suite with `uv run --locked --script tests/run.py`")


def _declared_python_floors():
    """(package, Requires-Python floor) for every installed runtime dependency."""
    out = {}
    for req in SERVER_DEPS:
        try:
            spec = importlib.metadata.metadata(req.name).get("Requires-Python") or ""
        except Exception:
            continue
        found = re.findall(r">=\s*(\d+)\.(\d+)", spec)
        if found:
            out[req.name] = max((int(a), int(b)) for a, b in found)
    return out


def test_the_advertised_python_floor_covers_every_runtime_dependency():
    """README and package.json promise a Python version; the deps have to fit under it.

    websockets 17 raised its own floor to >=3.11 while everything else still accepts 3.10,
    so this is not hypothetical: the upgrade moved the real minimum out from under a README
    that still said 3.10+. That failure is invisible here and lands on whoever installs
    next, on a machine we cannot see.
    """
    floors = _declared_python_floors()
    assert floors, "could not read Requires-Python for any declared runtime dependency"
    required = max(floors.values())
    strictest = [n for n, v in floors.items() if v == required]

    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    stated = re.search(r"\*\*Python (\d+)\.(\d+)\+\*\*", readme)
    assert stated, "README no longer states a **Python X.Y+** prerequisite"
    readme_floor = (int(stated.group(1)), int(stated.group(2)))
    assert readme_floor >= required, (
        f"README promises Python {readme_floor[0]}.{readme_floor[1]}+ but "
        f"{', '.join(strictest)} needs >={required[0]}.{required[1]}")

    package = json.loads((REPO_ROOT / "package.json").read_text(encoding="utf-8"))
    engine = package.get("engines", {}).get("python", "")
    found = re.search(r">=\s*(\d+)\.(\d+)", engine)
    assert found, f"package.json engines.python is not a >=X.Y range: {engine!r}"
    engine_floor = (int(found.group(1)), int(found.group(2)))
    assert engine_floor >= required, (
        f"package.json engines.python is {engine!r} but "
        f"{', '.join(strictest)} needs >={required[0]}.{required[1]}")
    assert engine_floor == readme_floor, (
        f"README says {readme_floor} and package.json says {engine_floor} — one number, "
        f"two files, and a newcomer reads whichever they find first")
    block = SpecifierSet(script_metadata(SERVER_SCRIPT)["requires-python"])
    assert Version(f"{readme_floor[0]}.{readme_floor[1]}") in block, (
        f"README's Python {readme_floor[0]}.{readme_floor[1]}+ is outside server/server.py's "
        f"requires-python '{block}'")


# ---- declared vs imported -----------------------------------------------------------------

def _third_party_imports():
    """{top-level module: [where]} for every non-stdlib, non-project import in the trees."""
    found = {}
    stdlib = set(sys.stdlib_module_names)
    for tree in SOURCE_TREES:
        for path in sorted((REPO_ROOT / tree).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    names = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    names = [node.module.split(".")[0]]
                else:
                    continue
                for name in names:
                    if name not in stdlib and name not in FIRST_PARTY and name != "__future__":
                        found.setdefault(name, []).append(
                            f"{path.relative_to(REPO_ROOT).as_posix()}:{node.lineno}")
    return found


def _distribution_of(module):
    dists = importlib.metadata.packages_distributions().get(module) or []
    return {canonicalize_name(d) for d in dists}


def test_every_third_party_import_is_declared():
    """A module the server imports only because something else happens to pull it in breaks
    the day that something drops it. Declare it in server/server.py (and tests/run.py)."""
    declared_names = set(by_name(SERVER_DEPS))
    offenders = []
    for module, where in sorted(_third_party_imports().items()):
        if module in PENDING_REMOVAL:
            continue
        dists = _distribution_of(module)
        if not dists & declared_names:
            offenders.append(f"{module} ({', '.join(sorted(dists)) or 'not installed'}) "
                             f"imported at {', '.join(where[:3])}")
    assert not offenders, "imported but not declared in server/server.py:\n" + "\n".join(offenders)


def test_every_declared_server_dependency_is_imported_directly():
    """The reverse: a declaration nothing imports is a floor nobody tests."""
    imported = set()
    for module in _third_party_imports():
        imported |= _distribution_of(module)
    unused = sorted(str(r) for r in SERVER_DEPS if canonicalize_name(r.name) not in imported)
    assert not unused, f"declared in server/server.py but imported nowhere: {unused}"


def test_the_pending_removals_are_still_pending():
    """Once nothing imports a PENDING_REMOVAL name, it leaves the set (and this file)."""
    imports = _third_party_imports()
    gone = sorted(PENDING_REMOVAL - set(imports))
    assert not gone, f"no longer imported anywhere: {gone}; remove them from PENDING_REMOVAL"


def test_what_preflight_and_server_env_probe_is_declared():
    from browserplug_shared import server_env
    from server import preflight
    assert set(preflight.REQUIRED_PACKAGES) == set(server_env.PROBE_IMPORTS)
    declared_names = set(by_name(SERVER_DEPS))
    for module in server_env.PROBE_IMPORTS:
        assert _distribution_of(module) & declared_names, module
