"""A detached server must start WITHOUT popping a terminal window.

Both spawners (launcher.spawn_detached_server and server.runtime.lifecycle.spawn_restart_process) used
DETACHED_PROCESS (0x8) -- the textbook "no console" flag -- and every spawn and every
/restart still flashed up a console window on Windows.

The cause is one process hop further down than the flag can see. A uv environment's
`Scripts/python.exe` (the server's script environment, and the checkout's .venv before it)
is a **uv trampoline** (~45 KB) that re-execs the real interpreter
(~/AppData/Roaming/uv/python/...) as a CHILD, passing no creation flags of its own. A
console app started by a parent that has NO console, with no console flag, gets a
BRAND NEW console allocated by Windows -- visible. So DETACHED_PROCESS, the flag whose
whole job was to suppress our window, is what handed the grandchild one.

The live test below asserts the PROPERTY (the interpreter that actually ends up running
server.py has no console window), not the flag constant -- because the flag was never
the thing that was wrong, the process topology was. It spawns through sys.executable,
which in the test environment is the same kind of trampoline the spawners start, so if a
uv environment's python.exe stops being a trampoline the test still measures the real
answer.
"""

import os
import subprocess
import sys
import textwrap
import time
from unittest.mock import patch

import pytest

import test_imports  # noqa: F401
from launcher import launcher
from browserplug_shared import server_env
from browserplug_shared.mcp_port import detached_spawn_kwargs
from server.server import BrowserPlugServer

DETACHED_PROCESS = 0x00000008


class _FakePopen:
    def __init__(self):
        self.kwargs = None

    def __call__(self, args, **kwargs):
        self.kwargs = kwargs
        return object()


@pytest.mark.skipif(os.name != "nt", reason="console windows are a Windows concept")
def test_a_detached_child_really_has_no_console_window(tmp_path):
    """THE property: spawned this way, the interpreter running our code has no console.

    Measured through sys.executable, a uv environment's trampoline like the one the
    spawners start, so the trampoline hop is exercised rather than assumed away.
    """
    probe = tmp_path / "probe.py"
    result = tmp_path / "result.txt"
    probe.write_text(textwrap.dedent(f"""
        import ctypes, sys
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        visible = bool(ctypes.windll.user32.IsWindowVisible(hwnd)) if hwnd else False
        with open(r"{result}", "w") as f:
            f.write(f"{{hwnd}} {{visible}} {{sys.executable}}")
    """), encoding="utf-8")

    child = subprocess.Popen([sys.executable, str(probe)],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, **detached_spawn_kwargs())
    for _ in range(80):
        if result.exists() and result.read_text(encoding="utf-8"):
            break
        time.sleep(0.25)
    assert result.exists(), "the probe never ran"
    child.wait(timeout=10)  # reap it: an unreaped Popen raises ResourceWarning at gc

    hwnd, visible, _exe = result.read_text(encoding="utf-8").split(" ", 2)
    assert hwnd == "0" and visible == "False", (
        f"the detached child owns console window {hwnd} (visible={visible}) — a terminal "
        f"window pops up on every spawn/restart. See detached_spawn_kwargs(): "
        f"DETACHED_PROCESS makes the uv trampoline's own child allocate a fresh console.")


@pytest.mark.skipif(os.name != "nt", reason="creationflags are Windows-only")
def test_detached_spawn_kwargs_never_asks_for_detached_process():
    """DETACHED_PROCESS wins over CREATE_NO_WINDOW, so passing both restores the window."""
    flags = detached_spawn_kwargs()["creationflags"]
    assert not flags & DETACHED_PROCESS, (
        "DETACHED_PROCESS is back — it takes precedence over CREATE_NO_WINDOW and the "
        "console window returns")


@pytest.mark.skipif(os.name != "nt", reason="creationflags are Windows-only")
def test_both_spawners_use_the_shared_windowless_policy(tmp_path):
    """One policy, two spawners — the split is exactly how they drifted apart before."""
    expected = detached_spawn_kwargs()["creationflags"]

    fake = _FakePopen()
    # The seam keeps the interpreter lookup off uv, whose calls the fake Popen would eat.
    with patch.dict(os.environ,
                    {"BROWSERPLUG_LOG_FILE": str(tmp_path / "browserplug-server.log"),
                     server_env.SERVER_PYTHON_ENV: sys.executable}, clear=False), \
         patch.object(subprocess, "Popen", fake):
        launcher.spawn_detached_server()
    if fake.kwargs["stdout"] is not subprocess.DEVNULL:
        fake.kwargs["stdout"].close()
    assert fake.kwargs["creationflags"] == expected, "launcher spawn is not windowless"

    server = BrowserPlugServer(host="localhost", port=8772, start_mcp=False)
    fake = _FakePopen()
    with patch.dict(os.environ,
                    {"BROWSERPLUG_LOG_FILE": str(tmp_path / "restart.log")}, clear=False), \
         patch.object(subprocess, "Popen", fake):
        server.lifecycle.spawn_restart_process()
    if fake.kwargs["stdout"] is not subprocess.DEVNULL:
        fake.kwargs["stdout"].close()
    assert fake.kwargs["creationflags"] == expected, "/restart replacement is not windowless"
