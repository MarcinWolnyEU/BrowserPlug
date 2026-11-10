r"""The pid of the MCP client that spawned this launcher.

`os.getppid()` is the client only when nothing sits between the two. Three kinds of process can:

- On Windows, a venv's `Scripts\python.exe` is a redirector (a uv trampoline, or the stock venv
  launcher) that starts the real interpreter as a child and waits. Run through it, the
  launcher's parent is the redirector, and the server named every client "Python" with the
  redirector's Temp cwd. POSIX venvs have no such process: `bin/python` is a symlink.
- uv itself. A client registered as `uv run --script server/launcher.py` gives the chain
  client -> uv -> (on Windows) the script env's trampoline -> this interpreter. uv does not
  `exec` the script on any platform (measured on macOS with uv 0.13.7: the launcher's parent
  is uv), so it stays resident for the whole session and is never the client.
- A scripted shell (`cmd /c ...`, `powershell -Command ...`, `sh -c ...`, `zsh -lc ...`) that
  a client used to start the launcher. The shell is not the client, the process above it is.

The Windows redirector is skipped only when all three hold, so a launcher started any other
way keeps its plain parent:

- the launcher runs inside a venv (`sys.prefix != sys.base_prefix`); a system or uv-managed
  interpreter started directly has no redirector above it;
- the parent's image is that venv's own `Scripts\python.exe` or `pythonw.exe`;
- the parent was created within a couple of seconds of this process. A redirector starts its
  child at once (9 to 40 ms measured, parallel load included), while a client that merely runs
  in the same venv is far older.

uv is skipped, after the redirector (or directly, when the launcher runs on a bare interpreter
under `uv run`), when the parent's image is `uv` or `uvx` (`.exe` on Windows) and it was created
before this process and within UV_SPAWN_WINDOW_SECONDS of it. The window is wider than the
redirector's because uv may build the launcher's environment (resolve and install psutil)
before it starts the interpreter; the ordering test is what rules out a recycled pid.

A shell is skipped only when it was handed a command flag. One without a flag is a terminal a
person typed the launcher into, and there the shell is the nearest thing to a client. This is
the rule of `server/reporting/client_identity.py` (`is_shell_wrapper`), repeated here because
the launcher may not import the server; `tests/unit/test_client_pid.py` keeps the two equal,
and the uv names too (`UV_WRAPPER_NAMES`).

The header, the `/timing` reports and the session's row must all carry this one pid, so the
server can match a timing to its session. Any failure to probe falls back to `os.getppid()`.
Stdlib only, like the rest of `launcher/`. Windows reads the table with
browserplug_shared.proc_facts's ctypes reader, the same one the launcher's server-ownership
check uses. Linux and macOS use `PosixProbe`: argv comes from `proc_facts.facts`, and the parent
pid and start time from `/proc` on Linux, from libproc on macOS (`proc_facts.parent_and_start`)
and from `ps` where neither is available, since `facts` carries neither.
"""

import errno
import functools
import ntpath
import os
import re
import subprocess
import sys
import time

from browserplug_shared import proc_facts
from browserplug_shared.proc_facts import WindowsProbe as _WindowsProbe

# A redirector spawns its child within milliseconds. The margin covers a loaded machine.
SPAWN_WINDOW_SECONDS = 2.0
REDIRECTOR_NAMES = ("python.exe", "pythonw.exe")
# uv resolves and installs a script's environment before it starts the interpreter; a cold
# first run downloads psutil. Same names as server/reporting/client_identity.py.
UV_SPAWN_WINDOW_SECONDS = 120.0
UV_WRAPPER_NAMES = frozenset({"uv.exe", "uv", "uvx.exe", "uvx"})
# Same names and flags as server/reporting/client_identity.py.
SHELL_WRAPPER_NAMES = frozenset({
    "cmd.exe", "cmd", "powershell.exe", "powershell", "pwsh.exe", "pwsh",
    "bash.exe", "bash", "sh.exe", "sh", "zsh.exe", "zsh", "wsl.exe",
    "dash", "ksh", "fish",
})
SHELL_COMMAND_FLAGS = frozenset({"-c", "/c", "-command", "-encodedcommand", "-ec"})
# Shells that take POSIX option clusters: `zsh -lc`, `bash -ilc` and `bash -cl` all mean "run
# this command". The cluster rule is not applied to cmd or PowerShell, whose `-ExecutionPolicy`
# also contains a "c".
POSIX_SHELL_NAMES = frozenset({"bash", "sh", "zsh", "dash", "ksh", "fish"})
_OPTION_CLUSTER = re.compile(r"-[a-z]+")
MAX_SHELL_HOPS = 3

