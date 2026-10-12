"""
Parsing / filesystem helpers for the gecko<->chromium parity validator (design.md D14).

Stdlib only (tomllib, re, hashlib, pathlib, json) — the parity suite runs with NO browser
and no third-party deps. All extraction is text/JSON based; every extractor floor-guards
against a vacuous parse (an empty match is a bug, not a pass) at its call site in
test_parity.py.

Layout: this file and test_parity.py live in tests/parity/. The repo root is two levels up.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import tomllib
from pathlib import Path

PARITY_DIR = Path(__file__).resolve().parent
TESTS_DIR = PARITY_DIR.parent
ROOT = TESTS_DIR.parent
REGISTRY_PATH = PARITY_DIR / "parity.toml"

JS_GECKO_DIR = TESTS_DIR / "js"
JS_CHROMIUM_DIR = TESTS_DIR / "js-chromium"
INTEGRATION_DIR = TESTS_DIR / "integration"


# --------------------------------------------------------------------------- registry / fs

def load_registry() -> dict:
    with open(REGISTRY_PATH, "rb") as fh:
        return tomllib.load(fh)


def tree_root() -> Path:
    """Base dir the extension trees are resolved under.

    Normally the repo ROOT. `BROWSERPLUG_PARITY_TREE_ROOT` overrides it so the validator
    can be pointed at COPIES of the two trees — used by the bite-drill to prove
    invariants A/C actually fail on a deliberately-broken copy without touching the
    real trees. Unset in every normal run.
    """
    override = os.environ.get("BROWSERPLUG_PARITY_TREE_ROOT")
    return Path(override) if override else ROOT


def tree_dir(registry: dict, tree: str) -> Path:
    return tree_root() / registry["trees"][tree]


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def list_tree_files(tree_root: Path) -> set[str]:
    """All files under a tree, as tree-relative POSIX relpaths."""
    out = set()
    for p in tree_root.rglob("*"):
        if p.is_file():
            out.add(p.relative_to(tree_root).as_posix())
    return out


def expand_shared(registry: dict, tree_root: Path) -> set[str]:
    """The concrete shared-file set: explicit files + expanded globs, POSIX relpaths."""
    shared = set(registry["shared"].get("files", []))
    for pattern in registry["shared"].get("globs", []):
        for p in tree_root.glob(pattern):
            if p.is_file():
                shared.add(p.relative_to(tree_root).as_posix())
    return shared


def expand_globs(registry: dict, section: str, tree_root: Path) -> dict[str, set[str]]:
    """{glob pattern: matching relpaths} for a [*_only_globs] section, POSIX relpaths.

    Empty per pattern rather than merged, so invariant C can say WHICH pattern matched
    nothing: a pattern gone stale after a rename is a whole asset tree left unclassified,
    and a silently-empty glob would let it pass.
    """
    out: dict[str, set[str]] = {}
    for pattern in registry.get(section, {}):
        out[pattern] = {
            p.relative_to(tree_root).as_posix()
            for p in tree_root.glob(pattern) if p.is_file()
        }
    return out


# --------------------------------------------------------------------------- background.js

# Single-quoted, dotted case labels: the WS action strings (area.verb). The verb may be
# camelCase (`tabs.captureVisibleTab`); a lowercase-only verb pattern silently skipped it.
_ACTION_RE = re.compile(r"case\s+'([a-z][a-z_]*\.[A-Za-z_]+)'")


def parse_action_strings(bg_src: str) -> set[str]:
    return set(_ACTION_RE.findall(bg_src))


# --------------------------------------------------------------------------- automation.js

_INVOKE_START = "self.invoke = async function"
_INVOKE_END = "window.__browserplugAuto = self;"
_AUTO_CASE_RE = re.compile(r"case\s+'([a-z_]+)'")


def parse_automation_methods(auto_src: str) -> set[str]:
    """The method names in the __browserplugAuto self.invoke dispatch switch."""
    start = auto_src.find(_INVOKE_START)
    end = auto_src.find(_INVOKE_END, start)
    if start < 0 or end <= start:
        return set()  # caller floor-guards; empty => the invoke registry moved
    return set(_AUTO_CASE_RE.findall(auto_src[start:end]))


# --------------------------------------------------------------------------- manifest.json

def parse_manifest(tree_root: Path) -> dict:
    return json.loads((tree_root / "manifest.json").read_text(encoding="utf-8"))


_PY_VERSION_RE = re.compile(r'^__version__\s*=\s*["\']([^"\']+)["\']', re.M)


def parse_python_version() -> str:
    """__version__ out of shared-py/browserplug_shared/version.py — the server's AND the launcher's number.

    Read as TEXT, not imported: the parity suite is stdlib-only and must not drag in
    server/ (nor care whether its dependencies are installed). Floor-guarded: a file that
    stopped declaring __version__ fails loudly instead of matching nothing.
    """
    path = ROOT / "shared-py" / "browserplug_shared" / "version.py"
    m = _PY_VERSION_RE.search(path.read_text(encoding="utf-8"))
    assert m, (f"No __version__ = \"...\" in {path} — it was renamed or reshaped. Invariant H "
               f"reads it as text; re-point this helper, do not hard-code a version.")
    return m.group(1)


def parse_package_json_version() -> str:
    """"version" out of the repo-root package.json, the fourth carrier of the shared number.

    Nothing reads it at runtime (npm never publishes this package), which is exactly why it
    drifted to 1.1.0 while the three enforced carriers moved on to 1.9.x.
    """
    path = ROOT / "package.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    version = data.get("version")
    assert isinstance(version, str) and version, f"{path} has no string \"version\" field."
    return version


def manifest_permissions(manifest: dict) -> set[str]:
    """permissions + optional_permissions (gecko carries proxy in optional)."""
    return set(manifest.get("permissions", [])) | set(manifest.get("optional_permissions", []))


# --------------------------------------------------------------------------- JS test parsing

# `<VAR> = [fs.]readFileSync(...'.../extension-<tree>/<relpath>'...)` — tolerant of BOTH read
# idioms in this codebase: the T4 slicers use `fs.readFileSync(new URL('../../extension-.../x',
# import.meta.url))`; the T5 UI tests use `readFileSync(path.join(__dirname, '../../extension-
# .../x'))`. \s* spans the newline the reads wrap across; the leading `../` bits are consumed
# non-greedily before the extension-<tree>/<relpath> capture.
_EXT_READ_RE = re.compile(
    r"(\w+)\s*=\s*(?:fs\.)?readFileSync\(\s*"
    r"(?:new URL\(\s*|path\.join\([^'\"]*,\s*)?"
    r"['\"][^'\"]*?(extension-[a-z]+)/([^'\"]+)['\"]"
)

# A string literal that stops at its own matching (possibly different type) quote.
_STR = r"(['\"])((?:\\.|(?!\2).)*)\2"
# receiver.indexOf('literal' ...) — group1 receiver, group3 literal content.
_INDEXOF_RE = re.compile(r"(\w+)\.indexOf\(\s*" + _STR)
# A bare slice('start', 'end', ...) helper call (not `.slice(`): first TWO string args.
_SLICE_RE = re.compile(
    r"(?<![.\w])slice\(\s*(['\"])((?:\\.|(?!\1).)*)\1\s*,\s*(['\"])((?:\\.|(?!\3).)*)\3"
)


def parse_ext_reads(test_src: str) -> list[tuple[str, str, str]]:
    """[(var, tree_dir, tree_relpath)] for each extension-tree file the test readFileSync's."""
    return [(m.group(1), m.group(2), m.group(3)) for m in _EXT_READ_RE.finditer(test_src)]


