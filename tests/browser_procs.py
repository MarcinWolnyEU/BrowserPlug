"""Start and stop a test browser together with everything it spawns, on POSIX.

Chrome forks renderer, GPU and utility helpers; Firefox forks content, GPU and socket
processes. Signalling only the parent leaves them running, holding the profile open and
eating memory across tests. On POSIX a browser therefore starts in a session of its own
(its pid becomes the process-group id), and a stop signals the whole group. Windows keeps
its `taskkill /F /T` path in each harness; nothing here runs there.
"""

import os
import signal
import subprocess
import time

IS_POSIX = os.name == 'posix'


def group_popen_kwargs():
    """Popen kwargs that give the browser its own process group on POSIX; {} on Windows."""
    return {'start_new_session': True} if IS_POSIX else {}


def _killpg(pgid, sig):
    """Signal a process group; False when no process of it is left."""
    try:
        os.killpg(pgid, sig)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _live_members(pgid):
    """Pids of the non-zombie processes in the group, or None when psutil is missing.

    A zombie is dead and only waits for its parent to reap it, so it must not keep a stop
    waiting: Linux's `kill(-pgid, 0)` succeeds on a zombie-only group, and macOS answers it
    with EPERM."""
    try:
        import psutil   # a declared server dependency, present in the test venv
    except ImportError:
        return None
    members = []
    for p in psutil.process_iter():
        try:
            if os.getpgid(p.pid) == pgid and p.status() != psutil.STATUS_ZOMBIE:
                members.append(p.pid)
        except (psutil.Error, OSError):
            continue   # gone while we looked, or not ours to read
    return members


def _group_gone(pgid):
    """True when the group has no live member left (signal 0 only probes)."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        pass   # macOS for a zombie-only group; the member scan below decides
    return _live_members(pgid) == []


def stop_process_group(proc, timeout=5):
    """SIGTERM the browser's group, wait up to `timeout`, then SIGKILL what is left.

    Returns True if anything had to be force-killed. The group id is the pid of the process
    `group_popen_kwargs` started, and it outlives the parent while any helper runs, so the
    group is signalled even when the parent has already exited (a first-run relaunch).
    Once the parent is reaped and no member is left, nothing is signalled: the group id is
    then a free pid that an unrelated process may already hold."""
    if proc is None:
        return False
    pgid = proc.pid
    if proc.poll() is not None and _group_gone(pgid):
        return False
    _killpg(pgid, signal.SIGTERM)
    forced = False
    deadline = time.monotonic() + timeout
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        forced = True
        deadline = 0   # the parent ignored SIGTERM: no point waiting for its helpers
    gone = _group_gone(pgid)
    while not gone and time.monotonic() < deadline:
        time.sleep(0.05)
        gone = _group_gone(pgid)
    if not gone:
        forced = True
        _killpg(pgid, signal.SIGKILL)
    if proc.poll() is None:
        proc.kill()
    proc.wait()
    return forced


def kill_user_data_dir_stragglers(user_data_dir, timeout=5):
    """SIGKILL every process whose command line names `--user-data-dir=<user_data_dir>`.

    The catch-all for a Chrome helper that left the group (or a relaunch the group never
    held). The directory is a unique mkdtemp name, so a browser the user runs themselves
    cannot match."""
    if not user_data_dir:
        return
    try:
        import psutil   # a declared server dependency, present in the test venv
    except ImportError:
        return
    needle = os.path.normpath(str(user_data_dir)).lower()
    victims = []
    for p in psutil.process_iter():
        try:
            cmd = p.cmdline()
        except psutil.Error:
            continue
        for arg in cmd:
            if arg.lower().startswith('--user-data-dir=') and \
                    os.path.normpath(arg.split('=', 1)[1]).lower() == needle:
                victims.append(p)
                break
    for p in victims:
        try:
            p.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(victims, timeout=timeout)
