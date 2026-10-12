r"""The pid of the MCP client that spawned this launcher.

`os.getppid()` is the client only when nothing sits between the two. On Windows three kinds of
process can:

- A venv's `Scripts\python.exe` is a redirector (a uv trampoline, or the stock venv launcher)
  that starts the real interpreter as a child and waits. Run through it, the launcher's parent
  is the redirector, and the server named every client "Python" with the redirector's Temp cwd.
- uv itself. A client registered as `uv run --script server/launcher.py` gives the chain
  client -> uv.exe -> the script env's trampoline -> this interpreter, so after the redirector
  the next process up is uv.exe, which stays resident for the whole session and is never
  the client.
- A scripted shell (`cmd /c ...`, `powershell -Command ...`) that a client used to start the
  launcher. The shell is not the client, the process above it is.

The redirector is skipped only when all three hold, so a launcher started any other way keeps
its plain parent:

- the launcher runs inside a venv (`sys.prefix != sys.base_prefix`); a system or uv-managed
  interpreter started directly has no redirector above it;
- the parent's image is that venv's own `Scripts\python.exe` or `pythonw.exe`;
- the parent was created within a couple of seconds of this process. A redirector starts its
  child at once (9 to 40 ms measured, parallel load included), while a client that merely runs
  in the same venv is far older.

uv is skipped, after the redirector (or directly, when the launcher runs on a bare interpreter
under `uv run`), when the parent's image is `uv.exe` or `uvx.exe` and it was created before
this process and within UV_SPAWN_WINDOW_SECONDS of it. The window is wider than the
redirector's because uv may build the launcher's environment (resolve and install psutil)
before it starts the interpreter; the ordering test is what rules out a recycled pid.

A shell is skipped only when it was handed a command flag. One without a flag is a terminal a
person typed the launcher into, and there the shell is the nearest thing to a client. This is
the rule of `server/reporting/client_identity.py` (`is_shell_wrapper`), repeated here because
the launcher may not import the server; `tests/unit/test_client_pid.py` keeps the two equal,
and the uv names too (`UV_WRAPPER_NAMES`).

The header, the `/timing` reports and the session's row must all carry this one pid, so the
server can match a timing to its session. Any failure to probe falls back to `os.getppid()`.
Stdlib only, like the rest of `launcher/`: the probe is browserplug_shared.proc_facts's
ctypes reader, the same one the launcher's server-ownership check uses.
"""

import functools
import os
import sys

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
})
SHELL_COMMAND_FLAGS = frozenset({"-c", "/c", "-command", "-encodedcommand", "-ec"})
MAX_SHELL_HOPS = 3

_IS_WINDOWS = os.name == "nt"


def _is_venv_redirector(own_pid, pid, probe, prefix):
    scripts = os.path.normcase(os.path.realpath(os.path.join(prefix, "Scripts")))
    # realpath on both sides. Short-name and long-name launches were observed to agree with
    # each other, so this only guards a junction or link that spells the two differently.
    image = os.path.normcase(os.path.realpath(probe.image_path(pid)))
    directory, name = os.path.split(image)
    if directory != scripts or name not in REDIRECTOR_NAMES:
        return False
    return abs(probe.created(own_pid) - probe.created(pid)) <= SPAWN_WINDOW_SECONDS


def _is_uv_wrapper(own_pid, pid, probe):
    name = os.path.basename(probe.image_path(pid).replace("\\", "/")).lower()
    if name not in UV_WRAPPER_NAMES:
        return False
    age = probe.created(own_pid) - probe.created(pid)
    return 0 <= age <= UV_SPAWN_WINDOW_SECONDS


def _is_shell_wrapper(pid, probe):
    name = os.path.basename(probe.image_path(pid).replace("\\", "/")).lower()
    if name not in SHELL_WRAPPER_NAMES:
        return False
    # The first token is the shell itself; split() is enough, since a flag is its own token.
    tokens = probe.command_line(pid).split()[1:]
    return any(token.lower() in SHELL_COMMAND_FLAGS for token in tokens)


def pick_client_pid(own_pid, ppid, probe, prefix, base_prefix):
    """The client above this launcher: `ppid`, minus a venv redirector, uv and wrapper shells.

    Pure over `probe`. An unreadable link in the chain keeps the last pid that was.
    """
    pid = ppid
    if prefix != base_prefix and _is_venv_redirector(own_pid, pid, probe, prefix):
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


@functools.lru_cache(maxsize=1)
def client_pid():
    """The client's pid, computed once: every request and timing report sends it."""
    ppid = os.getppid()
    if not _IS_WINDOWS:
        return ppid
    try:
        return pick_client_pid(os.getpid(), ppid, _WindowsProbe(), sys.prefix, sys.base_prefix)
    except Exception:
        return ppid
