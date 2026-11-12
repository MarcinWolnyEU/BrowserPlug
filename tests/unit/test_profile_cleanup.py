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
import signal
import subprocess
import sys
import time

import pytest

import test_imports  # noqa: F401  (automatic path setup)
import firefox_test_utils as ftu
from firefox_test_utils import FirefoxTestManager
from wait_helper import wait_until


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

    manager.popen_kwargs = []

    def popen(*args, **kwargs):
        manager.popen_kwargs.append(kwargs)
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
    from browser_procs import group_popen_kwargs
    assert all(manager.popen_kwargs[0].get(k) == v for k, v in group_popen_kwargs().items())


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


# ---- POSIX: a browser and its helpers live and die as one process group --------------

posix_only = pytest.mark.skipif(os.name != "posix", reason="process groups are the POSIX path")

_TREE = (
    "import signal, subprocess, sys, time\n"
    "ignore = sys.argv[1] == 'ignore'\n"
    "if ignore:\n"
    "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "child = subprocess.Popen([sys.executable, '-c',\n"
    "    'import signal, time\\n'\n"
    "    + ('signal.signal(signal.SIGTERM, signal.SIG_IGN)\\n' if ignore else '')\n"
    "    + 'time.sleep(60)'])\n"
    "print(child.pid, flush=True)\n"
    "time.sleep(60)\n"
)


def _browser_with_a_helper(mode):
    """A stand-in browser started the way the harness starts one, with a helper process of
    its own. `mode` 'ignore' makes both ignore SIGTERM, like a browser wedged on shutdown."""
    from browser_procs import group_popen_kwargs
    proc = subprocess.Popen([sys.executable, "-c", _TREE, mode], stdout=subprocess.PIPE,
                            text=True, **group_popen_kwargs())
    return proc, int(proc.stdout.readline())


def _gone(pid):
    """True once `pid` is dead: gone, or a zombie waiting for an init that reaps slowly."""
    import psutil
    try:
        return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True


@posix_only
@pytest.mark.parametrize("mode", ["obey", "ignore"])
def test_stop_takes_the_browsers_helpers_down_with_it(mode):
    """Signalling the parent alone orphans Chrome's renderers and Firefox's content
    processes. The stop goes to the group, and escalates to SIGKILL when SIGTERM is
    ignored."""
    proc, helper = _browser_with_a_helper(mode)
    try:
        forced = ftu._stop_firefox_tree(proc, None, "firefox", timeout=2)
        assert proc.poll() is not None
        wait_until(lambda: _gone(helper), "the helper ended with its browser", interval=0.05)
        assert forced is (mode == "ignore")
    finally:
        proc.stdout.close()
        if proc.poll() is None:
            proc.kill()


@posix_only
def test_stop_reaches_helpers_after_the_parent_has_gone():
    """A first run can relaunch the browser: the process we started exits while its
    helpers keep the profile. The group id outlives the parent, so the stop still finds
    them."""
    proc, helper = _browser_with_a_helper("obey")
    try:
        proc.kill()
        proc.wait()
        ftu._stop_firefox_tree(proc, None, "firefox", timeout=2)
        wait_until(lambda: _gone(helper), "the helper ended after its parent had gone", interval=0.05)
    finally:
        proc.stdout.close()


def test_browsers_start_in_a_group_of_their_own_on_posix():
    from browser_procs import group_popen_kwargs
    assert group_popen_kwargs() == ({"start_new_session": True} if os.name == "posix" else {})


@posix_only
def test_a_group_of_only_a_zombie_counts_as_gone_and_is_not_forced():
    """kill(-pgid, 0) succeeds on a zombie (Linux) or fails with EPERM (macOS), so the stop
    used to wait out its timeout and report `forced` for a browser that was already dead."""
    import psutil
    import browser_procs
    proc, helper = _browser_with_a_helper("obey")
    try:
        os.killpg(proc.pid, signal.SIGKILL)   # leader and helper die; nobody reaps the leader
        wait_until(lambda: browser_procs._group_gone(proc.pid),
                   "the group held nothing but a zombie", interval=0.05)
        # Nothing has reaped the leader yet: only a zombie is left in the group.
        assert psutil.Process(proc.pid).status() == psutil.STATUS_ZOMBIE
        started = time.monotonic()
        assert browser_procs.stop_process_group(proc, timeout=5) is False
        assert time.monotonic() - started < 3
    finally:
        proc.stdout.close()


def test_a_reaped_leader_and_an_empty_group_signal_nothing(monkeypatch):
    """The group id is a free pid by then: signalling it could hit an unrelated process."""
    import browser_procs
    signalled = []
    monkeypatch.setattr(browser_procs, "_killpg", lambda pgid, sig: signalled.append((pgid, sig)))
    monkeypatch.setattr(browser_procs, "_group_gone", lambda pgid: True)

    class Reaped:
        pid = 4321

        def poll(self):
            return 0

    assert browser_procs.stop_process_group(Reaped()) is False
    assert signalled == []


def test_the_chrome_sweep_kills_only_the_browser_on_this_user_data_dir(tmp_path):
    """A Chrome helper that left the group is still found by its --user-data-dir, which is
    a unique temp directory; a sibling profile's process is left alone."""
    from browser_procs import kill_user_data_dir_stragglers
    ours = tmp_path / "browserplug-new-profile-chromium-ours"
    theirs = tmp_path / "browserplug-new-profile-chromium-theirs"

    def chrome_on(path):
        return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)",
                                 f"--user-data-dir={path}", "--no-first-run"])

    survivor, bystander = chrome_on(ours), chrome_on(theirs)
    try:
        kill_user_data_dir_stragglers(str(ours), timeout=10)
        assert survivor.wait(timeout=10) is not None
        assert bystander.poll() is None, "a process on another user-data-dir was killed"
    finally:
        kill_user_data_dir_stragglers(str(theirs), timeout=10)
        for proc in (survivor, bystander):
            if proc.poll() is None:
                proc.kill()


