"""Which interpreter runs the shared server, and the environment behind it.

The server's dependencies are declared in the PEP 723 block of server/server.py and pinned
in server/server.py.lock. uv builds them into a persistent environment in the state
directory, `<state dir>/server-env` (%LOCALAPPDATA%\\browserplug\\server-env; BROWSERPLUG_STATE_DIR
moves it), with `uv sync --active --script server/server.py --locked` and VIRTUAL_ENV naming
that directory. `ensure_server_python` makes that environment exist and match the lock, checks
that it really works, and returns its python. A spawner then starts that python detached: the
env's `Scripts/python.exe` is a uv trampoline that runs the real interpreter as its child, so
the server is the documented process pair, with no uv.exe above it.

The environment lives outside uv's cache so that `uv cache prune` and `uv cache clean` cannot
reach it. uv hard-links installed files from its cache, so a prune drops the cache's link and
leaves the environment's own. UV_LINK_MODE is never passed on: a symlink mode would make the
environment depend on the cache again. Its pyvenv.cfg `home` is uv's minor-version link
(`cpython-3.12-<platform>`), so a `uv python upgrade` moves it to the new patch release.

Why every step is checked rather than trusted (probed on uv 0.12.18, Windows 11):

  * Given a VIRTUAL_ENV with no pyvenv.cfg, `uv sync --active` takes the trampoline's base
    interpreter for the environment and installs into the base Python itself. So a directory
    that is not a whole environment (pyvenv.cfg, site-packages, the python) is deleted, never
    synced, and every sync is preceded by the read-only `--check`, whose first line must name
    this directory.
  * A missing directory is fine: `uv sync --active` creates it with the interpreter the
    block's requires-python names, and replaces one built on another interpreter.
  * The environment is synced only while nothing runs from it. Swapping packages under a
    running server breaks its next lazy import, and an interpreter change makes uv delete
    Lib/ before it fails on the locked trampoline. On Windows a running process holds its
    Scripts/python.exe open for writing; on Linux, /proc shows a process that runs or maps
    files from the directory. Anything held is refused with the remedy, never synced.
  * The import probe runs with the caller's variables, which the spawned server inherits.
    When it fails, the same probe runs under `python -I` (no PYTHON* variables, no cwd on
    sys.path). Only a failure there is damage; one caused by a PYTHONPATH or a
    PYTHONWARNINGS is reported and nothing is deleted, because a rebuild cannot fix it.
  * A build or repair is serialized by a lock file next to the environment: launchers that
    start together and all see the same work do it once, and the others wait and find it
    done.
  * A /restart (`python_for_restart`) never syncs the environment it runs from: it asks the
    read-only `--check` and refuses the restart when anything would change.
  * The launcher's stdout is the MCP JSON-RPC channel, so every uv call captures both
    streams. The caller's cwd could hold a uv.toml that moves the cache or the index, so uv
    always runs from the checkout root.

Stdlib-only: the launcher imports this, and it runs before any server dependency exists.
"""

import contextlib
import functools
import glob
import os
import re
import shutil
import subprocess
import sys
import time

from browserplug_shared import proc_facts
from browserplug_shared.config_store import state_dir

# The hermetic seam. When set, it IS the interpreter: no uv call, no check. The unit
# sandbox sets it to sys.executable (tests/run.py), so no test builds or mutates the user's
# real server environment.
SERVER_PYTHON_ENV = "BROWSERPLUG_SERVER_PYTHON"
# The uv a spawner resolved. server_spawn_env exports it, so a /restart finds the same uv
# even when the client named uv by an absolute path that is not on PATH.
UV_ENV = "BROWSERPLUG_UV"

# The environment's directory under the state directory.
SERVER_ENV_DIR_NAME = "server-env"

SYNC_TIMEOUT_SECONDS = 120.0
FIND_TIMEOUT_SECONDS = 15.0
PROBE_TIMEOUT_SECONDS = 60.0
# How long a launcher waits for a peer that holds the build lock: the peer's assessment, its
# sync and its re-assessment (two checks, a sync and two rounds of import probes) at their
# limits.
REPAIR_LOCK_TIMEOUT_SECONDS = 3 * SYNC_TIMEOUT_SECONDS + 4 * PROBE_TIMEOUT_SECONDS
# A peer's import probe holds the env's python for a second or two. Something that still
# holds it after this long is a running process, and the environment counts as in use.
IN_USE_GRACE_SECONDS = 5.0

# What the server imports first. A gutted environment fails here, not at the server's start
# where the only witness is a startup log nobody reads.
PROBE_IMPORTS = ("mcp", "websockets", "uvicorn", "pydantic", "psutil", "cryptography")

LOCK_COMMAND = "uv lock --script server/server.py"
# Run from the checkout with VIRTUAL_ENV set to the environment's directory.
SYNC_COMMAND = "uv sync --active --script server/server.py --locked"
SYNC_CHECK_COMMAND = SYNC_COMMAND + " --check"
# The one command that builds, updates or repairs the environment and starts the server.
START_COMMAND = "python server/control.py start"
# What to do instead when uv is not installed: the README's step 1. Appended to every remedy
# that would otherwise make uv the only way.
NO_UV_ROUTE = (f"skip uv: make a venv with Python 3.11 or 3.12, run `<venv python> -m pip "
               f"install --requirements-from-script server/server.py` in the checkout, and set "
               f"{SERVER_PYTHON_ENV} to that venv's python in the MCP client's env block "
               f"(README, step 1)")

_STOP_AND_START = (f"stop the server (python server/control.py shutdown), then run "
                   f"`{START_COMMAND}` or reconnect an MCP client: either one rebuilds the "
                   f"environment before it starts the server")

# Removed from the environment of every uv call. A launcher run under `uv run` inherits
# VIRTUAL_ENV (its own env; each check and sync sets it to the server's explicitly) and the
# recursion depth. A client's UV_PYTHON or UV_PROJECT_ENVIRONMENT would re-target or recreate
# the server's environment, and a UV_LINK_MODE of symlink would tie it to uv's cache.
_UV_ENV_REMOVED = frozenset({
    "VIRTUAL_ENV", "UV_PYTHON", "UV_PROJECT_ENVIRONMENT", "UV_LOCKED", "UV_FROZEN",
    "UV_NO_SYNC", "UV_RUN_RECURSION_DEPTH", "UV_LINK_MODE",
})
# Also removed for the import probe, because mcp_port.server_spawn_env never hands them to
# the server, nor the launcher to its proxy child (mcp_port.SPAWN_ENV_DENIED; a unit test
# keeps the two equal): the probe sees the PYTHON* variables the spawned server will see.
_PROBE_ENV_REMOVED = frozenset({"PYTHONHOME", "PYTHONEXECUTABLE"})
# uv colours its output when FORCE_COLOR or CLICOLOR_FORCE is set to anything, "0" included,
# and a coloured first line no longer starts with the marker the parser looks for. Node tools
# set FORCE_COLOR for their children. NO_COLOR=1 wins over both in uv; they go as well so a
# future uv that ranks them differently still prints plain text.
_UV_COLOR_REMOVED = frozenset({"FORCE_COLOR", "CLICOLOR_FORCE", "NO_COLOR"})
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")

