"""The launcher keeps its Popen handle and watches the child it spawned (review L11, L16).

Before: the handle was discarded, so a server that died in one second still cost the whole
~20 s wait, a lock loser was never respawned, and the traceback in the startup capture never
reached the client (which saw a bare connection refusal near its 30 s budget). Two launchers
spawning together also shared one capture name and truncated each other.
"""

import os
import subprocess
import textwrap
import time as _time
from unittest.mock import patch

import pytest

from server import preflight
from browserplug_shared import mcp_port
from launcher import launcher

_REAL_SLEEP = _time.sleep


class _Child:
    def __init__(self, code):
        self._code = code

    def poll(self):
        return self._code


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("BROWSERPLUG_LOG_FILE", str(tmp_path / "browserplug-server.log"))
    monkeypatch.delenv("BROWSERPLUG_MCP_PORT", raising=False)
    sleeps = []
    monkeypatch.setattr(launcher.time, "sleep", lambda s: sleeps.append(s))
    return tmp_path, sleeps


def _spawner(tmp_path, scripted):
    """A spawn_detached_server stand-in: each call consumes (exit code, capture text)."""
    calls = []

    def spawn():
        code, text = scripted[len(calls)]
        path = tmp_path / f"spawn{len(calls)}.startup.log"
        path.write_text(text, encoding="utf-8")
        calls.append(path)
        return launcher.SpawnedServer(_Child(code), str(path))

    return spawn, calls


TRACE = ("Traceback (most recent call last):\n  File \"server.py\", line 1, in <module>\n"
         "ImportError: cannot import name 'boom'\n")


def test_a_child_that_dies_at_once_ends_the_wait_and_reports_its_traceback(env):
    tmp_path, sleeps = env
    spawn, calls = _spawner(tmp_path, [(3, TRACE)])
    with patch.object(launcher, "find_server_port", return_value=None), \
            patch.object(launcher, "spawn_detached_server", spawn):
        assert launcher.ensure_server() is None
    assert len(calls) == 1, "a crash with a traceback dies the same way twice; no respawn"
    assert len(sleeps) < 3, f"waited {len(sleeps)} polls on a dead child"
    assert "ImportError: cannot import name 'boom'" in launcher.spawn_failure
    assert "exited with code 3" in launcher.spawn_failure
    assert str(calls[0]) in launcher.spawn_failure


def test_a_lock_loser_is_respawned_once_and_the_second_child_is_awaited(env):
    tmp_path, sleeps = env
    lost = "ERROR Could not acquire the BrowserPlug single-instance lock (held by PID: 9)\n"
    spawn, calls = _spawner(tmp_path, [(1, lost), (None, "")])
    seen = iter([None, None, None, None, 2830])   # entry, loop, post-exit, pre-respawn, then up
    with patch.object(launcher, "find_server_port", lambda: next(seen, 2830)), \
            patch.object(launcher, "spawn_detached_server", spawn):
        assert launcher.ensure_server() == 2830
    assert len(calls) == 2


def test_a_lock_loser_is_not_respawned_twice(env):
    tmp_path, _ = env
    lost = "single-instance lock\n"
    spawn, calls = _spawner(tmp_path, [(1, lost), (1, lost)])
    with patch.object(launcher, "find_server_port", return_value=None), \
            patch.object(launcher, "spawn_detached_server", spawn):
        assert launcher.ensure_server() is None
    assert len(calls) == 2
    assert "single-instance lock" in launcher.spawn_failure


def test_a_peer_server_that_appears_as_the_child_exits_is_used(env):
    tmp_path, _ = env
    spawn, calls = _spawner(tmp_path, [(0, "another healthy server holds the lock\n")])
    seen = iter([None, 2831])
    with patch.object(launcher, "find_server_port", lambda: next(seen, 2831)), \
            patch.object(launcher, "spawn_detached_server", spawn):
        assert launcher.ensure_server() == 2831
    assert len(calls) == 1


def test_a_healthy_slow_child_still_gets_the_full_wait(env):
    tmp_path, sleeps = env
    spawn, _ = _spawner(tmp_path, [(None, "")])
    with patch.object(launcher, "find_server_port", return_value=None), \
            patch.object(launcher, "spawn_detached_server", spawn):
        assert launcher.ensure_server() is None
    assert len(sleeps) == 40


def test_spawn_returns_the_popen_handle_and_its_capture_path(env):
    tmp_path, _ = env
    sentinel = _Child(None)
    with patch.object(subprocess, "Popen", lambda args, **kw: sentinel):
        spawned = launcher.spawn_detached_server()
    assert spawned.child is sentinel
    assert os.path.exists(spawned.startup_log)


