"""server/usage_panels/handle_scan.py: another process's open files, without risking the server.

The rule under test: nothing on a /health path may do unbounded kernel-wide work. On
2026-10-09 psutil's open_files() ran for minutes inside the server under load and froze both
loops. Here the scan is a child process with a hard timeout, the caller never waits, a busy
machine is not scanned, and a failure backs every scan off.

Run: uv run --locked --script tests/run.py unit/test_handle_scan.py -q
"""
import os
import subprocess
import sys
import threading
import time

import pytest

import test_imports  # noqa: F401
from server.usage_panels import handle_scan
from server.usage_panels.handle_scan import Scan

# The conftest turns the worker thread off for every test; one test here wants the real one.
_REAL_ENSURE_WORKER = handle_scan._ensure_worker


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(handle_scan, "_clock", c)
    monkeypatch.setattr(handle_scan, "machine_busy", lambda: False)
    return c


def drain():
    """Run every queued scan on this thread, as the worker would."""
    ran = []
    while True:
        try:
            key = handle_scan._queue.get_nowait()
        except Exception:
            return ran
        handle_scan.process(key)
        ran.append(key)


# --- the caller never waits ----------------------------------------------------------

def test_a_lookup_returns_at_once_and_queues_one_scan(clock, monkeypatch):
    started = threading.Event()
    release = threading.Event()

    def slow(pid, workspace, timeout=None):
        started.set()
        release.wait(10)
        return Scan(("a.log",), None)

    monkeypatch.setattr(handle_scan, "_scan", slow)
    t0 = time.monotonic()
    assert handle_scan.lookup(4242) is None
    assert handle_scan.lookup(4242) is None, "a second ask queues nothing more"
    assert time.monotonic() - t0 < 0.5, "lookup must not wait for the scan"
    assert handle_scan._queue.qsize() == 1
    release.set()
    assert drain() == [(4242, False)]
    assert handle_scan.lookup(4242) == Scan(("a.log",), None)


def test_a_fresh_answer_is_served_from_the_cache_until_it_expires(clock, monkeypatch):
    calls = []
    monkeypatch.setattr(handle_scan, "_scan",
                        lambda pid, ws, timeout=None: calls.append(pid) or Scan(("a.log",), None))
    handle_scan.lookup(7)
    drain()
    for _ in range(20):
        assert handle_scan.lookup(7).paths == ("a.log",)
    assert calls == [7]
    clock.t += handle_scan.FRESH_SECONDS + 1
    stale = handle_scan.lookup(7)
    assert stale.paths == ("a.log",), "the stale answer is served while the refresh runs"
    drain()
    assert calls == [7, 7]


def test_a_miss_is_remembered_for_a_shorter_time_than_a_hit(clock, monkeypatch):
    calls = []
    monkeypatch.setattr(handle_scan, "_scan",
                        lambda pid, ws, timeout=None: calls.append(pid) or Scan((), None))
    handle_scan.lookup(7)
    drain()
    assert handle_scan.lookup(7) == Scan((), None)
    assert handle_scan.MISS_SECONDS < handle_scan.FRESH_SECONDS
    clock.t += handle_scan.MISS_SECONDS + 1
    handle_scan.lookup(7)
    drain()
    assert calls == [7, 7]


def test_a_process_that_cannot_be_read_is_a_cached_miss_and_no_back_off(clock, monkeypatch):
    def gone(pid, ws, timeout=None):
        raise handle_scan.ScanFailed("NoSuchProcess")

    monkeypatch.setattr(handle_scan, "_scan", gone)
    handle_scan.lookup(7)
    drain()
    assert handle_scan.lookup(7) == Scan((), None)
    assert handle_scan._backoff_until == 0.0


def test_the_workspace_flag_is_part_of_the_key(clock, monkeypatch):
    seen = []
    monkeypatch.setattr(handle_scan, "_scan",
                        lambda pid, ws, timeout=None: seen.append(ws) or Scan(("x",), "w" if ws else None))
    handle_scan.lookup(5)
    handle_scan.lookup(5, workspace=True)
    drain()
    assert sorted(seen) == [False, True]
    assert handle_scan.lookup(5).workspace is None
    assert handle_scan.lookup(5, workspace=True).workspace == "w"