_CREATE_NO_WINDOW = 0x08000000   # mcp_port.CREATE_NO_WINDOW; see detached_spawn_kwargs there

# The first line of `uv sync --check`: which environment, and what uv would do to it.
# "replace" is an interpreter change (a requires-python or base-interpreter bump).
_ENV_MARKERS = (("use", "Would use script environment at:"),
                ("create", "Would create script environment at:"),
                ("replace", "Would replace script environment at:"))
# The first line of a real `uv sync`.
_SYNC_MARKERS = (("use", "Using script environment at:"),
                 ("create", "Creating script environment at:"),
                 ("replace", "Updating script environment at:"))


class ServerEnvError(RuntimeError):
    """No interpreter for the server. str() is the whole message, remedy included.

    `kind` names the failure for callers and tests: uv_missing, override_missing,
    lock_missing, lock_stale, sync_failed, timeout, not_a_script_env (uv names another
    environment than the server's), not_built (no environment to join), damaged,
    damaged_in_use, env_in_use (it needs a sync and a process runs from it),
    env_vars_break_imports (the environment is whole, the caller's PYTHON* variables break
    its imports), deps_changed (a /restart from an environment that would change).
    """

    def __init__(self, message, *, kind, remedy=None, uv_output=""):
        self.kind = kind
        self.remedy = remedy
        self.uv_output = uv_output
        self.message = message.rstrip(".")      # the failure alone, without uv's words or the fix
        text = self.message + "."
        if uv_output:
            text += f" uv said: {uv_output}"
        if remedy:
            text += f" Fix: {remedy}"
        super().__init__(text)


# ---- small helpers -------------------------------------------------------------------

def _get(environ, name):
    """environ[name], matching the name case-insensitively on Windows (a plain dict of a
    Windows environment spells it `Path` as often as `PATH`)."""
    if name in environ:
        return environ[name]
    if os.name == "nt":
        upper = name.upper()
        for key, value in environ.items():
            if key.upper() == upper:
                return value
    return None


def _norm(path):
    """normcase(realpath()): on Windows realpath also expands an 8.3 short name, including in
    the existing part of a path whose tail does not exist yet."""
    return os.path.normcase(os.path.realpath(path))


def _tail(text, max_lines=6, max_chars=600):
    """The end of uv's stderr as one line, minus the advice uv gives for the wrong case: its
    `uv lock` hint (the lock is per script) and `--check`'s "run `uv sync`" (which, under a
    running server, is the very thing that guts it)."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    lines = [ln for ln in lines
             if not (ln.lower().startswith("hint:") and "uv lock" in ln)
             and "environment is outdated" not in ln.lower()]
    out = " | ".join(lines[-max_lines:])
    return out[-max_chars:]


def server_script(project_root):
    """The script uv reads the dependencies from: realpath, so every spelling agrees."""
    return os.path.realpath(os.path.join(project_root, "server", "server.py"))


def server_env_dir():
    """Where the server's environment lives: `<state dir>/server-env`."""
    return os.path.join(state_dir(), SERVER_ENV_DIR_NAME)


def _python_in(env_dir):
    sub = ("Scripts", "python.exe") if os.name == "nt" else ("bin", "python")
    return os.path.join(env_dir, *sub)


def server_env_python(env_dir=None):
    """The python of the server environment (default: server_env_dir())."""
    return _python_in(server_env_dir() if env_dir is None else env_dir)


def env_dir_of(python):
    """The environment directory a `Scripts/python.exe` (or `bin/python`) belongs to."""
    return os.path.dirname(os.path.dirname(python))


def _site_packages(env_dir):
    """The environment's site-packages directory, or None when it is gone."""
    if os.name == "nt":
        candidates = [os.path.join(env_dir, "Lib", "site-packages")]
    else:
        candidates = glob.glob(os.path.join(glob.escape(env_dir), "lib", "python*",
                                            "site-packages"))
    return next((path for path in candidates if os.path.isdir(path)), None)


def _structural_damage(env_dir):
    """Why `env_dir` is not a whole environment, judged by its files alone; None if it is."""
    if not os.path.isfile(os.path.join(env_dir, "pyvenv.cfg")):
        return "it has no pyvenv.cfg"
    if _site_packages(env_dir) is None:
        return ("it has no site-packages (a uv replace that failed under a running process "
                "removes it)")
    python = _python_in(env_dir)
    if not os.path.isfile(python):
        return f"it has no {os.path.relpath(python, env_dir)}"
    return None


def _marker(stderr, markers):
    """(action, env dir) from the first line of a `uv sync`; (None, None) when uv named no
    environment."""
    for line in (stderr or "").splitlines():
        line = line.strip()
        for action, marker in markers:
            if line.startswith(marker):
                return action, line[len(marker):].strip()
    return None, None


def _env_marker(stderr):
    return _marker(stderr, _ENV_MARKERS)


def _outdated(code, stderr):
    """`uv sync --check` exits 1 with this line when it would change the environment; any
    other non-zero exit is a failure to answer."""
    return code != 0 and "environment is outdated" in (stderr or "").lower()


# ---- finding uv ----------------------------------------------------------------------

def _uv_names():
    return ("uv.exe",) if os.name == "nt" else ("uv",)


def _standard_uv_dirs(environ, explicit):
    home = _get(environ, "USERPROFILE") or _get(environ, "HOME")
    if not home and not explicit:
        home = os.path.expanduser("~")
    dirs = []
    xdg_bin = _get(environ, "XDG_BIN_HOME")
    if xdg_bin:
        dirs.append(xdg_bin)
    if home:
        dirs += [os.path.join(home, ".local", "bin"), os.path.join(home, ".cargo", "bin")]
    if os.name == "nt":
        local = _get(environ, "LOCALAPPDATA")
        if local:
            dirs.append(os.path.join(local, "Microsoft", "WinGet", "Links"))
    elif not explicit:
        dirs += ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin"]
    return dirs