_MAC_CFT = "Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"


@pytest.mark.parametrize("platform_name, machine, layout", [
    ("win32", "AMD64", "chrome-win64/chrome.exe"),
    ("linux", "x86_64", "chrome-linux64/chrome"),
    ("darwin", "arm64", f"chrome-mac-arm64/{_MAC_CFT}"),
    ("darwin", "x86_64", f"chrome-mac-x64/{_MAC_CFT}"),
    ("darwin", "arm64", f"chrome-mac-x64/{_MAC_CFT}"),   # Rosetta: only the Intel build is there
])
def test_the_vendored_chrome_resolves_on_every_supported_platform(
        tmp_path, monkeypatch, platform_name, machine, layout):
    """CHROME_PATH is optional: the dist copy resolves wherever the fetch script installs
    one. The layouts are the zips' own top-level directories."""
    import types
    import chromium_test_utils as ctu
    binary = tmp_path / layout
    binary.parent.mkdir(parents=True)
    binary.write_text("")
    monkeypatch.delenv("CHROME_PATH", raising=False)
    monkeypatch.setattr(ctu.shutil, "which", lambda name: None)
    monkeypatch.setattr(ctu, "_DIST_CHROME_DIR", tmp_path)
    monkeypatch.setattr(ctu, "sys", types.SimpleNamespace(platform=platform_name))
    monkeypatch.setattr(ctu, "platform", types.SimpleNamespace(machine=lambda: machine))
    assert ctu.resolve_chrome_binary() == str(binary)


def test_chrome_starts_in_its_own_group_and_stops_through_the_group_helper(tmp_path, monkeypatch):
    """The glue: the helpers alone stay green while the launch or the stop bypasses them."""
    import chromium_test_utils as ctu
    from browser_procs import group_popen_kwargs
    launched, stopped, swept = [], [], []
    fake = _LiveProcess()
    monkeypatch.setenv("BROWSERPLUG_CHROME_LOADER", "flag")   # the binary here is python

    def popen(args, **kwargs):
        launched.append(kwargs)
        return fake

    manager = ctu.ChromiumTestManager.__new__(ctu.ChromiumTestManager)
    manager.chrome_path = sys.executable   # only has to exist
    manager.baked_dir = str(tmp_path)
    manager.user_data_dir = str(tmp_path)
    manager.profile_dir = str(tmp_path)
    manager._chrome_log_handle = None
    manager._chrome_log_path = None
    manager.firefox_process = None
    monkeypatch.setattr(manager, "_bake_config", lambda: None)
    monkeypatch.setattr(ctu.subprocess, "Popen", popen)
    assert manager._start_firefox(headless=True, wait_for_startup=False)
    assert all(launched[0].get(key) == value for key, value in group_popen_kwargs().items())

    monkeypatch.setattr(ctu, "stop_process_group", lambda proc, timeout=5: stopped.append(proc))
    monkeypatch.setattr(ctu, "kill_user_data_dir_stragglers", lambda d, timeout=5: swept.append(d))
    monkeypatch.setattr(ctu.subprocess, "run", lambda *a, **k: None)   # Windows taskkill
    manager.stop_firefox()
    if os.name == "posix":
        assert stopped == [fake] and swept == [str(tmp_path)]
    assert manager.firefox_process is None