def _js_unescape(s: str) -> str:
    """Decode the JS string escapes that appear in anchors (e.g. \\' -> ', \\\\ -> \\).

    A captured literal keeps its source backslashes (the regex matches `\\'` as two chars),
    but the anchor must equal the DECODED value to be found in the extension source — the
    comment on disk contains a bare apostrophe, not a backslash-apostrophe.
    """
    return re.sub(
        r"\\(.)",
        lambda m: {"n": "\n", "t": "\t", "r": "\r"}.get(m.group(1), m.group(1)),
        s,
    )


def parse_anchors(test_src: str, source_vars: set[str]) -> set[str]:
    """Slice-anchor string literals the test depends on.

    Two forms are collected (design test-infra census §3, technique T4):
      - `<sourceVar>.indexOf('anchor')`  (receiver must be a readFileSync'd source var)
      - a bare `slice('start','end', ...)` helper's first two string args
    The `what`/description third arg of slice() and any `.indexOf` on a non-source string
    are deliberately excluded so the set contains only real source anchors.
    """
    anchors: set[str] = set()
    for m in _INDEXOF_RE.finditer(test_src):
        if m.group(1) in source_vars:
            anchors.add(_js_unescape(m.group(3)))
    for m in _SLICE_RE.finditer(test_src):
        anchors.add(_js_unescape(m.group(2)))
        anchors.add(_js_unescape(m.group(4)))
    return anchors