def find_uv(environ=None):
    """The uv executable: BROWSERPLUG_UV, then UV (uv run sets it for its child), then PATH,
    then the standard install directories. None when there is none.

    PATH is walked by hand rather than with shutil.which, which on Windows looks in the
    current directory first: the launcher's cwd is the MCP client's project, and a uv.exe
    lying there is not ours to run.
    """
    explicit = environ is not None
    environ = os.environ if environ is None else environ
    for name in (UV_ENV, "UV"):
        candidate = (_get(environ, name) or "").strip().strip('"')
        if candidate and os.path.isfile(candidate):
            return candidate
    for directory in (_get(environ, "PATH") or "").split(os.pathsep):
        directory = directory.strip().strip('"')
        if not directory or not os.path.isabs(directory):
            continue
        for exe in _uv_names():
            candidate = os.path.join(directory, exe)
            if os.path.isfile(candidate):
                return candidate
    for directory in _standard_uv_dirs(environ, explicit):
        for exe in _uv_names():
            candidate = os.path.join(directory, exe)
            if os.path.isfile(candidate):
                return candidate
    return None


def _require_uv(environ):
    uv = find_uv(environ)
    if uv is None:
        raise ServerEnvError(
            "uv was not found (looked at BROWSERPLUG_UV, UV, PATH and ~/.local/bin)",
            kind="uv_missing",
            remedy="install uv (https://docs.astral.sh/uv/getting-started/installation/) or "
                   f"set {UV_ENV} to its full path, or {NO_UV_ROUTE}; then reconnect the "
                   f"MCP client")
    return uv


def uv_subprocess_env(environ=None):
    """The environment a uv call runs with: the caller's, minus _UV_ENV_REMOVED, with colour
    off (uv's output is parsed, so it must be plain text)."""
    source = os.environ if environ is None else environ
    out = {name: value for name, value in source.items()
           if name.upper() not in _UV_ENV_REMOVED | _UV_COLOR_REMOVED}
    out["NO_COLOR"] = "1"
    return out


def _active_env(env, env_dir):
    """`env` with VIRTUAL_ENV naming the server environment, for `uv sync --active`."""
    return {**env, "VIRTUAL_ENV": env_dir}


# ---- running uv ----------------------------------------------------------------------

def _run(argv, *, cwd, timeout, env):
    """(exit code, stdout, stderr) of one captured, windowless, stdin-less call.

    subprocess.TimeoutExpired and OSError propagate; the callers turn them into errors."""
    kwargs = dict(cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                  stderr=subprocess.PIPE, env=env, timeout=timeout,
                  text=True, encoding="utf-8", errors="replace")
    if os.name == "nt":
        kwargs["creationflags"] = _CREATE_NO_WINDOW
    proc = subprocess.run(argv, **kwargs)
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _call(runner, argv, *, root, timeout, env, what, deadline=None):
    """One call through `runner`. With a `deadline` (time.monotonic()), the call's timeout
    is cut to what is left of it, so a caller's bound holds however many calls it takes."""
    if deadline is not None:
        timeout = min(timeout, deadline - time.monotonic())
        if timeout <= 0:
            raise ServerEnvError(f"`{what}` was not started: the time allowed ran out",
                                 kind="timeout",
                                 remedy=f"run `{what}` from {root} to see how long it takes, "
                                        f"then retry")
    try:
        code, out, err = runner(argv, cwd=root, timeout=timeout, env=env)
        # Belt and braces for a uv that colours anyway (a --color default in uv.toml): the
        # parsers below match plain text.
        return code, _ANSI_ESCAPE.sub("", out), _ANSI_ESCAPE.sub("", err)
    except subprocess.TimeoutExpired:
        raise ServerEnvError(f"`{what}` did not finish within {timeout:.0f} s",
                             kind="timeout",
                             remedy=f"run `{what}` from {root} to see where it stalls "
                                    f"(a first build downloads packages), then retry") from None
    except OSError as exc:
        raise ServerEnvError(f"could not run {argv[0]}: {exc}", kind="uv_missing",
                             remedy=f"install uv or set {UV_ENV} to uv's full path, or "
                                    f"{NO_UV_ROUTE}") from None


def _in_use_error(text):
    """uv failing on a file a running process holds (Windows os error 5 or 32)."""
    text = text.lower()
    return any(marker in text for marker in ("os error 5)", "os error 32)", "access is denied",
                                             "being used by another process"))


# What uv says when the environment's python is a zero-byte, truncated or garbage file (a
# launcher killed while uv wrote it, a full disk, a quarantined file). Checked on the whole
# stderr, not the tail ServerEnvError.uv_output keeps.
_UNRUNNABLE_PYTHON = ("failed to inspect python interpreter",
                      "failed to query python interpreter")


def _sync_error(code, stderr, root, env_dir, *, checked=False):
    """The ServerEnvError for a failed `uv sync`. `checked`: it was the read-only --check,
    so the remedy repeats that and never the mutating sync."""
    out = _tail(stderr)
    text = (stderr or "").lower()
    if "requires a script lockfile" in text:
        return ServerEnvError("server/server.py has no lock file (server/server.py.lock)",
                              kind="lock_missing", uv_output=out,
                              remedy=f"run `{LOCK_COMMAND}` from {root}, then retry")
    if "needs to be updated" in text and ("--locked" in text or "--check" in text):
        return ServerEnvError("server/server.py.lock does not match the dependencies "
                              "declared in server/server.py", kind="lock_stale",
                              uv_output=out,
                              remedy=f"run `{LOCK_COMMAND}` from {root}, then retry")
    if any(marker in text for marker in _UNRUNNABLE_PYTHON):
        # The environment's own python is truncated or garbage; only a rebuild helps.
        error = ServerEnvError(
            f"uv cannot run the server environment's python at {_python_in(env_dir)}",
            kind="sync_failed", uv_output=out,
            remedy=f"delete {env_dir}, then run `{START_COMMAND}`")
        error.interpreter_unusable = True
        return error
    if _in_use_error(text):
        # Re-running the sync hits the same lock: only stopping the holder helps.
        return ServerEnvError("`uv sync` could not change the server environment because a "
                              "running process holds its files", kind="sync_failed",
                              uv_output=out, remedy=_STOP_AND_START)
    command = SYNC_CHECK_COMMAND if checked else SYNC_COMMAND
    return ServerEnvError(f"`uv sync` of the server environment failed with exit code {code}",
                          kind="sync_failed", uv_output=out,
                          remedy=f"run `{command}` from {root} with VIRTUAL_ENV={env_dir} to "
                                 f"see the whole error")