def test_the_cache_cannot_grow_without_bound(clock, monkeypatch):
    monkeypatch.setattr(handle_scan, "_scan", lambda pid, ws, timeout=None: Scan(("a",), None))
    for pid in range(1, 200):
        handle_scan.lookup(pid)
        drain()
        clock.t += handle_scan.FRESH_SECONDS + 1     # every earlier entry has expired
    assert len(handle_scan._entries) <= handle_scan.MAX_ENTRIES + 1


# --- a busy machine is not scanned -----------------------------------------------------

def test_no_scan_starts_while_the_machine_is_busy(clock, monkeypatch):
    monkeypatch.setattr(handle_scan, "machine_busy", lambda: True)
    spawned = []
    monkeypatch.setattr(handle_scan, "_scan", lambda *a, **k: spawned.append(a))
    handle_scan.lookup(7)
    drain()
    assert spawned == [], "no child process while the machine is loaded"
    assert handle_scan.lookup(7) is None
    assert handle_scan._queue.qsize() == 0, "and no hammering: the next try waits"
    clock.t += handle_scan.LOAD_RETRY_SECONDS + 1
    monkeypatch.setattr(handle_scan, "machine_busy", lambda: False)
    monkeypatch.setattr(handle_scan, "_scan", lambda *a, **k: Scan(("a.log",), None))
    handle_scan.lookup(7)
    drain()
    assert handle_scan.lookup(7) == Scan(("a.log",), None)


def test_a_busy_machine_keeps_the_stale_answer_visible(clock, monkeypatch):
    monkeypatch.setattr(handle_scan, "_scan", lambda *a, **k: Scan(("a.log",), None))
    handle_scan.lookup(7)
    drain()
    clock.t += handle_scan.FRESH_SECONDS + 1
    monkeypatch.setattr(handle_scan, "machine_busy", lambda: True)
    handle_scan.lookup(7)
    drain()
    assert handle_scan.lookup(7) == Scan(("a.log",), None)


def test_machine_busy_reads_cpu_use_and_samples_rarely(monkeypatch):
    import psutil
    clock = Clock()
    monkeypatch.setattr(handle_scan, "_clock", clock)
    handle_scan._cpu.update(at=None, busy=False)
    readings = iter([0.0, 97.0, 12.0])
    calls = []
    monkeypatch.setattr(psutil, "cpu_percent",
                        lambda interval=None: calls.append(interval) or next(readings))
    assert handle_scan.machine_busy() is False, "the first sample has nothing to compare"
    assert handle_scan.machine_busy() is False and len(calls) == 1, "sampled at most every 2 s"
    clock.t += handle_scan.CPU_SAMPLE_SECONDS + 0.1
    assert handle_scan.machine_busy() is True
    clock.t += handle_scan.CPU_SAMPLE_SECONDS + 0.1
    assert handle_scan.machine_busy() is False
    assert set(calls) == {None}, "never the blocking form of cpu_percent"


# --- a hung or failing scan backs everything off -----------------------------------------

def test_a_timeout_backs_every_scan_off_and_the_back_off_doubles(clock, monkeypatch):
    def hang(pid, ws, timeout=None):
        raise handle_scan.ScanTimeout("no answer")

    monkeypatch.setattr(handle_scan, "_scan", hang)
    handle_scan.lookup(1)
    drain()
    assert handle_scan._backoff_until == clock.t + handle_scan.BACKOFF_FIRST_SECONDS
    # A different pid is held off too: the hazard is machine-wide, not per process.
    handle_scan.lookup(2)
    assert handle_scan._queue.qsize() == 0
    clock.t += handle_scan.BACKOFF_FIRST_SECONDS + 1
    handle_scan.lookup(2)
    drain()
    assert handle_scan._backoff_until == clock.t + 2 * handle_scan.BACKOFF_FIRST_SECONDS
    for _ in range(10):
        clock.t = handle_scan._backoff_until + 1
        handle_scan.lookup(3)
        drain()
    assert handle_scan._backoff_until - clock.t <= handle_scan.BACKOFF_MAX_SECONDS