def list_js_tests(directory: Path) -> set[str]:
    return {p.name for p in directory.glob("*.test.mjs")}


# --------------------------------------------------------------------------- integration

def list_integration_modules() -> set[str]:
    return {p.name for p in INTEGRATION_DIR.glob("test_*.py")}


def module_uses_swe(module_src: str) -> bool:
    """True if the module references server_with_extension (directly or as a fixture arg).

    This mirrors what conftest.pytest_collection_modifyitems keys on: the browser param only
    exists on items whose fixture graph resolves to server_with_extension, and that fixture
    is named literally wherever it enters a module (a test arg, or a chained fixture's arg
    such as `full_mcp_system(self, server_with_extension)`).
    """
    return "server_with_extension" in module_src


_MODULE_MARKER_RE = re.compile(
    r"pytestmark\s*=\s*pytest\.mark\.(firefox_only|chromium_only)\("
)
# Any firefox_only/chromium_only marker call, module-level or per-method, with its args.
_ANY_MARKER_RE = re.compile(
    r"pytest\.mark\.(firefox_only|chromium_only)\(\s*([^)]*)", re.S
)


def module_level_engine_marker(module_src: str):
    """Return 'firefox' / 'chromium' if the module carries a module-level engine marker."""
    m = _MODULE_MARKER_RE.search(module_src)
    if not m:
        return None
    return "firefox" if m.group(1) == "firefox_only" else "chromium"


def engine_marker_calls(module_src: str) -> list[tuple[str, bool]]:
    """[(marker_name, has_reason_string)] for every engine-only marker call in the module."""
    out = []
    for m in _ANY_MARKER_RE.finditer(module_src):
        args = m.group(2)
        has_reason = bool(re.search(r"""['\"]\s*\S""", args))
        out.append((m.group(1), has_reason))
    return out


# A test that BUILDS its own browser is pinned to that engine even though it carries no
# firefox_only/chromium_only marker — the marker path is what conftest deselects on, so this
# family is invisible to both pytest's deselect count AND the module-level check below it.
_MANAGER_ENGINE = {"FirefoxTestManager": "firefox", "ChromiumTestManager": "chromium"}

_SWE = "server_with_extension"


def _def_nodes(tree: ast.Module):
    """Every (async) def in the module, module-level or nested in a class, by name.

    Later definitions win, mirroring Python. Names are unqualified because that is how a
    pytest fixture is requested (by bare name) regardless of the class it is defined in.
    """
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[node.name] = node
    return out


def _arg_names(node) -> list[str]:
    a = node.args
    return [x.arg for x in (a.posonlyargs + a.args + a.kwonlyargs) if x.arg != "self"]


def _constructs_manager(node) -> str | None:
    """The engine of a <Engine>TestManager CONSTRUCTED in this def's body, if any.

    AST-based on purpose: a bare grep also matches the manager's name inside an f-string
    error message (test_multi_browser.py does exactly that), which is not a construction.
    """
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        fn = sub.func
        name = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else None
        if name in _MANAGER_ENGINE:
            return _MANAGER_ENGINE[name]
    return None


def _reaches_swe(name: str, defs: dict, seen: frozenset = frozenset()) -> bool:
    """True if `name` is a module-local def whose fixture graph reaches server_with_extension."""
    if name == _SWE:
        return True
    node = defs.get(name)
    if node is None or name in seen:
        return False
    seen = seen | {name}
    return any(_reaches_swe(a, defs, seen) for a in _arg_names(node))


def inline_engine_bound_tests(module_src: str) -> dict[str, str]:
    """{test_name: engine} for tests pinned to ONE engine by CONSTRUCTION, not by marker.

    A test is engine-bound when it (or a module-local fixture it requests) builds a
    <Engine>TestManager itself AND its fixture graph never reaches the parametrized
    `server_with_extension`. Such a test silently runs on one engine only: it is not
    deselected (no marker) and its module can still satisfy the module-level fixture check
    via its OTHER tests, so nothing else in invariant G can see it.

    A test that takes the parametrized fixture and ALSO builds a second browser on purpose
    (test_multi_browser.py) is correctly not reported — it runs under both engines.
    """
    defs = _def_nodes(ast.parse(module_src))
    engine_of = {n: _constructs_manager(node) for n, node in defs.items()}

    out = {}
    for name, node in defs.items():
        if not name.startswith("test_"):
            continue
        if _reaches_swe(name, defs):
            continue
        engine = engine_of.get(name)
        if engine is None:
            for arg in _arg_names(node):
                if engine_of.get(arg):
                    engine = engine_of[arg]
                    break
        if engine:
            out[name] = engine
    return out