def _probe(python, imports, call, env, *, isolated=False):
    """None when `python` imports `imports`, else why not. A timeout raises."""
    statement = "import " + ", ".join(imports)
    flags = ["-I"] if isolated else []
    code, _out, err = call([python, *flags, "-c", statement], timeout=PROBE_TIMEOUT_SECONDS,
                           env=env, what=f"{python} {' '.join(flags + ['-c'])} \"{statement}\"")
    if code == 0:
        return None
    return f"`{statement}` failed with exit code {code}: {_tail(err, max_lines=3)}"


def _import_damage(python, imports, call, env):
    """Why the environment's imports fail, or None when they work.

    First with the variables the spawned server will inherit; only when that fails, the same
    probe under `-I`. Raises env_vars_break_imports when only the variables break it: a
    rebuild would delete a healthy environment."""
    if not imports:
        return None
    probe_env = {name: value for name, value in env.items()
                 if name.upper() not in _PROBE_ENV_REMOVED}
    failed = _probe(python, imports, call, probe_env)
    if failed is None:
        return None
    isolated = _probe(python, imports, call, probe_env, isolated=True)
    if isolated is not None:
        return isolated
    names = sorted(name for name in probe_env if name.upper().startswith("PYTHON"))
    raise ServerEnvError(
        f"the server environment at {env_dir_of(python)} is intact, but {failed}; under "
        f"`python -I` the same imports work, so a variable this process passes on (and the "
        f"server would inherit) breaks them", kind="env_vars_break_imports",
        remedy=f"check the PYTHON* variables the MCP client starts BrowserPlug with (set "
               f"here: {', '.join(names) or 'none'}); a PYTHONPATH that shadows a package and "
               f"PYTHONWARNINGS=error are the usual causes. Remove the culprit, then reconnect. "
               f"Nothing was deleted")


def _damaged_in_use(env_dir, problem):
    return ServerEnvError(f"the server environment at {env_dir} is damaged and a running "
                          f"process holds it: {problem}", kind="damaged_in_use",
                          remedy=_STOP_AND_START)


def _not_ours(named, env_dir, what, *, synced=False):
    done = ("uv may have changed that environment instead" if synced
            else "nothing was synced")
    return ServerEnvError(
        f"`{what}` named {named}, not the server environment at {env_dir}; {done}",
        kind="not_a_script_env",
        remedy=f"delete {env_dir} if it is there, check UV_* variables and any uv.toml, then "
               f"run `{START_COMMAND}`")


def _not_understood(err, env_dir, what, root):
    """uv answered, but with no line naming an environment: its output is not what the
    parser knows (a newer uv, or a wrapper that rewrites stderr). Blaming UV_* variables
    would send the user to delete a healthy environment."""
    first = next((ln.strip() for ln in (err or "").splitlines() if ln.strip()), "(nothing)")
    return ServerEnvError(
        f"uv's output for `{what}` was not understood: it names no script environment "
        f"(first line: {first[:200]})", kind="not_a_script_env",
        remedy=f"run `{SYNC_CHECK_COMMAND}` from {root} with VIRTUAL_ENV={env_dir} and read "
               f"what uv prints; nothing was synced")


# ---- is anything running from the environment? -----------------------------------------

def _linux_holder(env_dir, proc_root="/proc"):
    """A pid whose argv[0] or memory maps name a file under `env_dir`, or None."""
    prefix = env_dir.rstrip("/") + "/"
    try:
        pids = [name for name in os.listdir(proc_root) if name.isdigit()]
    except OSError:
        return None
    for pid in pids:
        try:
            with open(os.path.join(proc_root, pid, "cmdline"), "rb") as fh:
                argv0 = os.fsdecode(fh.read().split(b"\0", 1)[0])
            if argv0.startswith(prefix):
                return int(pid)
            with open(os.path.join(proc_root, pid, "maps"), "r", encoding="utf-8",
                      errors="replace") as fh:
                if any(prefix in line for line in fh):
                    return int(pid)
        except OSError:
            continue                 # gone, or another user's: not ours to see
    return None


def _on_windows():
    return os.name == "nt"


def env_in_use(env_dir):
    """What runs from `env_dir`, or None when nothing is seen to.

    Windows: a process whose image is the env's Scripts/python.exe (the trampoline every
    process from this env starts through), found from the process list. The exe is never
    opened: a write-open of it, which was the test before, makes a concurrent CreateProcess
    of the same file fail with a sharing violation. Linux: /proc. macOS and the BSDs: psutil
    when the interpreter has it (the uv-run launcher form installs it there), else only this
    process is known."""
    if _norm(sys.prefix) == _norm(env_dir):
        return "this process runs from it"
    python = _python_in(env_dir)
    if _on_windows():
        try:
            pids = proc_facts.pids_running_image(python)
        except (OSError, proc_facts.ProbeUnavailable):
            return None
        if pids:
            return (f"pid {pids[0]} runs its {os.path.relpath(python, env_dir)}")
        return None
    if sys.platform.startswith("linux"):
        pid = _linux_holder(os.path.realpath(env_dir))
        return f"pid {pid} runs from it" if pid is not None else None
    pid = proc_facts.psutil_holder(env_dir)
    return f"pid {pid} runs from it" if pid is not None else None


def _wait_unused(env_dir):
    """None once nothing holds `env_dir`, or what still holds it after IN_USE_GRACE_SECONDS
    (a peer's import probe lets go within that)."""
    give_up = time.monotonic() + IN_USE_GRACE_SECONDS
    while True:
        holder = env_in_use(env_dir)
        if holder is None or time.monotonic() >= give_up:
            return holder
        time.sleep(0.25)


# ---- building and repairing -------------------------------------------------------------

def _try_lock(fd):
    try:
        if os.name == "nt":
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(fd):
    try:
        if os.name == "nt":
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


def repair_lock_path(env_dir):
    """The lock file that serializes builds and repairs of `env_dir`: `<env_dir>.lock`,
    beside it in the state directory."""
    return env_dir.rstrip("\\/") + ".lock"


@contextlib.contextmanager
def _repair_lock(env_dir, deadline=None):
    """Hold the build lock of `env_dir`. Locks are per open file (LockFileEx, flock), so
    threads of one process exclude each other too. An unwritable state directory builds
    unlocked rather than not at all: the delete still tolerates a peer's."""
    wait = REPAIR_LOCK_TIMEOUT_SECONDS
    if deadline is not None:
        wait = max(0.0, min(wait, deadline - time.monotonic()))
    path = repair_lock_path(env_dir)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        fd = None
    if fd is None:
        yield
        return
    try:
        give_up = time.monotonic() + wait
        while not _try_lock(fd):
            if time.monotonic() >= give_up:
                raise ServerEnvError(
                    f"another launcher has been building the server environment at "
                    f"{env_dir} for over {wait:.0f} s", kind="timeout",
                    remedy=f"reconnect the MCP client; if this repeats, stop every BrowserPlug "
                           f"process and delete {env_dir}")
            time.sleep(0.1)
        try:
            yield
        finally:
            _unlock(fd)
    finally:
        os.close(fd)