def test_a_success_ends_the_back_off(clock, monkeypatch):
    outcomes = [handle_scan.ScanTimeout("x"), Scan(("a",), None)]

    def scan(pid, ws, timeout=None):
        out = outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return out

    monkeypatch.setattr(handle_scan, "_scan", scan)
    handle_scan.lookup(1)
    drain()
    clock.t = handle_scan._backoff_until + 1
    handle_scan.lookup(1)
    drain()
    assert handle_scan._backoff_step == 0.0
    assert handle_scan.lookup(1) == Scan(("a",), None)


def test_a_failed_spawn_backs_off_like_a_timeout(clock, monkeypatch):
    def nope(pid, ws, timeout=None):
        raise handle_scan.ScanUnavailable("CreateProcess failed")

    monkeypatch.setattr(handle_scan, "_scan", nope)
    handle_scan.lookup(1)
    drain()
    assert handle_scan._backoff_until > clock.t


# --- the real child -------------------------------------------------------------------------

def test_the_child_reports_a_file_this_process_holds_open(tmp_path):
    held = tmp_path / "held-open-by-the-test.log"
    with open(held, "w") as fh:
        fh.write("x")
        scan = handle_scan._scan(os.getpid(), True, timeout=30)
    assert any(os.path.normcase(p) == os.path.normcase(str(held)) for p in scan.paths), scan
    assert scan.workspace is None or isinstance(scan.workspace, str)


def test_the_child_names_the_error_for_a_process_that_does_not_exist():
    with pytest.raises(handle_scan.ScanFailed):
        handle_scan._scan(2_000_000_000, False, timeout=30)


def test_a_hung_child_is_killed_at_the_deadline(monkeypatch):
    """The point of the whole module: a scan that never returns costs one child process
    and a bounded wait, not a thread of ours stuck in the kernel."""
    children = []
    real_popen = subprocess.Popen

    def hung_popen(args, **kw):
        child = real_popen([sys.executable, "-I", "-c", "import time; time.sleep(120)"], **kw)
        children.append(child)
        return child

    monkeypatch.setattr(subprocess, "Popen", hung_popen)
    t0 = time.monotonic()
    with pytest.raises(handle_scan.ScanTimeout):
        handle_scan._scan(os.getpid(), False, timeout=0.5)
    assert time.monotonic() - t0 < 10
    assert children and children[0].poll() is not None, "the hung child was killed"


def test_a_child_that_answers_nonsense_is_unavailable_not_a_crash(monkeypatch):
    real_popen = subprocess.Popen
    monkeypatch.setattr(
        subprocess, "Popen",
        lambda args, **kw: real_popen([sys.executable, "-I", "-c", "print('not json')"], **kw))
    with pytest.raises(handle_scan.ScanUnavailable):
        handle_scan._scan(os.getpid(), False, timeout=30)


# --- the worker thread -------------------------------------------------------------------------

def test_the_worker_thread_files_the_answer(clock, monkeypatch):
    monkeypatch.setattr(handle_scan, "_ensure_worker", _REAL_ENSURE_WORKER)
    monkeypatch.setattr(handle_scan, "_worker", None)
    monkeypatch.setattr(handle_scan, "_scan", lambda *a, **k: Scan(("seen.log",), None))
    try:
        assert handle_scan.lookup(31337) is None
        deadline = time.monotonic() + 10
        got = None
        while got is None and time.monotonic() < deadline:
            time.sleep(0.02)
            got = handle_scan.lookup(31337)
        assert got == Scan(("seen.log",), None)
        worker = handle_scan._worker
        assert worker.name == "usage-scan" and worker.daemon
    finally:
        # Stop it: a worker left behind would take queued keys from the tests that follow.
        handle_scan._queue.put(None)
        if handle_scan._worker is not None:
            handle_scan._worker.join(10)


def test_importing_the_module_starts_nothing():
    code = ("import sys, threading; before = threading.active_count();"
            "import server.usage_panels.handle_scan as m;"
            "assert threading.active_count() == before; assert 'psutil' not in sys.modules")
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([root, os.path.join(root, "shared-py")]))
    subprocess.run([sys.executable, "-c", code], check=True, env=env, timeout=60)
