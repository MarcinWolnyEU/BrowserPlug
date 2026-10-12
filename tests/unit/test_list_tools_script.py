"""scripts/list-tools.sh: a read-only diagnostic of the RUNNING server.

It needs a python that imports mcp.Client and finds the server environment's. It must look
that up read-only: ensure_server_python syncs and may repair the very environment the live
server runs from (a package swapped under it, or its site-packages deleted)."""

import os
import re
import subprocess
import sys
from pathlib import Path

import test_imports  # noqa: F401
from browserplug_shared import server_env

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "list-tools.sh"


def _resolver_source():
    """The python the script runs to name the interpreter (its first heredoc)."""
    text = SCRIPT.read_text(encoding="utf-8").replace("\r\n", "\n")
    match = re.search(r'PY="\$\(ROOT="\$ROOT" uv run --no-project python - <<\'PY\'\n(.*?)\nPY\n',
                      text, re.S)
    assert match, "list-tools.sh no longer resolves its interpreter in the expected heredoc"
    return match.group(1)


def test_the_script_never_syncs_or_repairs_the_live_servers_environment():
    source = _resolver_source()
    assert "ensure_server_python" not in source
    assert "inspect_server_env" in source


def _resolve(tmp_path, **env):
    environ = {k: v for k, v in os.environ.items()
               if k.upper() not in (server_env.SERVER_PYTHON_ENV, server_env.UV_ENV, "UV",
                                    "VIRTUAL_ENV", "XDG_BIN_HOME")}
    environ.update(ROOT=str(tmp_path), PYTHONPATH=str(ROOT / "shared-py"), PATH="",
                   USERPROFILE=str(tmp_path), HOME=str(tmp_path), LOCALAPPDATA=str(tmp_path),
                   **env)
    return subprocess.run([sys.executable, "-c", _resolver_source()], capture_output=True,
                          text=True, env=environ, timeout=60, stdin=subprocess.DEVNULL)


def test_the_resolver_prints_the_interpreter_inspect_names(tmp_path):
    proc = _resolve(tmp_path, **{server_env.SERVER_PYTHON_ENV: sys.executable})
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == sys.executable


def test_the_resolver_exits_with_the_problem_and_its_fix(tmp_path):
    (tmp_path / "server").mkdir()
    (tmp_path / "server" / "server.py").write_text("# stand-in\n", encoding="utf-8")
    proc = _resolve(tmp_path)
    assert proc.returncode != 0
    assert "no server interpreter: uv was not found" in proc.stderr
    assert "Fix:" in proc.stderr