_IS_WINDOWS = os.name == "nt"


def _image_name(probe, pid):
    """The lowercase file name of the process image, without a login shell's leading dash."""
    image = probe.image_path(pid).replace("\\", "/")
    return os.path.basename(image).lower().lstrip("-")


def _is_venv_redirector(own_pid, pid, probe, prefix):
    # ntpath, not os.path: the redirector exists only on Windows, and the policy is tested
    # with Windows paths on every platform.
    scripts = ntpath.normcase(ntpath.realpath(ntpath.join(prefix, "Scripts")))
    # realpath on both sides. Short-name and long-name launches were observed to agree with
    # each other, so this only guards a junction or link that spells the two differently.
    image = ntpath.normcase(ntpath.realpath(probe.image_path(pid)))
    directory, name = ntpath.split(image)
    if directory != scripts or name not in REDIRECTOR_NAMES:
        return False
    return abs(probe.created(own_pid) - probe.created(pid)) <= SPAWN_WINDOW_SECONDS


def _is_uv_wrapper(own_pid, pid, probe):
    if _image_name(probe, pid) not in UV_WRAPPER_NAMES:
        return False
    age = probe.created(own_pid) - probe.created(pid)
    # A probe with a coarse clock (ps reports whole seconds) may see uv and this process
    # start "together"; the slop keeps that from reading as a recycled pid.
    return -getattr(probe, "order_slop", 0.0) <= age <= UV_SPAWN_WINDOW_SECONDS


def is_command_flag(shell_name, token):
    """Does `token` tell the shell `shell_name` to run a command rather than talk to a person?"""
    token = token.lower()
    if token in SHELL_COMMAND_FLAGS:
        return True
    stem = shell_name[:-4] if shell_name.endswith(".exe") else shell_name
    return (stem in POSIX_SHELL_NAMES and _OPTION_CLUSTER.fullmatch(token) is not None
            and "c" in token)


def _is_shell_wrapper(pid, probe):
    name = _image_name(probe, pid)
    if name not in SHELL_WRAPPER_NAMES:
        return False
    arguments = getattr(probe, "arguments", None)
    if arguments is not None:
        tokens = arguments(pid)
    else:
        # The first token is the shell itself; split() is enough, since a flag is its own token.
        tokens = probe.command_line(pid).split()[1:]
    return any(is_command_flag(name, token) for token in tokens)


def pick_client_pid(own_pid, ppid, probe, prefix, base_prefix):
    """The client above this launcher: `ppid`, minus a venv redirector, uv and wrapper shells.

    Pure over `probe`. An unreadable link in the chain keeps the last pid that was.
    """
    pid = ppid
    if (getattr(probe, "redirectors", True) and prefix != base_prefix
            and _is_venv_redirector(own_pid, pid, probe, prefix)):
        above = probe.parent(pid)
        if above > 0:
            pid = above
    try:
        if _is_uv_wrapper(own_pid, pid, probe):
            above = probe.parent(pid)
            if above > 0:
                pid = above
    except OSError:
        pass
    for _ in range(MAX_SHELL_HOPS):
        try:
            if not _is_shell_wrapper(pid, probe):
                break
            above = probe.parent(pid)
        except OSError:
            break
        if above <= 0:
            break
        pid = above
    return pid


# ---- Linux and macOS ---------------------------------------------------------------------

PS_TIMEOUT_SECONDS = 5.0
# Stat times are whole seconds from `ps`, and clock ticks from /proc.
PS_ORDER_SLOP_SECONDS = 1.5


def parse_proc_stat(text):
    """(parent pid, start time in clock ticks since boot) from the text of /proc/<pid>/stat.

    The command name is parenthesised and may hold spaces and parentheses, so the fields
    are counted from the last ")".
    """
    fields = text.rpartition(")")[2].split()
    return int(fields[1]), int(fields[19])


def parse_etime(text):
    """Seconds from `ps -o etime=`: [[dd-]hh:]mm:ss."""
    text = text.strip()
    days, _, clock = text.rpartition("-")
    parts = [int(part) for part in clock.split(":")]
    if not 2 <= len(parts) <= 3 or any(part < 0 for part in parts):
        raise ValueError(f"not an elapsed time: {text!r}")
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + part
    return seconds + (int(days) * 86400 if days else 0)


