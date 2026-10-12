"""Test profiles must not pile up in %TEMP%.

On Windows a Firefox that has just been stopped leaves files in its profile locked for a
moment, and cleanup() runs straight after it; a Firefox left running on a fresh profile
keeps parent.lock, so the profile can be neither cached nor opened by the next Firefox.
These pin the parts of the answer: every stop takes the whole process tree down and then
sweeps for a browser still on the profile, the removal retries while the locks
clear (on its own thread, so the next test need not wait), and the exit sweep waits for
those threads and then tries any profile that still resisted. The last test pins the
glue, because the helpers alone stay green with cleanup() bypassing them.
"""

import os
import shutil
import subprocess
import sys

import pytest

import test_imports  # noqa: F401  (automatic path setup)
import firefox_test_utils as ftu
from firefox_test_utils import FirefoxTestManager


def _rmtree_failing(times):
    """An rmtree that raises the Windows sharing violation `times` times, then works."""
    real = shutil.rmtree
    calls = []

    def rmtree(path, *args, **kwargs):
        calls.append(path)
        if len(calls) <= times:
            raise PermissionError(32, "The process cannot access the file because it is "
                                      "being used by another process")
        return real(path, *args, **kwargs)

    return rmtree, calls


def test_a_locked_profile_is_retried_until_the_lock_clears(tmp_path, monkeypatch):
    profile = tmp_path / "browserplug-extracted-locked"
    (profile / "cache2").mkdir(parents=True)
    (profile / "cache2" / "entry").write_text("x")
    rmtree, calls = _rmtree_failing(times=2)
    monkeypatch.setattr(ftu.shutil, "rmtree", rmtree)

    assert ftu._remove_profile_dir(str(profile), timeout=5.0) is True
    assert not profile.exists()
    assert len(calls) == 3


def test_a_profile_that_stays_locked_is_left_for_the_exit_sweep(tmp_path, monkeypatch):
    profile = tmp_path / "browserplug-extracted-stuck"
    profile.mkdir()
    real = shutil.rmtree
    monkeypatch.setattr(ftu, "_UNREMOVED_PROFILE_DIRS", set())
    rmtree, _calls = _rmtree_failing(times=10**6)
    monkeypatch.setattr(ftu.shutil, "rmtree", rmtree)

    assert ftu._remove_profile_dir(str(profile), timeout=0.2) is False
    assert str(profile) in ftu._UNREMOVED_PROFILE_DIRS

    monkeypatch.setattr(ftu.shutil, "rmtree", real)   # every Firefox is gone by exit
    ftu._remove_unremoved_profile_dirs()
    assert not profile.exists()
    assert not ftu._UNREMOVED_PROFILE_DIRS


def test_the_exit_sweep_waits_for_a_background_removal(tmp_path, monkeypatch):
    profile = tmp_path / "browserplug-extracted-background"
    (profile / "cache2").mkdir(parents=True)
    rmtree, calls = _rmtree_failing(times=3)
    monkeypatch.setattr(ftu.shutil, "rmtree", rmtree)
    monkeypatch.setattr(ftu, "_PENDING_REMOVALS", [])
    monkeypatch.setattr(ftu, "_UNREMOVED_PROFILE_DIRS", set())

    ftu._remove_profile_dir_in_background(str(profile))
    ftu._remove_unremoved_profile_dirs()      # what atexit runs

    assert not profile.exists()
    assert len(calls) == 4
    assert not any(worker.is_alive() for worker in ftu._PENDING_REMOVALS)


class _LiveProcess:
    pid = 4321

    def __init__(self):
        self.terminated = False
        self.waited = False

    def poll(self):
        return None

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        self.waited = True
        return 0

    def kill(self):
        pass