def test_two_spawns_get_distinct_capture_files_that_do_not_truncate_each_other(env):
    tmp_path, _ = env
    files = []

    def popen(args, **kw):
        kw["stdout"].write("child says hi\n")
        files.append(kw["stdout"].name)
        return _Child(None)

    with patch.object(subprocess, "Popen", popen):
        first = launcher.spawn_detached_server()
        with patch.object(launcher.time, "time", return_value=1.0):   # same millisecond
            second = launcher.spawn_detached_server()
            launcher.spawn_detached_server()
    assert first.startup_log != second.startup_log
    assert "child says hi" in open(first.startup_log, encoding="utf-8").read()
    assert first.startup_log.endswith(".startup.log")


def test_old_captures_are_pruned_but_the_newest_stay(env):
    tmp_path, _ = env
    str(tmp_path / "browserplug-server.log")
    for i in range(9):
        p = tmp_path / f"browserplug-server.{i}-1.startup.log"
        p.write_text("x")
        os.utime(p, (1000 + i, 1000 + i))
    with patch.object(subprocess, "Popen", lambda args, **kw: _Child(None)):
        spawned = launcher.spawn_detached_server()
    left = [f for f in os.listdir(tmp_path) if f.endswith(".startup.log")]
    assert len(left) <= launcher.STARTUP_LOGS_KEPT
    assert os.path.basename(spawned.startup_log) in left
    assert "browserplug-server.8-1.startup.log" in left


def test_a_real_short_lived_child_is_noticed_and_its_traceback_surfaces(env, tmp_path):
    """No fake Popen: a real python child that crashes at import, like a broken edit."""
    _, sleeps = env
    script = tmp_path / "crashing_server.py"
    script.write_text(textwrap.dedent("""\
        import sys
        raise RuntimeError("real child blew up")
        """), encoding="utf-8")
    real_sleep_calls = []
    with patch.object(launcher, "SERVER", str(script)), \
            patch.object(launcher, "find_server_port", return_value=None), \
            patch.object(launcher.time, "sleep",
                         lambda s: (real_sleep_calls.append(s), _REAL_SLEEP(0.2))):
        assert launcher.ensure_server() is None
    assert "RuntimeError: real child blew up" in launcher.spawn_failure
    assert len(real_sleep_calls) < 40


def test_the_restart_startup_log_is_named_in_the_client_error_and_in_preflight():
    assert "browserplug-restart*.startup.log" in launcher.RESTART_STARTUP_HINT
    assert "browserplug-restart*.startup.log" in preflight._START_SERVER
    assert mcp_port.startup_log_glob("C:/x/browserplug-server.log").endswith(
        "browserplug-server*.startup.log")


@pytest.mark.parametrize("log_file_set", [True, False], ids=["log-file-set", "log-file-unset"])
def test_the_launcher_finds_the_capture_a_restart_replacement_writes(
        tmp_path, monkeypatch, log_file_set):
    """lifecycle names a restart's capture `<log base>.restart-<pid>-<ms>.startup.log`. The
    pattern the launcher and preflight point a human at must match that file, whether the
    server ran with BROWSERPLUG_LOG_FILE or fell back to server/browserplug-restart.log."""
    import fnmatch
    import glob
    from types import SimpleNamespace
    from server.runtime import lifecycle
    from server.server import BrowserPlugServer

    server = BrowserPlugServer(host="localhost", port=8772, mcp_port=2851, start_mcp=False,
                               ws_token="t" * 32)
    if log_file_set:
        log = tmp_path / "browserplug-server.log"
        monkeypatch.setenv("BROWSERPLUG_LOG_FILE", str(log))
        pattern = mcp_port.startup_log_glob(log)       # what the client error names
    else:
        monkeypatch.delenv("BROWSERPLUG_LOG_FILE", raising=False)
        # lifecycle writes beside server.py; keep the test out of the checkout
        monkeypatch.setattr(lifecycle, "restart_log_file",
                            lambda server_dir: mcp_port.restart_log_file(str(tmp_path)))
        pattern = mcp_port.restart_startup_log_glob(str(tmp_path))
        # and the launcher's hint is that same pattern for the real server directory
        assert launcher.RESTART_STARTUP_HINT == mcp_port.restart_startup_log_glob(
            os.path.dirname(lifecycle.SERVER_SCRIPT))
        assert os.path.basename(launcher.RESTART_STARTUP_HINT) in preflight._START_SERVER

    captured = {}

    def fake_popen(args, **kwargs):
        captured["out"] = kwargs["stdout"]
        return SimpleNamespace(pid=1234)
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    server.lifecycle.spawn_restart_process()
    written = captured["out"].name
    captured["out"].close()

    assert ".restart-" in os.path.basename(written)
    assert glob.glob(pattern) == [written]
    assert fnmatch.fnmatch(os.path.basename(written), os.path.basename(pattern))