def _skip_vanished(_func, _path, exc):
    """rmtree's error hook: a file already gone was a peer's delete, so carry on."""
    if not isinstance(exc, FileNotFoundError):
        raise exc


def _delete_env(env_dir, problem):
    """Delete a damaged environment, or raise damaged_in_use.

    The python goes first. On Windows a running process (a server, a /restart replacement
    waiting for ports) holds its Scripts/python.exe open, so a PermissionError there means
    in use before anything that process still imports has been removed. Files that vanish
    meanwhile were a peer's doing; only a PermissionError means in use."""
    python = _python_in(env_dir)
    if os.name == "nt":
        give_up = time.monotonic() + IN_USE_GRACE_SECONDS
        while True:
            try:
                os.remove(python)
                break
            except FileNotFoundError:
                break
            except PermissionError as exc:
                if time.monotonic() >= give_up:
                    raise _damaged_in_use(env_dir, f"{problem}; its python is in use "
                                                   f"({exc})") from None
                time.sleep(0.25)
    try:
        if sys.version_info >= (3, 12):
            shutil.rmtree(env_dir, onexc=_skip_vanished)
        else:
            shutil.rmtree(env_dir, onerror=lambda f, p, info: _skip_vanished(f, p, info[1]))
    except FileNotFoundError:
        pass
    except PermissionError as exc:
        raise _damaged_in_use(env_dir, f"{problem}; deleting it failed: {exc}") from None
    except OSError as exc:
        raise ServerEnvError(f"the server environment at {env_dir} is damaged ({problem}) "
                             f"and deleting it failed: {exc}", kind="damaged",
                             remedy=f"delete {env_dir} by hand, then run "
                                    f"`{START_COMMAND}`") from None


# ---- the entry points ----------------------------------------------------------------

def _override(environ):
    """BROWSERPLUG_SERVER_PYTHON, or None. Raises override_missing for a stale one."""
    override = (_get(environ, SERVER_PYTHON_ENV) or "").strip()
    if override and not os.path.isfile(override):
        raise ServerEnvError(f"{SERVER_PYTHON_ENV} names {override}, which does not exist",
                             kind="override_missing",
                             remedy=f"unset {SERVER_PYTHON_ENV} (uv then builds the "
                                    f"environment), or point it at a python that has the "
                                    f"server's dependencies (README, step 1)")
    return override or None


class _Env:
    """One ensure_server_python call: where things are and how uv is run."""

    def __init__(self, root, uv, call, env, env_dir, probe_imports, note):
        self.root, self.uv, self.call, self.env = root, uv, call, env
        self.env_dir, self.python = env_dir, _python_in(env_dir)
        self.script = server_script(root)
        self.probe_imports, self.note = probe_imports, note

    def check(self):
        """(exit code, stderr, action) of the read-only `--check`, after making sure uv named
        this environment. Raises lock and check failures."""
        code, _out, err = self.call(
            [self.uv, "sync", "--active", "--script", self.script, "--locked", "--check"],
            timeout=SYNC_TIMEOUT_SECONDS, env=_active_env(self.env, self.env_dir),
            what=SYNC_CHECK_COMMAND)
        if code != 0 and not _outdated(code, err):
            raise _sync_error(code, err, self.root, self.env_dir, checked=True)
        action, named = _env_marker(err)
        if named is None:
            raise _not_understood(err, self.env_dir, SYNC_CHECK_COMMAND, self.root)
        if _norm(named) != _norm(self.env_dir):
            raise _not_ours(named, self.env_dir, SYNC_CHECK_COMMAND)
        return code, err, action

    def assess(self):
        """("ok", None), ("stale", why) or ("damaged", why), changing nothing."""
        if os.path.lexists(self.env_dir):
            problem = _structural_damage(self.env_dir)
            if problem:
                return "damaged", problem
        try:
            code, _err, action = self.check()
        except ServerEnvError as exc:
            if getattr(exc, "interpreter_unusable", False):
                return "damaged", f"uv cannot run its python ({exc.uv_output})"
            raise
        if action == "create":
            return "stale", "it has not been built"
        if action == "replace":
            return "stale", "the lock names another Python than it was built on"
        if code != 0:
            return "stale", "its packages differ from the lock"
        problem = _import_damage(self.python, self.probe_imports, self.call, self.env)
        return ("damaged", problem) if problem else ("ok", None)

    def sync(self):
        started = time.monotonic()
        code, _out, err = self.call(
            [self.uv, "sync", "--active", "--script", self.script, "--locked"],
            timeout=SYNC_TIMEOUT_SECONDS, env=_active_env(self.env, self.env_dir),
            what=SYNC_COMMAND)
        _action, named = _marker(err, _SYNC_MARKERS)
        if named is not None and _norm(named) != _norm(self.env_dir):
            raise _not_ours(named, self.env_dir, SYNC_COMMAND, synced=True)
        if code != 0:
            raise _sync_error(code, err, self.root, self.env_dir)
        if "Creating script environment" in err or "Installed " in err:
            self.note(f"built the server environment at {self.env_dir} in "
                      f"{time.monotonic() - started:.1f} s ({_tail(err, max_lines=2)})")