class _FakeDevToolsPipe:
    """Stands in for DevToolsPipe: records the launch and every call, starts no process."""

    def __init__(self):
        self.events = []
        self.popen_kwargs = None
        self.proc = _LiveProcess()
        _FakeDevToolsPipe.last = self

    def popen(self, flags, **kwargs):
        self.events.append("popen")
        self.popen_kwargs = kwargs
        self.flags = flags
        return self.proc

    def call(self, method, params=None, timeout=30.0):
        self.events.append((method, params))
        return {}

    def close(self):
        self.events.append("close")


def test_chrome_pipe_launch_gets_the_group_loads_the_extension_and_closes_the_pipe(
        tmp_path, monkeypatch):
    """The glue of the macOS branded-Chrome path: the pipe launch carries the group kwargs,
    the baked extension goes in over the pipe, and the stop closes the pipe."""
    import types
    import chromium_test_utils as ctu
    from browser_procs import group_popen_kwargs
    monkeypatch.setenv("BROWSERPLUG_CHROME_LOADER", "pipe")
    monkeypatch.setattr(ctu, "sys", types.SimpleNamespace(platform="linux"))   # Windows has no pipe
    monkeypatch.setattr(ctu, "DevToolsPipe", _FakeDevToolsPipe)
    manager = ctu.ChromiumTestManager.__new__(ctu.ChromiumTestManager)
    manager.chrome_path = __file__   # only has to exist
    manager.baked_dir = str(tmp_path)
    manager.user_data_dir = str(tmp_path)
    manager.profile_dir = str(tmp_path)
    manager._chrome_log_handle = None
    manager._chrome_log_path = None
    manager.firefox_process = None
    monkeypatch.setattr(manager, "_bake_config", lambda: None)

    assert manager._start_firefox(headless=True, wait_for_startup=False)
    pipe = _FakeDevToolsPipe.last
    assert manager.firefox_process is pipe.proc
    assert all(pipe.popen_kwargs.get(k) == v for k, v in group_popen_kwargs().items())
    assert "--remote-debugging-pipe" in pipe.flags
    assert pipe.events == ["popen", ("Extensions.loadUnpacked", {"path": str(tmp_path)})]

    monkeypatch.setattr(ctu, "stop_process_group", lambda proc, timeout=5: None)
    monkeypatch.setattr(ctu, "kill_user_data_dir_stragglers", lambda d, timeout=5: None)
    monkeypatch.setattr(ctu.subprocess, "run", lambda *a, **k: None)   # Windows taskkill
    manager.stop_firefox()
    assert pipe.events[-1] == "close"
    assert manager._devtools is None


@pytest.mark.parametrize("posix", [True, False], ids=["posix", "windows"])
def test_the_main_firefox_launch_starts_its_own_group_only_on_posix(tmp_path, monkeypatch,
                                                                    posix):
    import browser_procs
    launched = []
    monkeypatch.setattr(browser_procs, "IS_POSIX", posix)
    monkeypatch.setattr(ftu.subprocess, "Popen",
                        lambda cmd, **kwargs: launched.append(kwargs) or _LiveProcess())
    manager = FirefoxTestManager.__new__(FirefoxTestManager)
    manager.profile_dir = str(tmp_path)
    manager.firefox_path = "firefox"
    assert manager._start_firefox(headless=True, wait_for_startup=False)
    assert launched[0].get("start_new_session") is (True if posix else None)


@pytest.mark.parametrize("platform_name, mock_keychain", [("darwin", True), ("win32", False),
                                                           ("linux", False)])
def test_chrome_gets_a_mock_keychain_only_on_macos(tmp_path, monkeypatch, platform_name,
                                                   mock_keychain):
    """On macOS an unanswered keychain prompt keeps the extension's WebSocket from ever
    opening, so every chromium test would time out in setup."""
    import types
    import chromium_test_utils as ctu
    monkeypatch.setenv("BROWSERPLUG_CHROME_LOADER", "flag")
    manager = ctu.ChromiumTestManager.__new__(ctu.ChromiumTestManager)
    manager.chrome_path = "chrome"
    manager.baked_dir = str(tmp_path)
    manager.user_data_dir = str(tmp_path)
    monkeypatch.setattr(ctu, "sys", types.SimpleNamespace(platform=platform_name))
    flags = manager._launch_flags(headless=True)
    assert ("--use-mock-keychain" in flags) is mock_keychain
    assert "--headless=new" in flags and f"--load-extension={tmp_path}" in flags