@pytest.mark.skipif(os.name != "nt", reason="taskkill /T is the Windows path")
def test_stop_firefox_takes_down_the_whole_process_tree(monkeypatch):
    ran, swept = [], []
    monkeypatch.setattr(ftu.subprocess, "run", lambda args, **kwargs: ran.append(args))
    monkeypatch.setattr(ftu, "_kill_profile_stragglers",
                        lambda profile, exe, timeout=5: swept.append((profile, exe)))
    manager = FirefoxTestManager.__new__(FirefoxTestManager)   # no ports, no profile
    proc = _LiveProcess()
    manager.firefox_process = proc
    manager.profile_dir = "C:/tmp/browserplug-extracted-abc"
    manager.firefox_path = "C:/Program Files/Firefox Developer Edition/firefox.exe"

    manager.stop_firefox()

    assert ran == [["taskkill", "/F", "/T", "/PID", "4321"]]
    assert swept == [("C:/tmp/browserplug-extracted-abc", "firefox")]
    assert not proc.terminated and proc.waited
    assert manager.firefox_process is None


def _process_on_profile(profile_dir):
    """A stand-in browser: a real process whose command line names `profile_dir`."""
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)",
                             "-profile", str(profile_dir), "-no-remote"])


def test_the_sweep_kills_only_processes_running_on_this_profile(tmp_path):
    """The case taskkill /T cannot reach: a browser whose parent has already gone (a
    first run relaunches Firefox as a new process) still holds parent.lock. The sweep
    finds it by the profile's unique directory name and leaves every other process
    alone, including one on a sibling profile."""
    ours = tmp_path / "browserplug-new-profile-ours"
    theirs = tmp_path / "browserplug-new-profile-theirs"
    survivor = _process_on_profile(ours)
    bystander = _process_on_profile(theirs)
    python = ftu._exe_name(sys.executable)
    try:
        ftu._kill_profile_stragglers(str(ours), python, timeout=10)
        assert survivor.wait(timeout=10) is not None
        assert bystander.poll() is None, "a process on another profile was killed"
    finally:
        ftu._kill_profile_stragglers(str(theirs), python, timeout=10)
        for proc in (survivor, bystander):
            if proc.poll() is None:
                proc.kill()


def _manager_for_extensions_run(profile_dir, monkeypatch, stopped, make_json):
    """A manager whose extensions.json run starts a fake Firefox and records how it is
    stopped. `make_json` decides whether Firefox 'writes' extensions.json."""
    manager = FirefoxTestManager.__new__(FirefoxTestManager)
    manager.profile_dir = str(profile_dir)
    manager.firefox_path = "C:/Program Files/Firefox Developer Edition/firefox.exe"
    fake = _LiveProcess()

    def popen(*args, **kwargs):
        if make_json:
            (profile_dir / "extensions.json").write_text("{}")
        return fake

    monkeypatch.setattr(ftu.subprocess, "Popen", popen)
    monkeypatch.setattr(ftu.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(ftu, "_stop_firefox_tree",
                        lambda proc, profile, exe, timeout=5: stopped.append((proc, profile, exe)))
    return manager, fake


@pytest.mark.parametrize("make_json, result", [(True, True), (False, False)])
def test_the_extensions_json_run_stops_through_the_tree_helper(tmp_path, monkeypatch,
                                                               make_json, result):
    """The glue: both ends of the cold-profile run (extensions.json written, or never)
    must stop Firefox through _stop_firefox_tree, not a parent-only terminate()."""
    profile = tmp_path / "browserplug-new-profile-cold"
    profile.mkdir()
    stopped = []
    manager, fake = _manager_for_extensions_run(profile, monkeypatch, stopped, make_json)

    assert manager._initialize_extensions_json() is result
    assert stopped == [(fake, str(profile), "firefox")]
    assert not fake.terminated


def test_cleanup_hands_the_profile_to_the_background_removal(tmp_path, monkeypatch):
    profile = tmp_path / "browserplug-extracted-glue"
    profile.mkdir()
    removed = []
    monkeypatch.setattr(ftu, "_remove_profile_dir_in_background", removed.append)
    monkeypatch.setattr(FirefoxTestManager, "cache_key", property(lambda self: "not-cached"))
    manager = FirefoxTestManager.__new__(FirefoxTestManager)
    manager.firefox_process = None
    manager.profile_dir = str(profile)

    manager.cleanup()

    assert removed == [str(profile)]
    assert manager.profile_dir is None