def ensure_server_python(project_root, *, environ=None, log=None, runner=None,
                         probe_imports=PROBE_IMPORTS, deadline=None):
    """The python to run server/server.py with, building, updating or repairing its
    environment (server_env_dir()) when nothing runs from it.

    Order: BROWSERPLUG_SERVER_PYTHON (returned as is); otherwise the environment is
    assessed read-only: its files (pyvenv.cfg, site-packages, the python), then
    `uv sync --check`, whose answer must name this directory, then `probe_imports`. Anything
    short of that is done under the build lock, and only once nothing runs from the
    environment: a damaged one is deleted and rebuilt, a stale one synced. Raises
    ServerEnvError, whose text names the remedy: env_in_use when it needs work and a process
    runs from it.

    `environ` is read for the seam and uv's location and passed to uv (minus
    _UV_ENV_REMOVED); `log(str)` gets the slow or unusual events (a build, a repair);
    `runner(argv, *, cwd, timeout, env) -> (code, stdout, stderr)` replaces the subprocess
    layer in tests; `deadline` (time.monotonic()) bounds the whole call, lock wait included.
    """
    environ = os.environ if environ is None else environ
    override = _override(environ)
    if override:
        return override

    root = os.path.realpath(project_root)
    uv = _require_uv(environ)
    env_dir = server_env_dir()
    os.makedirs(os.path.dirname(env_dir), exist_ok=True)
    call = functools.partial(_call, runner or _run, root=root, deadline=deadline)
    work = _Env(root, uv, call, uv_subprocess_env(environ), env_dir, probe_imports,
                log or (lambda message: None))

    state, why = work.assess()
    if state == "ok":
        return work.python
    with _repair_lock(env_dir, deadline):
        # Whoever held the lock before may have done the work already.
        state, why = work.assess()
        if state == "ok":
            return work.python
        holder = _wait_unused(env_dir)
        if holder is not None:
            if state == "damaged":
                raise _damaged_in_use(env_dir, f"{why}; {holder}")
            raise ServerEnvError(
                f"the server environment at {env_dir} needs `uv sync` ({why}), but {holder}; "
                f"it is changed only while nothing runs from it", kind="env_in_use",
                remedy=_STOP_AND_START)
        if state == "damaged":
            work.note(f"the server environment at {env_dir} is damaged ({why}); deleting and "
                      f"rebuilding it")
            _delete_env(env_dir, why)
        work.sync()
        state, why = work.assess()
        if state == "ok":
            return work.python
    raise ServerEnvError(f"the server environment at {env_dir} is still not usable after "
                         f"`uv sync`: {why}", kind="damaged",
                         remedy=f"delete {env_dir}, run `{START_COMMAND}` and read its output")


def existing_server_python(project_root, *, environ=None, deadline=None):
    """The server environment's python as it stands, for a process that joins a server
    already running from it (the launcher's proxy transport). Changes nothing and runs
    nothing: the directory is known, so only its files are checked (pyvenv.cfg,
    site-packages, the python). `deadline` is accepted for the callers' symmetry with
    ensure_server_python; nothing here waits.

    Raises ServerEnvError: not_built when there is no environment (so the running server
    does not run from it), damaged_in_use when it is damaged; the remedy is to stop the
    server and start it again, which rebuilds it."""
    environ = os.environ if environ is None else environ
    override = _override(environ)
    if override:
        return override
    env_dir = server_env_dir()
    if not os.path.isdir(env_dir):
        raise ServerEnvError(f"the server environment at {env_dir} has not been built, so "
                             f"the running server does not run from it", kind="not_built",
                             remedy=_STOP_AND_START)
    damage = _structural_damage(env_dir)
    if damage:
        raise _damaged_in_use(env_dir, damage)
    return _python_in(env_dir)


def python_for_restart(project_root, *, running_prefix=None, environ=None, log=None,
                       runner=None, probe_imports=PROBE_IMPORTS, deadline=None):
    """The python a /restart replacement runs on, found without changing the environment
    the running server uses.

    When `running_prefix` (default sys.prefix) IS the server environment, only read-only uv
    calls run. Nothing to change: its own python, unsynced. Any change (a package, or a
    "Would replace", which is an interpreter change) raises deps_changed: the next start,
    after a shutdown, applies it. A damaged environment raises damaged_in_use. Everything
    else (the seam, a server running from any other interpreter) is ensure_server_python,
    which syncs the environment only while nothing runs from it.
    """
    environ = os.environ if environ is None else environ
    delegate = functools.partial(ensure_server_python, project_root, environ=environ, log=log,
                                 runner=runner, probe_imports=probe_imports, deadline=deadline)
    if (_get(environ, SERVER_PYTHON_ENV) or "").strip():
        return delegate()
    env_dir = server_env_dir()
    prefix = sys.prefix if running_prefix is None else running_prefix
    if _norm(prefix) != _norm(env_dir):
        return delegate()

    root = os.path.realpath(project_root)
    damage = _structural_damage(env_dir)
    if damage:
        raise _damaged_in_use(env_dir, damage)
    uv = _require_uv(environ)
    call = functools.partial(_call, runner or _run, root=root, deadline=deadline)
    work = _Env(root, uv, call, uv_subprocess_env(environ), env_dir, probe_imports,
                log or (lambda message: None))
    try:
        code, err, action = work.check()
    except ServerEnvError as exc:
        if getattr(exc, "interpreter_unusable", False):
            raise _damaged_in_use(env_dir, f"uv cannot run its python ({exc.uv_output})") \
                from None
        raise
    if code == 0 and action == "use":
        return work.python
    # The script and its lock disagree: the fix is a relock, not a stop.
    lock_code, _out, lock_err = call([uv, "lock", "--check", "--script", work.script],
                                     timeout=FIND_TIMEOUT_SECONDS, env=work.env,
                                     what="uv lock --check --script server/server.py")
    if lock_code != 0 and "needs to be updated" in lock_err.lower():
        raise _sync_error(lock_code, lock_err, root, env_dir, checked=True)
    why = {"replace": "uv would replace it, because the lock names another Python",
           "create": "uv would create it anew"}.get(action, "its packages differ from the lock")
    raise ServerEnvError(
        f"the server environment at {env_dir} would change ({why}), and this server runs "
        f"from it; a /restart never changes the environment under a running server",
        kind="deps_changed", uv_output=_tail(err),
        remedy=f"stop the server (python server/control.py shutdown), then run "
               f"`{START_COMMAND}` or reconnect an MCP client: either one updates the "
               f"environment before it starts the server")


