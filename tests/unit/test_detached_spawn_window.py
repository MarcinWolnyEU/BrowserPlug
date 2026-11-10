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
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from unittest.mock import patch

import pytest

import test_imports  # noqa: F401
from launcher import launcher
from browserplug_shared import server_env
from browserplug_shared.mcp_port import detached_spawn_kwargs
from server.server import BrowserPlugServer

DETACHED_PROCESS = 0x00000008
SHARED_PY = str(Path(__file__).resolve().parents[2] / "shared-py")


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


# --- POSIX: the same helper, judged by session and by what outlives the parent ---------

# A parent that starts one child with detached_spawn_kwargs() ("detached") or with none
# ("plain"), reports its pid, and then idles so the test can kill the parent's whole group.
_PARENT = textwrap.dedent("""
    import subprocess, sys, time
    sys.path.insert(0, sys.argv[2])
    from browserplug_shared.mcp_port import detached_spawn_kwargs
    kwargs = detached_spawn_kwargs() if sys.argv[1] == "detached" else {}
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, **kwargs)
    print(child.pid, flush=True)
    time.sleep(120)
""")


def _alive(pid):
    import psutil
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def _spawn_then_kill_the_parents_group(mode):
    """Start a parent in its own process group, let it spawn a child, SIGKILL that group.

    Returns the child's pid, for the caller to clean up. A child left in the parent's group
    dies with it; a detached one is in a session of its own and does not.
    """
    parent = subprocess.Popen([sys.executable, "-c", _PARENT, mode, SHARED_PY],
                              stdout=subprocess.PIPE, start_new_session=True)
    try:
        child_pid = int(parent.stdout.readline())
    finally:
        os.killpg(parent.pid, signal.SIGKILL)
        parent.wait(timeout=10)
        parent.stdout.close()
    return child_pid


@pytest.mark.skipif(os.name == "nt", reason="POSIX sessions")
def test_posix_detach_asks_for_a_new_session():
    assert detached_spawn_kwargs() == {"start_new_session": True}


@pytest.mark.skipif(os.name == "nt", reason="POSIX sessions")
def test_a_detached_child_leads_a_session_of_its_own():
    """The child is its own session and process group leader, so a signal sent to the
    parent's group (a terminal hang-up, a killpg from a supervisor) never reaches it."""
    probe = "import os; print(os.getsid(0), os.getpgid(0), os.getpid())"
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True,
                         timeout=30, **detached_spawn_kwargs()).stdout.split()
    sid, pgid, pid = map(int, out)
    assert sid == pid == pgid, f"not a session leader: sid={sid} pgid={pgid} pid={pid}"
    assert sid != os.getsid(0), "the child shares this test's session"


@pytest.mark.skipif(os.name == "nt", reason="POSIX sessions")
def test_a_detached_child_survives_its_parent_and_a_plain_one_does_not():
    """The property the server depends on: it outlives every client. The plain child is the
    control, proving the kill reaches a child that stayed in the group."""
    detached = _spawn_then_kill_the_parents_group("detached")
    plain = _spawn_then_kill_the_parents_group("plain")
    try:
        deadline = time.monotonic() + 10
        while _alive(plain) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _alive(plain), "the control child outlived its group: the kill proves nothing"
        assert _alive(detached), "the detached child died with its parent's group"
    finally:
        for pid in (detached, plain):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
