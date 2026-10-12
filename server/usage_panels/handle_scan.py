"""Which files does another process hold open, without putting this server at risk.

RULE: nothing on a /health path may do unbounded kernel-wide work.

The connection report (`/health`, `server_info`, the popup's poll) runs the session
detectors, and the Kilo and Copilot detectors need another process's open files. On Windows
psutil's `Process.open_files()` enumerates every handle on the system and duplicates each
one into the calling process, with a helper thread per handle. On 2026-10-09 an integration
sweep loaded the machine with eight browsers, one such call ran for minutes inside the
server, and every kernel object creation in the process (a socket for the WebSocket loop, a
thread for the HTTP loop) queued behind it. Both loops were silent for 62 s. A hung psutil
call cannot be interrupted from inside the process, so the fix is where the call runs:

* the scan runs in a short-lived child process (this file, run as a script), killed at a hard
  timeout, so the handle duplication happens in the child and a hang costs one child;
* the caller never waits: `lookup()` answers from a cache (or None while unknown) and a
  single worker thread refreshes it, so a report build cannot be held up by a scan at all;
* the scan is skipped while the machine is busy, results are cached for minutes (a miss for
  a minute), and a timeout or a failed spawn backs every scan off, doubling up to ten
  minutes, so a loaded machine is not asked for more of the work that is hurting it.

The same child also reads the working directory the extension host's children share (Copilot
Chat's workspace fallback), because `os.path.realpath` opens each path and can sit on a
network drive for a long time.

Importing this module has no side effects: no thread, no process, no psutil.
"""
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter, namedtuple

# A found set of paths changes only when the window or the CLI restarts, so it is kept for
# minutes; the callers re-read the files themselves every few seconds. A miss is retried
# sooner, because "no log yet" turns into "a log" the moment the first chat turn is logged.
FRESH_SECONDS = 300.0
MISS_SECONDS = 60.0
# Wait before asking again when the machine was too busy to scan.
LOAD_RETRY_SECONDS = 15.0
SCAN_TIMEOUT_SECONDS = 5.0
BACKOFF_FIRST_SECONDS = 30.0
BACKOFF_MAX_SECONDS = 600.0
# Mean CPU use over the last sampling window at or above which no scan starts.
BUSY_CPU_PERCENT = 85.0
CPU_SAMPLE_SECONDS = 2.0
MAX_ENTRIES = 64

Scan = namedtuple("Scan", "paths workspace")

_SELF = os.path.abspath(__file__)
_clock = time.monotonic


class ScanTimeout(Exception):
    """The child did not answer within SCAN_TIMEOUT_SECONDS and was killed."""


class ScanUnavailable(Exception):
    """No child could be started, or it answered nonsense."""


class ScanFailed(Exception):
    """The child ran and says the process cannot be read (gone, access denied)."""


class Pending(Exception):
    """Raised by a detector whose answer waits on a scan; the caller must not cache it."""


# --- The child: runs as `python -I handle_scan.py <pid> [workspace]` --------------------

def _norm(path):
    # realpath expands 8.3 names: %TEMP% is often C:\Users\ABCDEF~1\..., a child's cwd
    # the long spelling of the same folder.
    return os.path.normcase(os.path.realpath(path)).rstrip("\\/")


def child_workspace(proc):
    """The cwd most of ``proc``'s children share, other than its own and the temp dir
    (where an MCP launcher child runs); ``None`` when there is none."""
    try:
        skip = {_norm(proc.cwd())}
    except Exception:
        skip = set()
    temp = _norm(tempfile.gettempdir())
    counts, spelling = Counter(), {}
    try:
        children = proc.children()
    except Exception:
        return None
    for child in children:
        try:
            cwd = child.cwd()
        except Exception:
            continue
        key = _norm(cwd) if cwd else None
        if not key or key in skip or key == temp or key.startswith(temp + os.sep):
            continue
        counts[key] += 1
        spelling.setdefault(key, cwd)
    if not counts:
        return None
    return spelling[counts.most_common(1)[0][0]]


def _child_main(argv):
    out = {}
    try:
        import psutil
        proc = psutil.Process(int(argv[1]))
        out["paths"] = [f.path for f in proc.open_files()]
        if "workspace" in argv[2:]:
            out["workspace"] = child_workspace(proc)
    except Exception as e:
        out = {"error": type(e).__name__}
    # ASCII-only JSON: -I ignores PYTHONIOENCODING, and a path may hold any character.
    sys.stdout.buffer.write(json.dumps(out).encode("ascii"))
    sys.stdout.buffer.flush()


# --- The parent -------------------------------------------------------------------------

def _child_flags():
    if os.name != "nt":
        return 0
    # No console window, and below normal priority: the scan must not compete with the work
    # that made the machine busy.
    return (getattr(subprocess, "CREATE_NO_WINDOW", 0)
            | getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0))