def inspect_server_env(project_root, *, environ=None, runner=None):
    """What ensure_server_python would start, found WITHOUT changing anything (preflight).

    Never raises. Read-only uv calls only: `uv lock --check` and `uv sync --locked --check`.
    Returns {mode, python, uv, script, lock_current, env_current, env_state, env_dir,
    problem, remedy, uv_output}; `mode` is override or uv, and `problem` is None when the
    environment looks usable (the caller still probes its imports). `env_state` says what
    the next start would do to it: current, not_built, out_of_date or replace (all three of
    which the start fixes by itself while nothing runs from it), damaged, or error.
    """
    environ = os.environ if environ is None else environ
    state = {"mode": None, "python": None, "uv": None, "script": None, "lock_current": None,
             "env_current": None, "env_state": None, "env_dir": None, "problem": None,
             "remedy": None, "uv_output": ""}
    try:
        override = (_get(environ, SERVER_PYTHON_ENV) or "").strip()
        if override:
            state.update(mode="override", python=override)
            if not os.path.isfile(override):
                state.update(
                    problem=f"{SERVER_PYTHON_ENV} names {override}, which does not exist",
                    remedy=f"unset {SERVER_PYTHON_ENV} (uv then builds the environment), or "
                           f"point it at a python that has the server's dependencies "
                           f"(README, step 1)")
            return state
        root = os.path.realpath(project_root)
        script = server_script(root)
        env_dir = server_env_dir()
        state.update(mode="uv", script=script, env_dir=env_dir)
        uv = find_uv(environ)
        state["uv"] = uv
        if uv is None:
            state.update(problem="uv was not found (looked at BROWSERPLUG_UV, UV, PATH and "
                                 "~/.local/bin)",
                         remedy="install uv (https://docs.astral.sh/uv/getting-started/"
                                f"installation/), or {NO_UV_ROUTE}; then reconnect the MCP "
                                f"client")
            return state
        if not os.path.isfile(script + ".lock"):
            state.update(lock_current=False, problem="server/server.py.lock is missing",
                         remedy=f"run `{LOCK_COMMAND}` from {root}")
            return state
        env = uv_subprocess_env(environ)
        call = functools.partial(_call, runner or _run, root=root)

        code, _out, err = call([uv, "lock", "--check", "--script", script],
                               timeout=FIND_TIMEOUT_SECONDS, env=env,
                               what="uv lock --check --script server/server.py")
        state["lock_current"] = code == 0
        if code != 0:
            state.update(problem="server/server.py.lock does not match the dependencies "
                                 "declared in server/server.py", uv_output=_tail(err),
                         remedy=f"run `{LOCK_COMMAND}` from {root}")
            return state

        if os.path.lexists(env_dir):
            # Asked before uv: given a directory without pyvenv.cfg, uv answers for the base
            # interpreter instead.
            damage = _structural_damage(env_dir)
            if damage:
                state.update(env_state="damaged", env_current=False,
                             problem=f"the server environment at {env_dir} is damaged: "
                                     f"{damage}", remedy=_STOP_AND_START)
                return state

        work = _Env(root, uv, call, env, env_dir, (), lambda message: None)
        try:
            code, err, action = work.check()
        except ServerEnvError as exc:
            if getattr(exc, "interpreter_unusable", False):
                state.update(env_state="damaged", env_current=False,
                             problem=f"the server environment at {env_dir} is damaged: uv "
                                     f"cannot run its python", uv_output=exc.uv_output,
                             remedy=_STOP_AND_START)
                return state
            state.update(env_state="error", env_current=False, problem=exc.message,
                         uv_output=exc.uv_output, remedy=exc.remedy)
            return state
        env_state = {"create": "not_built", "replace": "replace"}.get(
            action, "out_of_date" if code != 0 else "current")
        state.update(env_state=env_state, env_current=env_state == "current")
        if env_state == "current":
            state["python"] = work.python
            return state
        build = f"to do it now, run `{START_COMMAND}`, which also starts the server"
        state.update(uv_output=_tail(err), **{
            "not_built": dict(
                problem="the server environment has not been built yet",
                remedy=f"nothing to fix: the next start (a launcher spawn, or "
                       f"`{START_COMMAND}`) builds it in {env_dir}; a first build on an empty "
                       f"uv cache downloads every package and can take tens of seconds"),
            "out_of_date": dict(
                problem="the server environment is out of date with the lock",
                remedy=f"nothing to fix while no server runs: the next start updates it; "
                       f"{build}. A server already running keeps its old packages until it "
                       f"is stopped (python server/control.py shutdown); /restart refuses to "
                       f"change the environment it runs from"),
            "replace": dict(
                problem="the lock names a different Python than the server environment "
                        "was built on, so uv will replace the environment",
                remedy="stop any running server first (python server/control.py "
                       "shutdown): uv cannot replace an environment a process runs "
                       f"from. Then the next start replaces it; {build}"),
        }[env_state])
    except ServerEnvError as exc:
        state.update(problem=str(exc), remedy=exc.remedy)
    except Exception as exc:        # a diagnostic must never become a second failure
        state.update(problem=f"{type(exc).__name__}: {exc}")
    return state


def running_env_damage(prefix=None, base_prefix=None):
    """Why the RUNNING interpreter's environment is damaged, or None.

    A uv replace that fails on the locked trampoline deletes Lib/ but keeps pyvenv.cfg, and
    an environment deleted under a running server loses both; either way the server keeps
    running on what it already imported and fails on the next lazy import. It shows as a
    venv interpreter (prefix != base_prefix) missing either one."""
    prefix = sys.prefix if prefix is None else prefix
    base_prefix = getattr(sys, "base_prefix", sys.prefix) if base_prefix is None else base_prefix
    if _norm(prefix) == _norm(base_prefix):
        return None
    if not os.path.exists(os.path.join(prefix, "pyvenv.cfg")):
        lost = "its pyvenv.cfg (the environment was deleted or pruned under it)"
    elif _site_packages(prefix) is None:
        lost = ("its site-packages (a uv replace that failed on its locked files removes "
                "them)")
    else:
        return None
    return (f"this server runs from {prefix}, which has lost {lost}; imports it has not made "
            f"yet will fail. Stop the server (python server/control.py shutdown), then run "
            f"`{START_COMMAND}` or reconnect an MCP client: either one rebuilds the "
            f"environment")


# ---- the interpreter a client registers ------------------------------------------------

# uv's managed-Python directory names: the minor-version link (cpython-3.12-windows-x86_64-
# none) that uv moves to each new patch release, and a patch directory (cpython-3.12.14-...).
_MINOR_LINK = re.compile(r"cpython-(\d+\.\d+)-[a-z0-9_]+-.+", re.IGNORECASE)
_PATCH_DIR = re.compile(r"cpython-(\d+\.\d+\.\d+)[a-z0-9]*-.+", re.IGNORECASE)
_REQUIRES_PYTHON = re.compile(r"""(?m)^# requires-python\s*=\s*["']([^"']+)["']""")
_BOUND = re.compile(r"(>=|<=|<|>)\s*(\d+)\.(\d+)(?:\.(\d+))?")


def uv_python_dir(path):
    """("link", "3.12") when `path` lies under uv's minor-version link, ("patch", "3.12.14")
    under a patch directory, (None, None) otherwise."""
    for part in reversed(str(path).replace("\\", "/").split("/")):
        match = _MINOR_LINK.fullmatch(part)
        if match:
            return "link", match.group(1)
        match = _PATCH_DIR.fullmatch(part)
        if match:
            return "patch", match.group(1)
    return None, None


def _requires_python(project_root):
    """The requires-python specifier of server/server.py's PEP 723 block, or None."""
    try:
        with open(os.path.join(project_root, "server", "server.py"), "r",
                  encoding="utf-8") as fh:
            head = fh.read(4096)
    except OSError:
        return None
    match = _REQUIRES_PYTHON.search(head)
    return match.group(1) if match else None


