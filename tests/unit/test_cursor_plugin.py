"""The Cursor plugin files: `.cursor-plugin/plugin.json` and the root `mcp.json`.

No Cursor and no browser: the tests read the two JSON files and check what Cursor's plugin
reference and the Agent Plugins MCP schema require of them, plus the two promises BrowserPlug
makes (the stdio entry runs the launcher, the HTTP entry names the default MCP port).
`.mcp.json` with a dot is the Codex plugin's generated, git-ignored file and is not read here.
"""

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / ".cursor-plugin" / "plugin.json"
MCP_JSON = ROOT / "mcp.json"

SCHEMA_URL = "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"
KEBAB_NAME = re.compile(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$")
PLACEHOLDER = re.compile(r"\$\{([^}]+)\}")
# Expanded by Cursor itself, so a plugin does not declare them under `variables`.
BUILT_IN_PLACEHOLDERS = {"CURSOR_PLUGIN_ROOT", "CLAUDE_PLUGIN_ROOT"}


def _load(path):
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def manifest():
    return _load(MANIFEST)


@pytest.fixture(scope="module")
def mcp():
    return _load(MCP_JSON)


def _relative_inside_root(value):
    """A manifest path Cursor accepts: relative, no `..`, and it exists in the checkout."""
    path = Path(value)
    assert not path.is_absolute() and ".." not in path.parts, value
    assert (ROOT / path).resolve().is_relative_to(ROOT), value
    return ROOT / path


def test_manifest_name_is_kebab_case(manifest):
    assert KEBAB_NAME.match(manifest["name"]), manifest["name"]
    assert manifest["name"] == "browserplug"


def test_manifest_metadata_is_complete(manifest):
    assert manifest["description"].strip()
    assert manifest["author"]["name"] == "Marcin Wolny"
    assert manifest["license"] == "MIT"
    assert "MIT License" in (ROOT / "LICENSE").read_text(encoding="utf-8")
    for key in ("homepage", "repository"):
        assert manifest[key] == "https://github.com/MarcinWolnyEU/browserplug"
    assert manifest["keywords"] and all(isinstance(k, str) for k in manifest["keywords"])


def test_manifest_paths_are_relative_and_exist(manifest):
    for key in ("logo", "mcpServers"):
        assert isinstance(manifest[key], str), key
        assert _relative_inside_root(manifest[key]).is_file(), (key, manifest[key])
    assert _relative_inside_root(manifest["mcpServers"]) == MCP_JSON


def test_mcp_json_is_not_the_codex_dotfile(manifest):
    assert Path(manifest["mcpServers"]).name == "mcp.json"


def test_schema_sits_at_the_top_level_only(mcp):
    assert mcp["$schema"] == SCHEMA_URL
    assert set(mcp) == {"$schema", "mcpServers"}
    for name, server in mcp["mcpServers"].items():
        assert "$schema" not in server, name


def test_stdio_entry_launches_the_launcher_from_the_plugin_root(mcp):
    server = mcp["mcpServers"]["browserplug"]
    assert server["type"] == "stdio"
    assert server["command"] == "uv"
    assert server["cwd"] == "./"
    assert server["args"] == ["run", "--script", "server/launcher.py"]
    # Relative to cwd, so it must be the checkout's launcher stub, never server/server.py.
    assert (ROOT / server["args"][-1]).is_file()
    assert set(server) <= {"type", "command", "args", "cwd", "env"}


def test_stdio_entry_leaves_the_interpreter_to_the_launcher(mcp):
    """Without BROWSERPLUG_SERVER_PYTHON the launcher builds its uv environment. Cursor's
    handling of an unset plugin variable is undocumented, and a literal placeholder reaching
    the launcher would be read as an interpreter path, so the entry does not pass one."""
    server = mcp["mcpServers"]["browserplug"]
    assert "BROWSERPLUG_SERVER_PYTHON" not in json.dumps(server.get("env", {}))


def test_http_entry_points_at_the_default_mcp_port(mcp):
    server = mcp["mcpServers"]["browserplug-http"]
    assert server["type"] == "streamable-http"
    assert server["url"] == "http://localhost:2830/mcp"
    assert set(server) <= {"type", "url", "headers"}


def test_every_placeholder_is_declared_or_built_in(manifest, mcp):
    used = set(PLACEHOLDER.findall(json.dumps(mcp["mcpServers"])))
    declared = set(manifest.get("variables", {}).get("properties", {}))
    assert used - BUILT_IN_PLACEHOLDERS <= declared, used - BUILT_IN_PLACEHOLDERS - declared
    if "variables" in manifest:
        assert manifest["variables"]["type"] == "object"