def _scan(pid, workspace, timeout=None):
    """Run the child and return its Scan. Raises ScanTimeout, ScanUnavailable, ScanFailed."""
    if not sys.executable:
        raise ScanUnavailable("no interpreter")
    limit = SCAN_TIMEOUT_SECONDS if timeout is None else timeout
    args = [sys.executable, "-I", _SELF, str(int(pid))] + (["workspace"] if workspace else [])
    try:
        child = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, creationflags=_child_flags())
    except (OSError, ValueError) as e:
        raise ScanUnavailable(str(e)) from e
    try:
        raw, _ = child.communicate(timeout=limit)
    except subprocess.TimeoutExpired:
        # A child stuck in the kernel may not die at once; never wait for it for long.
        try:
            child.kill()
            child.wait(1)
        except Exception:
            pass
        try:
            child.stdout.close()
        except Exception:
            pass
        raise ScanTimeout(f"no answer within {limit:g}s") from None
    try:
        data = json.loads(raw.decode("ascii", "replace"))
        if not isinstance(data, dict):
            raise ValueError("not an object")
    except ValueError as e:
        raise ScanUnavailable(f"unreadable answer: {e}") from e
    if "error" in data:
        raise ScanFailed(str(data["error"]))
    paths = tuple(str(p) for p in data.get("paths") or ())
    return Scan(paths, data.get("workspace") or None)


_cpu = {"at": None, "busy": False}


def machine_busy():
    """Whether the machine is too loaded to start a scan. Sampled at most every
    CPU_SAMPLE_SECONDS, from psutil's mean CPU use since the previous sample."""
    now = _clock()
    if _cpu["at"] is not None and now - _cpu["at"] < CPU_SAMPLE_SECONDS:
        return _cpu["busy"]
    first = _cpu["at"] is None
    _cpu["at"] = now
    try:
        import psutil
        percent = psutil.cpu_percent(interval=None)
    except Exception:
        percent = 0.0
    # The first sample has nothing to compare with and reads 0.0; a busy machine shows up
    # on the next one.
    _cpu["busy"] = (not first) and percent >= BUSY_CPU_PERCENT
    return _cpu["busy"]


class _Entry:
    __slots__ = ("scan", "fresh_until", "retry_at")

    def __init__(self, scan=None, fresh_until=0.0, retry_at=0.0):
        self.scan, self.fresh_until, self.retry_at = scan, fresh_until, retry_at


_lock = threading.Lock()
_entries = {}              # (pid, workspace) -> _Entry
_queued = set()            # keys waiting for, or in, a scan
_queue = queue.SimpleQueue()
_worker = None
_backoff_until = 0.0
_backoff_step = 0.0


def lookup(pid, workspace=False):
    """The open files of ``pid`` as a Scan(paths, workspace), or None while not known yet.

    Never waits for a scan: an unknown or expired answer queues one and returns what is
    cached (the stale answer, else None). ``workspace=True`` also asks for the cwd the
    process's children share."""
    key = (int(pid), bool(workspace))
    now = _clock()
    with _lock:
        entry = _entries.get(key)
        if entry is not None and now < entry.fresh_until:
            return entry.scan
        due = (now >= _backoff_until and key not in _queued
               and (entry is None or now >= entry.retry_at))
        if due:
            _queued.add(key)
            _queue.put(key)
        stale = entry.scan if entry is not None else None
    if due:
        _ensure_worker()
    return stale


def _ensure_worker():
    global _worker
    with _lock:
        if _worker is not None and _worker.is_alive():
            return
        _worker = threading.Thread(target=_work, name="usage-scan", daemon=True)
        _worker.start()


def _work():
    while True:
        key = _queue.get()
        if key is None:          # the stop sentinel (tests)
            return
        process(key)


def process(key):
    """Scan one queued key and file the result. The worker's body; tests call it directly."""
    global _backoff_until, _backoff_step
    pid, workspace = key
    scan, fresh, retry = None, 0.0, 0.0
    try:
        if machine_busy():
            retry = _clock() + LOAD_RETRY_SECONDS
        else:
            try:
                scan = _scan(pid, workspace)
                fresh = _clock() + (FRESH_SECONDS if scan.paths else MISS_SECONDS)
                _backoff_step = 0.0
            except ScanFailed:
                # The process is gone or closed to us: an answer, and it will not change soon.
                scan = Scan((), None)
                fresh = _clock() + MISS_SECONDS
            except (ScanTimeout, ScanUnavailable):
                with _lock:
                    _backoff_step = (BACKOFF_FIRST_SECONDS if not _backoff_step
                                     else min(_backoff_step * 2, BACKOFF_MAX_SECONDS))
                    _backoff_until = _clock() + _backoff_step
                retry = _backoff_until
    finally:
        with _lock:
            old = _entries.get(key)
            if scan is None and old is not None:
                scan = old.scan             # keep the stale answer visible
            _entries[key] = _Entry(scan, fresh, retry)
            _queued.discard(key)
            if len(_entries) > MAX_ENTRIES:
                now = _clock()
                for dead in [k for k, e in _entries.items()
                             if e.fresh_until <= now and e.retry_at <= now]:
                    del _entries[dead]


def _reset():
    """Forget every cached answer and the back-off (tests only)."""
    global _backoff_until, _backoff_step
    with _lock:
        _entries.clear()
        _queued.clear()
        _backoff_until = _backoff_step = 0.0
        _cpu.update(at=None, busy=False)
    while True:
        try:
            _queue.get_nowait()
        except queue.Empty:
            break


if __name__ == "__main__":
    _child_main(sys.argv)