def server_python_minor(project_root):
    """The server's lowest Python minor ("3.11"), the lowest `>=` bound of server/server.py's
    requires-python, or None when the block names none. The launcher is NOT tied to it: any
    minor inside the range will do (`server_python_minors`)."""
    spec = _requires_python(project_root)
    bound = re.search(r">=\s*(\d+)\.(\d+)", spec) if spec else None
    return f"{bound.group(1)}.{bound.group(2)}" if bound else None


def server_python_minors(project_root):
    """Every Python minor ("3.11", "3.12") server/server.py's requires-python admits,
    ascending; [] when the block names no lower bound. With no upper bound only the lowest
    is named, since "the highest" is then undefined. A patch part in a lower bound does not
    narrow the minor (`>=3.11.2` still admits 3.11)."""
    spec = _requires_python(project_root)
    if not spec:
        return []
    low = high = None
    for op, major, minor, patch in _BOUND.findall(spec):
        major, minor = int(major), int(minor)
        if op in (">=", ">"):
            low = max(low or (major, minor), (major, minor))
        elif op == "<=":
            high = min(high or (major, minor), (major, minor))
        else:           # `<3.13` and `<3.13.0` exclude the whole 3.13 minor; `<3.13.2` does not
            top = (major, minor - 1) if not patch or int(patch) == 0 else (major, minor)
            high = min(high or top, top)
    if low is None:
        return []
    if high is None or high[0] != low[0]:
        return [f"{low[0]}.{low[1]}"]
    return [f"{low[0]}.{m}" for m in range(low[1], high[1] + 1)]


def _span(minors):
    return minors[0] if len(minors) == 1 else f"{minors[0]} to {minors[-1]}"


def _env_pyvenv():
    """The server environment's pyvenv.cfg as a lower-cased {key: value}; {} without one."""
    try:
        with open(os.path.join(server_env_dir(), "pyvenv.cfg"), "r",
                  encoding="utf-8-sig") as fh:
            return {k.strip().lower(): v.strip()
                    for k, _sep, v in (line.partition("=") for line in fh)}
    except OSError:
        return {}


def server_env_minor():
    """The Python minor ("3.12") the persistent server environment was built on, read from
    its pyvenv.cfg (`home`, else `version_info`/`version`) with no process started; None
    when there is no environment or it names none."""
    cfg = _env_pyvenv()
    kind, minor = uv_python_dir(cfg["home"]) if cfg.get("home") else (None, None)
    if kind == "link":
        return minor
    version = re.match(r"(\d+)\.(\d+)", cfg.get("version_info") or cfg.get("version") or "")
    return f"{version.group(1)}.{version.group(2)}" if version else None


def _env_home_python(minors):
    """uv's minor-version link interpreter, for a minor in `minors`, as the server
    environment's pyvenv.cfg names it (`home`; uv writes the link there), or None when it
    names something else."""
    home = _env_pyvenv().get("home", "")
    kind, minor = uv_python_dir(home) if home else (None, None)
    if kind != "link" or minor not in minors:
        return None
    names = ("python.exe",) if os.name == "nt" else (f"python{minor}", "python3")
    for name in names:
        python = os.path.join(home, name)
        if uv_python_dir(python) == ("link", minor) and os.path.isfile(python):
            return python
    return None


def launcher_python(project_root, *, uv=None, environ=None, runner=None, ask_uv=True):
    """(python, None): the interpreter MCP clients register the launcher with, uv's
    minor-version link for a Python minor inside the server's requires-python range;
    (None, why) when there is none.

    The launcher is stdlib-only, so ANY in-range minor serves. The choice: the minor the
    persistent server environment already uses (its pyvenv.cfg names the link as `home`,
    read with no process started), else the HIGHEST in-range minor uv has installed (only
    with `ask_uv`: `uv python find --system --managed-python --no-python-downloads <minor>`
    per candidate, highest first, from the checkout; `--system` keeps a .venv in the
    checkout, or an active one, from answering), else (None, why) recommending
    `uv python install` of the highest in range.
    An answer counts only when it lies under the minor-version link, which `uv python
    upgrade` retargets; a patch directory would pin the registration to one release.
    macOS and the BSDs get (None, None): a bare interpreter has no psutil there, so a
    launcher on it cannot check which process answers /health, nor whether the server
    environment is in use. The `uv run --script` form installs psutil for the launcher on
    darwin (its PEP 723 block), so that is the registration to recommend.
    Never raises."""
    try:
        if not (sys.platform == "win32" or sys.platform.startswith("linux")):
            return None, None
        root = os.path.realpath(project_root)
        minors = server_python_minors(root)
        if not minors:
            return None, "server/server.py names no requires-python"
        python = _env_home_python(minors)
        if python:
            return python, None
        # Highest first, but the environment's own minor (when it is in range) leads.
        order = sorted(minors, key=lambda m: tuple(map(int, m.split("."))), reverse=True)
        env_minor = server_env_minor()
        if env_minor in order:
            order.remove(env_minor)
            order.insert(0, env_minor)
        if not ask_uv:
            named = (f"uv's Python {minors[0]} minor-version link" if len(minors) == 1 else
                     f"a uv minor-version link inside the server's Python range "
                     f"({_span(minors)})")
            return None, (f"the server environment does not name {named}; "
                          f"`uv python find --managed-python {order[0]}` prints it")
        uv = uv or find_uv(environ)
        if uv is None:
            return None, "uv was not found"
        call = functools.partial(_call, runner or _run, root=root)
        not_a_link = None
        for minor in order:
            code, out, _err = call([uv, "python", "find", "--system", "--managed-python",
                                    "--no-python-downloads", minor],
                                   timeout=FIND_TIMEOUT_SECONDS,
                                   env=uv_subprocess_env(environ),
                                   what=f"uv python find --managed-python {minor}")
            lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
            if code != 0 or not lines:
                continue
            python = lines[-1]
            if uv_python_dir(python) == ("link", minor):
                return python, None
            not_a_link = not_a_link or (python, minor)
        if not_a_link:
            python, minor = not_a_link
            return None, (f"uv answered {python}, which is not its Python {minor} "
                          f"minor-version link; run `uv python install {minor}`, which "
                          f"recreates the link")
        best = minors[-1]
        names = (best if len(minors) == 1 else
                 f"{' or '.join(reversed(minors))} (the server's range is {_span(minors)})")
        return None, f"uv has no managed Python {names}; run `uv python install {best}` first"
    except Exception as exc:        # a diagnostic must never become a second failure
        return None, f"{type(exc).__name__}: {exc}"