# Absolute: the launcher inherits the MCP client's PATH, and a different `ps` earlier on it
# would be believed about which process is the client. /bin/ps exists on macOS and Linux.
PS = "/bin/ps"


def _ps(pid, field):
    """One `ps` column of `pid`; OSError when the process is gone or ps cannot run."""
    try:
        done = subprocess.run([PS, "-ww", "-o", f"{field}=", "-p", str(pid)],
                              capture_output=True, text=True, timeout=PS_TIMEOUT_SECONDS,
                              env={**os.environ, "LC_ALL": "C"})
    except subprocess.SubprocessError as exc:
        raise OSError(f"ps failed for pid {pid}: {exc}") from None
    text = done.stdout.strip()
    if done.returncode != 0 or not text:
        raise OSError(f"ps could not read {field} of pid {pid}")
    return text


class PosixProbe:
    """The process-table reads `pick_client_pid` needs, on Linux and macOS.

    There is no venv redirector on POSIX, so the policy skips that step (`redirectors`).
    The parent pid and start time come from /proc on Linux and from libproc on macOS
    (proc_facts.parent_and_start, microsecond start times); `ps` stands in where neither
    reads them. `order_slop` follows the clock in use.
    """

    redirectors = False
    order_slop = PS_ORDER_SLOP_SECONDS

    def __init__(self):
        self._linux = sys.platform.startswith("linux") and os.path.isdir("/proc/self")
        self._native = (not self._linux and sys.platform == "darwin"
                        and proc_facts.darwin_probe() is not None)
        if self._native:
            # proc_bsdinfo's start time is exact, so a parent started before its child
            # always reads strictly earlier: the whole-second slop `ps` needs does not apply.
            self.order_slop = 0.0
        self._argv = {}
        self._foreign = set()
        self._native_rows = {}

    def argv(self, pid):
        if pid not in self._argv:
            try:
                same_user, argv, _cwd = proc_facts.facts(pid)
            except PermissionError:
                # Another user's process (macOS refuses to read it): never this launcher's
                # client or one of its wrappers, so it has no argv to ask about. The walk
                # ends there instead of believing what `ps` prints for it.
                same_user, argv = False, []
            except (OSError, proc_facts.ProbeUnavailable):
                # No native reader (a bare interpreter on macOS without psutil): ps prints
                # the joined command line, which is enough to find the name and the flags.
                # A path with spaces in its first token shifts nothing, because only the
                # tokens after it are read as flags.
                argv = _ps(pid, "command").split()
                same_user = True
            if not same_user:
                self._foreign.add(pid)
            self._argv[pid] = argv if same_user else []
        return self._argv[pid]

    def image_path(self, pid):
        argv = self.argv(pid)
        if argv:
            return argv[0]
        if pid in self._foreign:
            # Another user's process: no `ps comm` fallback either, or the walk would
            # believe the one reading it refused to take natively.
            raise PermissionError(errno.EPERM, f"pid {pid} belongs to another user")
        return _ps(pid, "comm")

    def arguments(self, pid):
        return self.argv(pid)[1:]

    def _proc_stat(self, pid):
        with open(f"/proc/{pid}/stat", "r", encoding="utf-8", errors="replace") as fh:
            return parse_proc_stat(fh.read())

    def _libproc(self, pid):
        """(parent pid, start time) from libproc, read once per pid. OSError when the pid
        cannot be read."""
        if pid not in self._native_rows:
            self._native_rows[pid] = proc_facts.parent_and_start(pid)
        return self._native_rows[pid]

    def parent(self, pid):
        if self._linux:
            return self._proc_stat(pid)[0]
        if self._native:
            return self._libproc(pid)[0]
        return int(_ps(pid, "ppid"))

    def created(self, pid):
        """Seconds on a clock shared by every pid this probe is asked about."""
        if self._linux:
            return self._proc_stat(pid)[1] / os.sysconf("SC_CLK_TCK")
        if self._native:
            return self._libproc(pid)[1]
        return time.time() - parse_etime(_ps(pid, "etime"))


@functools.lru_cache(maxsize=1)
def client_pid():
    """The client's pid, computed once: every request and timing report sends it."""
    ppid = os.getppid()
    try:
        if _IS_WINDOWS:
            return pick_client_pid(os.getpid(), ppid, _WindowsProbe(), sys.prefix,
                                   sys.base_prefix)
        return pick_client_pid(os.getpid(), ppid, PosixProbe(), sys.prefix, sys.base_prefix)
    except Exception:
        return ppid