def slice_block(src: str, start: str, end: str) -> str:
    """The source between two anchors, start INCLUSIVE / end EXCLUSIVE.

    Same slicing shape the JS twins use (indexOf .. indexOf), so a block that this
    validator compares is literally the block those tests drive.
    """
    a = src.find(start)
    if a < 0:
        return ""
    b = src.find(end, a)
    if b < 0:
        return ""
    return src[a:b]


def code_lines(block: str) -> list[str]:
    """A block reduced to its CODE: blank lines and whole-line // comments dropped,
    trailing whitespace stripped.

    Whole-line comments only, deliberately. Stripping trailing `// ...` too would mean
    parsing JS well enough to know a `//` is not inside a string or a regex — and this
    file is full of both (`/^file:\\/\\//i`, `'https://...'`). Getting that wrong would
    make two DIFFERENT blocks compare equal, which is the one failure mode a parity
    validator must not have. The cost is that an engine-specific note has to live on its
    own comment line rather than trailing a statement.
    """
    out = []
    for line in block.split("\n"):
        stripped = line.strip()
        if not stripped or stripped.startswith("//"):
            continue
        out.append(line.rstrip())
    return out


# --------------------------------------------------------------------------- server-sent actions

SERVER_DIR = ROOT / "server"
_WIRE_ACTION_SHAPE = re.compile(r"^[a-z_]+\.[A-Za-z_]+$")


def _const_str(node: ast.AST) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def parse_server_sent_actions(server_dir: Path | None = None) -> dict[str, list[str]]:
    """{wire action: ["file:line", ...]} for every literal action the server sends to an extension.

    Built with `ast`, so prose in docstrings and comments never counts. Four shapes are
    recognised, and each is a place a misspelt action would otherwise ride along unnoticed:
      * `<x>.request("area.verb", ...)`: every tool call in server/tools/;
      * a request envelope: a dict literal whose "type" is "request" and whose "action" is
        a string (the `test.*` helpers, the retry re-send);
      * `action = "area.verb"` (a builder that returns the action, fused_read.read_plan);
      * the members of the IDEMPOTENT_ACTIONS frozenset, which claims to name actions the
        extension handles.
    Response envelopes ("type": "response", the server.* answers) are deliberately not
    matched: the extension sends those requests, the server answers them.
    """
    server_dir = server_dir or SERVER_DIR
    found: dict[str, list[str]] = {}

    def add(action: str | None, path: Path, node: ast.AST) -> None:
        if action and _WIRE_ACTION_SHAPE.match(action):
            found.setdefault(action, []).append(f"{path.relative_to(server_dir.parent).as_posix()}:{node.lineno}")

    for path in sorted(server_dir.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "request" and node.args):
                add(_const_str(node.args[0]), path, node)
            elif isinstance(node, ast.Dict):
                pairs = {_const_str(k): v for k, v in zip(node.keys, node.values) if k is not None}
                if _const_str(pairs.get("type")) == "request":
                    add(_const_str(pairs.get("action")), path, node)
            elif isinstance(node, ast.Assign):
                names = [t.id for t in node.targets if isinstance(t, ast.Name)]
                if "action" in names:
                    add(_const_str(node.value), path, node)
                if "IDEMPOTENT_ACTIONS" in names and isinstance(node.value, ast.Call):
                    for arg in node.value.args:
                        for elt in getattr(arg, "elts", []):
                            add(_const_str(elt), path, elt)
    return found


def parse_dynamic_request_sites(server_dir: Path | None = None) -> list[str]:
    """["file:line", ...] for every `<x>.request(<non-literal>, ...)` call in server/.

    `parse_server_sent_actions` can only see a literal. A call whose action is a variable
    is invisible to it, so each such site must be named in `[server_sent_actions]` in
    parity.toml, with the reason its possible values are covered elsewhere.
    """
    server_dir = server_dir or SERVER_DIR
    sites: list[str] = []
    for path in sorted(server_dir.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "request" and node.args
                    and _const_str(node.args[0]) is None):
                sites.append(f"{path.relative_to(server_dir.parent).as_posix()}:{node.lineno}")
    return sites