@pytest.mark.parametrize("platform_name, path, env, loader", [
    ("darwin", "/x/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing", None, "flag"),
    ("darwin", "/Applications/Chromium.app/Contents/MacOS/Chromium", None, "flag"),
    ("darwin", "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary", None, "pipe"),
    ("darwin", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "flag", "flag"),
    ("win32", "C:/Program Files/Google/Chrome/Application/chrome.exe", None, "flag"),
    ("win32", "C:/Program Files/Google/Chrome/Application/chrome.exe", "pipe", "flag"),
    ("linux", "/opt/chrome/chrome", None, "flag"),
    ("linux", "/opt/chrome/chrome", "pipe", "pipe"),
])
def test_branded_chrome_on_macos_loads_the_extension_over_the_devtools_pipe(
        monkeypatch, platform_name, path, env, loader):
    """Canary, Beta and Stable ignore --load-extension, so only Chrome for Testing and
    Chromium take the flag on macOS; BROWSERPLUG_CHROME_LOADER overrides."""
    import types
    import chromium_test_utils as ctu
    monkeypatch.delenv("BROWSERPLUG_CHROME_LOADER", raising=False)
    if env:
        monkeypatch.setenv("BROWSERPLUG_CHROME_LOADER", env)
    monkeypatch.setattr(ctu, "sys", types.SimpleNamespace(platform=platform_name))
    assert ctu.extension_loader(path) == loader


def test_the_pipe_loader_swaps_the_load_flags_for_the_devtools_flags(tmp_path, monkeypatch):
    import chromium_test_utils as ctu
    import types
    monkeypatch.setenv("BROWSERPLUG_CHROME_LOADER", "pipe")
    monkeypatch.setattr(ctu, "sys", types.SimpleNamespace(platform="linux"))   # Windows has no pipe
    manager = ctu.ChromiumTestManager.__new__(ctu.ChromiumTestManager)
    manager.chrome_path = "chrome"
    manager.baked_dir = str(tmp_path)
    manager.user_data_dir = str(tmp_path)
    flags = manager._launch_flags(headless=True)
    assert "--remote-debugging-pipe" in flags and "--enable-unsafe-extension-debugging" in flags
    assert not any(f.startswith(("--load-extension", "--disable-extensions-except")) for f in flags)


_FAKE_CHROME = (
    "import json, os, sys\n"
    "buf = b''\n"
    "while True:\n"
    "    chunk = os.read(3, 4096)\n"
    "    if not chunk:\n"
    "        sys.exit(7)   # the pipe closed: a real Chrome exits here\n"
    "    buf += chunk\n"
    "    while b'\\0' in buf:\n"
    "        raw, _, buf = buf.partition(b'\\0')\n"
    "        msg = json.loads(raw)\n"
    "        os.write(4, json.dumps({'method': 'Some.event', 'params': {}}).encode() + b'\\0')\n"
    "        if msg['params'].get('path') == 'bad':\n"
    "            reply = {'id': msg['id'], 'error': {'code': -32000, 'message': 'no manifest'}}\n"
    "        else:\n"
    "            reply = {'id': msg['id'], 'result': {'id': 'abc' + os.path.basename(msg['params']['path'])}}\n"
    "        os.write(4, json.dumps(reply).encode() + b'\\0')\n"
)


@posix_only
def test_the_devtools_pipe_carries_a_command_to_the_browser_and_back():
    from chrome_cdp_pipe import DevToolsPipe
    pipe = DevToolsPipe()
    proc = pipe.popen([sys.executable, "-c", _FAKE_CHROME])
    try:
        assert pipe.call("Extensions.loadUnpacked", {"path": "/x/ext"}, timeout=20) == {"id": "abcext"}
        with pytest.raises(RuntimeError, match="no manifest"):
            pipe.call("Extensions.loadUnpacked", {"path": "bad"}, timeout=20)
        assert proc.poll() is None
        pipe.close()
        assert proc.wait(timeout=10) == 7, "closing the pipe is what stops a real Chrome"
    finally:
        pipe.close()
        if proc.poll() is None:
            proc.kill()
            proc.wait()


@posix_only
def test_a_silent_browser_fails_the_pipe_call_instead_of_hanging():
    from chrome_cdp_pipe import DevToolsPipe
    pipe = DevToolsPipe()
    proc = pipe.popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        with pytest.raises(RuntimeError, match="did not answer"):
            pipe.call("Extensions.loadUnpacked", {"path": "/x"}, timeout=0.5)
    finally:
        pipe.close()
        proc.kill()
        proc.wait()
