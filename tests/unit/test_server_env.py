"""shared-py/browserplug_shared/server_env.py: which interpreter runs the shared server.

Everything here runs against a fake uv (the `runner` seam) except the tests at the end, which
run the real uv offline on a scratch checkout, a scratch UV_CACHE_DIR and a scratch state
directory. Nothing here touches the user's real server environment, uv cache or
server/server.py.

The shapes the fakes answer with are uv 0.12.18's, probed on Windows 11:
`uv sync --active --script ... --check` names the environment on its first line ("Would use",
"Would create", "Would replace"), exits 1 with "The environment is outdated" when it would
change it, and, given a VIRTUAL_ENV with no pyvenv.cfg, names the base interpreter instead
(a sync then installs into the base Python). `sync --locked` on a stale lock blames
`uv.lock` and hints `uv lock`, which is the wrong command for a script.
"""

import os
import pathlib
import shutil
import subprocess
import sys
import textwrap

import pytest

import test_imports  # noqa: F401
from browserplug_shared import mcp_port, server_env
from browserplug_shared.config_store import STATE_DIR_ENV
from browserplug_shared.server_env import ServerEnvError

PY = ("Scripts", "python.exe") if os.name == "nt" else ("bin", "python")
SITE = ("Lib", "site-packages") if os.name == "nt" else ("lib", "python3.12", "site-packages")
JUNCTION = os.path.join("C:\\", "uv", "python", "cpython-3.12-windows-x86_64-none", "python.exe")


class Checkout:
    """A scratch checkout, a scratch state directory and the server environment in it."""

    def __init__(self, tmp_path, monkeypatch):
        self.root = tmp_path / "checkout"
        (self.root / "server").mkdir(parents=True)
        (self.root / "server" / "server.py").write_text(
            '# /// script\n# requires-python = ">=3.12,<3.13"\n# dependencies = []\n# ///\n',
            encoding="utf-8")
        self.state = tmp_path / "state"
        self.state.mkdir()
        monkeypatch.setenv(STATE_DIR_ENV, str(self.state))
        self.env_dir = self.state / server_env.SERVER_ENV_DIR_NAME
        self.python = self.env_dir.joinpath(*PY)
        self.build()
        self.uv = tmp_path / "bin" / ("uv.exe" if os.name == "nt" else "uv")
        self.uv.parent.mkdir()
        self.uv.write_text("", encoding="utf-8")

    def build(self):
        self.python.parent.mkdir(parents=True, exist_ok=True)
        self.python.write_text("", encoding="utf-8")
        (self.env_dir / "pyvenv.cfg").write_text("home = x\n", encoding="utf-8")
        self.site_packages.mkdir(parents=True, exist_ok=True)

    @property
    def site_packages(self):
        return self.env_dir.joinpath(*SITE)

    def environ(self, **extra):
        env = {"PATH": str(self.uv.parent)}
        env.update(extra)
        return env


def _check(action, env_dir, outdated=False):
    """`uv sync --check`'s answer: `action` is use, create or replace."""
    head = {"use": "Would use", "create": "Would create", "replace": "Would replace"}[action]
    if outdated or action != "use":
        return (1, "", f"{head} script environment at: {env_dir}\nResolved 2 packages in 1ms\n"
                       f"Would install 1 package\n + idna==3.10\nerror: The environment is "
                       f"outdated; run `uv sync` to update the environment\n")
    return 0, "", f"{head} script environment at: {env_dir}\nWould make no changes\n"


class FakeUv:
    """Answers uv's subcommands from a script; records every call."""

    def __init__(self, box, **answers):
        self.box = box
        self.calls = []
        self.answers = {
            "sync": [(0, "", f"Using script environment at: {box.env_dir}\n"
                             f"Checked 31 packages in 4ms\n")],
            "sync_check": [_check("use", box.env_dir)],
            "probe": [(0, "", "")],
            "probe_isolated": [(0, "", "")],
            "lock": [(0, "", "Resolved 32 packages in 1ms\n")],
            "find": [(0, JUNCTION + "\n", "")],
        }
        self.answers.update(answers)
        self.on_sync = None

    @staticmethod
    def kind(argv):
        if argv[1:2] == ["sync"]:
            return "sync_check" if "--check" in argv else "sync"
        if argv[1:2] == ["lock"]:
            return "lock"
        if argv[1:3] == ["python", "find"]:
            return "find"
        if argv[1:2] == ["-c"]:
            return "probe"
        if argv[1:3] == ["-I", "-c"]:
            return "probe_isolated"
        raise AssertionError(f"unexpected call {argv}")

    def __call__(self, argv, *, cwd, timeout, env):
        kind = self.kind(argv)
        self.calls.append((kind, list(argv), cwd, timeout, env))
        if kind == "sync" and self.on_sync:
            self.on_sync()
        queue = self.answers[kind]
        answer = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def kinds(self):
        return [call[0] for call in self.calls]


@pytest.fixture
def box(tmp_path, monkeypatch):
    return Checkout(tmp_path, monkeypatch)


def ensure(box, uv, **kw):
    kw.setdefault("environ", box.environ())
    return server_env.ensure_server_python(str(box.root), runner=uv, **kw)


def stale_then_current(box, action="use"):
    """A FakeUv whose check says the environment needs a sync until one ran."""
    uv = FakeUv(box)
    synced = []
    uv.answers["sync_check"] = [_check(action, box.env_dir, outdated=True)]

    def sync():
        synced.append(box.env_dir.exists())
        box.build()
        uv.answers["sync_check"] = [_check("use", box.env_dir)]
    uv.on_sync = sync
    return uv, synced


# ---- where the environment lives, the seam, a .venv left in the checkout -------------------

def test_the_environment_lives_in_the_state_directory(box, tmp_path, monkeypatch):
    assert server_env.server_env_dir() == str(box.env_dir)
    monkeypatch.setenv(STATE_DIR_ENV, str(tmp_path / "elsewhere"))
    assert server_env.server_env_dir() == str(tmp_path / "elsewhere" / "server-env")
    assert server_env.server_env_python(str(box.env_dir)) == str(box.python)


def test_the_hermetic_seam_is_returned_without_asking_uv(box):
    uv = FakeUv(box)
    got = ensure(box, uv, environ=box.environ(**{server_env.SERVER_PYTHON_ENV: sys.executable}))
    assert got == sys.executable
    assert uv.calls == []


def test_a_seam_naming_a_missing_file_is_an_error_not_a_spawn(box, tmp_path):
    with pytest.raises(ServerEnvError) as err:
        ensure(box, FakeUv(box), environ=box.environ(
            **{server_env.SERVER_PYTHON_ENV: str(tmp_path / "gone" / "python.exe")}))
    assert err.value.kind == "override_missing"
    assert server_env.SERVER_PYTHON_ENV in str(err.value)


def test_a_venv_in_the_checkout_is_ignored(box):
    """A whole-looking `<root>/.venv` is never the answer: every entry point names the
    environment in the state directory, and a server running from the .venv restarts onto
    it."""
    venv = box.root / ".venv"
    venv_python = venv.joinpath(*PY)
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("", encoding="utf-8")
    (venv / "pyvenv.cfg").write_text("home = x\n", encoding="utf-8")
    venv.joinpath(*SITE).mkdir(parents=True)

    def asked(uv):
        assert not [argv for _kind, argv, *_ in uv.calls if str(venv_python) in argv]
        assert all(env.get("VIRTUAL_ENV") in (None, str(box.env_dir))
                   for *_, env in uv.calls)
        return uv.kinds()

    uv = FakeUv(box)
    assert ensure(box, uv) == str(box.python)
    assert asked(uv) == ["sync_check", "probe"]
    assert server_env.existing_server_python(str(box.root), environ=box.environ()) == \
        str(box.python)
    uv = FakeUv(box)
    assert for_restart(box, uv, running_prefix=str(venv)) == str(box.python)
    assert asked(uv) == ["sync_check", "probe"]
    uv = FakeUv(box)
    state = inspect(box, uv)
    assert state["mode"] == "uv" and state["python"] == str(box.python)
    assert asked(uv) == ["lock", "sync_check"]


# ---- finding uv ----------------------------------------------------------------------

def test_uv_missing_is_a_clear_error_naming_the_install_and_the_override(box):
    with pytest.raises(ServerEnvError) as err:
        ensure(box, FakeUv(box), environ={"PATH": ""})
    assert err.value.kind == "uv_missing"
    text = str(err.value)
    assert "uv was not found" in text and server_env.UV_ENV in text
    assert "https://docs.astral.sh/uv/" in text
    assert server_env.SERVER_PYTHON_ENV in text and "--requirements-from-script" in text


def test_find_uv_order(box, tmp_path):
    other = tmp_path / "other" / box.uv.name
    other.parent.mkdir()
    other.write_text("", encoding="utf-8")
    third = tmp_path / "third" / box.uv.name
    third.parent.mkdir()
    third.write_text("", encoding="utf-8")
    path = {"PATH": str(box.uv.parent)}
    assert server_env.find_uv(path) == str(box.uv)
    assert server_env.find_uv({**path, "UV": str(other)}) == str(other)
    assert server_env.find_uv({**path, "UV": str(other), server_env.UV_ENV: str(third)}) \
        == str(third)
    # A stale override is skipped, not trusted.
    assert server_env.find_uv({**path, server_env.UV_ENV: str(tmp_path / "gone.exe")}) \
        == str(box.uv)


def test_find_uv_looks_in_the_standard_install_dir(tmp_path):
    home = tmp_path / "home"
    uv = home / ".local" / "bin" / ("uv.exe" if os.name == "nt" else "uv")
    uv.parent.mkdir(parents=True)
    uv.write_text("", encoding="utf-8")
    assert server_env.find_uv({"PATH": "", "USERPROFILE": str(home), "HOME": str(home)}) \
        == str(uv)


def test_find_uv_never_runs_a_uv_lying_in_the_current_directory(tmp_path, monkeypatch):
    """The launcher's cwd is the MCP client's project; a uv.exe there is not ours."""
    stray = tmp_path / ("uv.exe" if os.name == "nt" else "uv")
    stray.write_text("", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    found = server_env.find_uv({"PATH": os.pathsep.join(["", "."])})
    assert found is None or not os.path.samefile(found, stray)


# ---- ensure: a whole, current environment -----------------------------------------------

def test_a_current_environment_is_checked_probed_and_returned(box):
    uv = FakeUv(box)
    assert ensure(box, uv) == str(box.python)
    assert uv.kinds() == ["sync_check", "probe"]
    _kind, check, cwd, _timeout, env = uv.calls[0]
    assert check[1:] == ["sync", "--active", "--script",
                         server_env.server_script(str(box.root)), "--locked", "--check"]
    assert cwd == os.path.realpath(box.root)
    assert env["VIRTUAL_ENV"] == str(box.env_dir)
    probe = uv.calls[1][1]
    assert probe[0] == str(box.python)
    assert probe[2] == "import " + ", ".join(server_env.PROBE_IMPORTS)
    assert "VIRTUAL_ENV" not in uv.calls[1][4]


def test_the_script_is_named_by_the_realpath_of_the_checkouts_server_py(box):
    spelled = os.path.join(str(box.root), "server", "..").replace("\\", "/")
    assert server_env.server_script(spelled) == os.path.realpath(box.root / "server" / "server.py")
    if os.name == "nt":
        import ctypes
        buf = ctypes.create_unicode_buffer(1024)
        assert ctypes.windll.kernel32.GetShortPathNameW(str(box.root), buf, 1024)
        assert os.path.normcase(server_env.server_script(buf.value)) == \
            os.path.normcase(os.path.realpath(box.root / "server" / "server.py"))


# ---- ensure: building and updating --------------------------------------------------------

def test_a_missing_environment_is_built_by_uv_sync_active(box):
    shutil.rmtree(box.env_dir)
    uv, synced = stale_then_current(box, action="create")
    notes = []
    uv.answers["sync"] = [(0, "", f"Creating script environment at: {box.env_dir}\n"
                                  f"Installed 31 packages in 480ms\n")]
    assert ensure(box, uv, log=notes.append) == str(box.python)
    # The assessment, the re-assessment under the build lock, the sync, the final check.
    assert uv.kinds() == ["sync_check", "sync_check", "sync", "sync_check", "probe"]
    sync = next(call for call in uv.calls if call[0] == "sync")
    assert sync[1][1:] == ["sync", "--active", "--script",
                           server_env.server_script(str(box.root)), "--locked"]
    assert sync[4]["VIRTUAL_ENV"] == str(box.env_dir)
    assert synced == [False]
    assert len(notes) == 1 and "built the server environment" in notes[0]
    assert "Installed 31 packages" in notes[0]


def test_an_out_of_date_environment_is_synced_while_nothing_runs_from_it(box):
    uv, synced = stale_then_current(box)
    assert ensure(box, uv) == str(box.python)
    assert synced == [True]                 # updated in place, not deleted
    assert uv.kinds() == ["sync_check", "sync_check", "sync", "sync_check", "probe"]


def test_an_environment_built_on_another_python_is_replaced_by_the_sync(box):
    uv, synced = stale_then_current(box, action="replace")
    assert ensure(box, uv) == str(box.python)
    assert synced == [True]


@pytest.mark.parametrize("state", ["out_of_date", "damaged"])
def test_an_environment_a_process_runs_from_is_never_changed(box, monkeypatch, state):
    """Swapping packages under a running server breaks its next lazy import; replacing or
    deleting the environment guts it. Refused, with the remedy, before uv is asked to."""
    monkeypatch.setattr(server_env, "env_in_use",
                        lambda env_dir: "a running process holds its Scripts\\python.exe")
    monkeypatch.setattr(server_env, "IN_USE_GRACE_SECONDS", 0.1)
    deleted = []
    monkeypatch.setattr(server_env, "_delete_env", lambda *a: deleted.append(a))
    if state == "damaged":
        (box.env_dir / "pyvenv.cfg").unlink()
        uv = FakeUv(box)
    else:
        uv, _synced = stale_then_current(box)
    with pytest.raises(ServerEnvError) as err:
        ensure(box, uv)
    assert err.value.kind == ("damaged_in_use" if state == "damaged" else "env_in_use")
    assert "control.py shutdown" in err.value.remedy
    assert server_env.START_COMMAND in err.value.remedy
    assert "a running process holds" in str(err.value)
    assert "sync" not in uv.kinds() and deleted == []


def test_a_peer_that_lets_go_within_the_grace_is_waited_for(box, monkeypatch):
    """A peer launcher's import probe holds the python for a second or two."""
    answers = ["a running process holds it", None]
    monkeypatch.setattr(server_env, "env_in_use", lambda env_dir: answers.pop(0))
    uv, synced = stale_then_current(box)
    assert ensure(box, uv) == str(box.python)
    assert synced == [True] and answers == []


def test_the_running_interpreters_own_environment_counts_as_in_use(box, monkeypatch):
    monkeypatch.setattr(server_env.sys, "prefix", str(box.env_dir))
    assert server_env.env_in_use(str(box.env_dir)) == "this process runs from it"
    assert server_env.env_in_use(str(box.state / "other")) is None


@pytest.mark.skipif(os.name != "nt", reason="the process-image test is Windows' in-use signal")
def test_a_python_nothing_runs_reads_as_unused(box):
    assert server_env.env_in_use(str(box.env_dir)) is None
    box.python.unlink()
    assert server_env.env_in_use(str(box.env_dir)) is None


@pytest.mark.skipif(os.name != "nt", reason="the process-image test is Windows' in-use signal")
def test_a_process_running_the_envs_python_counts_as_in_use(box, monkeypatch):
    seen = []
    monkeypatch.setattr(server_env.proc_facts, "pids_running_image",
                        lambda path: seen.append(path) or [4321])
    assert "4321" in server_env.env_in_use(str(box.env_dir))
    assert seen == [str(box.python)]
    monkeypatch.setattr(server_env.proc_facts, "pids_running_image", lambda path: [])
    assert server_env.env_in_use(str(box.env_dir)) is None


@pytest.mark.skipif(os.name != "nt", reason="the process-image test is Windows' in-use signal")
def test_an_unreadable_process_list_does_not_block_the_work(box, monkeypatch):
    def broken(path):
        raise OSError("EnumProcesses failed")
    monkeypatch.setattr(server_env.proc_facts, "pids_running_image", broken)
    assert server_env.env_in_use(str(box.env_dir)) is None


@pytest.mark.skipif(os.name != "nt", reason="the process-image test is Windows' in-use signal")
def test_asking_whether_the_env_is_in_use_never_opens_its_python(box, monkeypatch):
    """A write-open of the trampoline makes a concurrent launch of it (a /restart
    replacement, a peer's import probe) fail with a sharing violation while the handle is
    open. The question is answered from the process list instead."""
    import builtins
    opened = []
    real_open, real_os_open = builtins.open, os.open

    def spy(path, *a, **k):
        opened.append(os.fspath(path))
        return real_open(path, *a, **k)

    def spy_os(path, *a, **k):
        opened.append(os.fspath(path))
        return real_os_open(path, *a, **k)
    monkeypatch.setattr(builtins, "open", spy)
    monkeypatch.setattr(os, "open", spy_os)
    server_env.env_in_use(str(box.env_dir))
    assert not [p for p in opened if os.path.normcase(p) == os.path.normcase(str(box.python))]


def test_on_macos_a_psutil_launcher_still_sees_the_env_in_use(box, monkeypatch):
    """The `uv run --script` launcher form installs psutil on darwin; with it the guard
    against a sync under a live server works there as it does elsewhere."""
    import types

    class Error(Exception):
        pass

    def proc(pid, exe, cmdline):
        return types.SimpleNamespace(info={"pid": pid, "exe": exe, "cmdline": cmdline})
    processes = [proc(1, "/usr/bin/zsh", ["zsh"]),
                 proc(2, None, None),
                 proc(3, str(box.python), [str(box.python), "server.py"])]
    fake = types.SimpleNamespace(Error=Error, process_iter=lambda attrs: iter(processes))
    monkeypatch.setitem(sys.modules, "psutil", fake)
    assert server_env.proc_facts.psutil_holder(str(box.env_dir)) == 3
    monkeypatch.setattr(server_env, "_on_windows", lambda: False)
    monkeypatch.setattr(server_env.sys, "platform", "darwin")
    assert server_env.env_in_use(str(box.env_dir)) == "pid 3 runs from it"
    del processes[2]
    assert server_env.env_in_use(str(box.env_dir)) is None


def test_on_macos_without_psutil_only_this_process_is_known(box, monkeypatch):
    monkeypatch.setitem(sys.modules, "psutil", None)
    assert server_env.proc_facts.psutil_holder(str(box.env_dir)) is None


@pytest.mark.parametrize("platform", ["darwin", "freebsd14"])
def test_no_bare_interpreter_registration_is_advised_where_it_loses_the_safety_nets(
        box, monkeypatch, platform):
    """A bare interpreter has no psutil on macOS or a BSD: the launcher could neither check
    which process answers /health nor see that the environment is in use. Preflight then
    recommends the `uv run --script` form, whose PEP 723 block installs psutil on darwin."""
    monkeypatch.setattr(server_env.sys, "platform", platform)
    uv = FakeUv(box)
    assert server_env.launcher_python(str(box.root), uv=str(box.uv), runner=uv) == (None, None)
    assert uv.calls == []


LINUX_ENV = "/home/me/.local/state/browserplug/server-env"


def _proc_tree(tmp_path, *rows):
    """A /proc fixture: rows of (pid, argv0, maps text)."""
    proc = tmp_path / "proc"
    for pid, argv0, maps in rows:
        (proc / pid).mkdir(parents=True)
        (proc / pid / "cmdline").write_bytes(argv0.encode() + b"\0server.py\0")
        (proc / pid / "maps").write_text(maps, encoding="utf-8")
    (proc / "self").mkdir()
    return str(proc)


OTHER = ("10", "/usr/bin/bash", "7f00 r-xp /usr/lib/libc.so.6\n")


def test_a_linux_process_is_found_by_its_argv0_alone(tmp_path):
    """argv0 names the env's python, and nothing in its maps does."""
    proc = _proc_tree(tmp_path, OTHER, ("12", f"{LINUX_ENV}/bin/python", ""))
    assert server_env._linux_holder(LINUX_ENV, proc_root=proc) == 12


def test_a_linux_process_is_found_by_its_memory_maps_alone(tmp_path):
    """The real interpreter runs from the base python; only a mapped file under the env
    gives it away."""
    proc = _proc_tree(tmp_path, OTHER, ("11", "/usr/bin/python3",
                                        f"7f00 r-xp {LINUX_ENV}/lib/python3.12/site-packages/"
                                        f"x.so\n"))
    assert server_env._linux_holder(LINUX_ENV, proc_root=proc) == 11


def test_a_linux_process_outside_the_env_is_not_a_holder(tmp_path):
    proc = _proc_tree(tmp_path, OTHER, ("11", "/usr/bin/python3", ""),
                      ("13", f"{LINUX_ENV}-other/bin/python", f"7f00 r-xp {LINUX_ENV}-other/x\n"))
    assert server_env._linux_holder(LINUX_ENV, proc_root=proc) is None


# ---- ensure: uv's colour --------------------------------------------------------------

COLOUR_VARIABLES = {"FORCE_COLOR": "1", "CLICOLOR_FORCE": "1", "force_color": "0"}


def test_uv_runs_with_colour_off_whatever_the_caller_forces():
    env = server_env.uv_subprocess_env({**COLOUR_VARIABLES, "NO_COLOR": "0", "KEEP": "me"})
    assert env["NO_COLOR"] == "1" and env["KEEP"] == "me"
    assert not [name for name in env if name.upper() in ("FORCE_COLOR", "CLICOLOR_FORCE")]


def test_every_uv_call_of_an_ensure_runs_with_colour_off(box):
    uv, _synced = stale_then_current(box)
    ensure(box, uv, environ=box.environ(**COLOUR_VARIABLES))
    assert uv.calls
    for _kind, argv, _cwd, _timeout, env in uv.calls:
        if argv[1:2] in (["sync"], ["lock"]):
            assert env["NO_COLOR"] == "1" and "FORCE_COLOR" not in env, argv


def test_a_uv_that_colours_anyway_is_still_understood(box):
    """The first line of `uv sync --check` is what names the environment; coloured, it no
    longer starts with the words the parser looks for."""
    dim, reset = "\x1b[2m", "\x1b[0m"
    coloured = (0, "", f"{dim}Would use script environment at:{reset} {box.env_dir}\n"
                       f"{dim}Would make no changes{reset}\n")
    assert ensure(box, FakeUv(box, sync_check=[coloured])) == str(box.python)
    state = inspect(box, FakeUv(box, sync_check=[coloured]))
    assert state["env_state"] == "current", state


def test_a_uv_whose_output_names_no_environment_is_reported_as_not_understood(box):
    uv = FakeUv(box, sync_check=[(0, "", "something nobody expected\nWould make no changes\n")])
    with pytest.raises(ServerEnvError) as err:
        ensure(box, uv)
    assert err.value.kind == "not_a_script_env"
    assert "not understood" in str(err.value) and "something nobody expected" in str(err.value)
    # Not the UV_* / uv.toml advice that made people delete a healthy environment.
    assert "UV_" not in err.value.remedy and "delete" not in err.value.remedy
    assert "sync" not in uv.kinds()


# ---- ensure: damage -----------------------------------------------------------------

UNRUNNABLE = (2, "", "error: Failed to inspect Python interpreter from active virtual "
                     "environment at `{python}`\n  Caused by: Failed to query Python "
                     "interpreter\n  Caused by: %1 is not a valid Win32 application. "
                     "(os error 193)\n")


def test_a_python_uv_cannot_run_is_damage_and_is_rebuilt(box):
    """A truncated Scripts/python.exe (a launcher killed while uv wrote it, a full disk):
    uv fails before the damage logic ran, and nothing ever repaired it."""
    broken = (UNRUNNABLE[0], "", UNRUNNABLE[2].format(python=box.python))
    uv = FakeUv(box, sync_check=[broken, broken, _check("use", box.env_dir)])
    rebuilt = []

    def rebuild():
        rebuilt.append(box.env_dir.exists())
        box.build()
    uv.on_sync = rebuild
    notes = []
    assert ensure(box, uv, log=notes.append) == str(box.python)
    assert rebuilt == [False]          # deleted first, then synced
    assert uv.kinds() == ["sync_check", "sync_check", "sync", "sync_check", "probe"]
    assert any("damaged" in n and "cannot run its python" in n for n in notes)


def test_a_python_uv_cannot_run_under_a_running_server_says_to_stop_it(box, monkeypatch):
    monkeypatch.setattr(server_env, "_wait_unused", lambda env_dir: "a running process holds it")
    broken = (UNRUNNABLE[0], "", UNRUNNABLE[2].format(python=box.python))
    with pytest.raises(ServerEnvError) as err:
        ensure(box, FakeUv(box, sync_check=[broken]))
    assert err.value.kind == "damaged_in_use"
    assert "control.py shutdown" in err.value.remedy


def test_a_restart_from_an_environment_with_an_unrunnable_python_is_refused(box):
    broken = (UNRUNNABLE[0], "", UNRUNNABLE[2].format(python=box.python))
    with pytest.raises(ServerEnvError) as err:
        for_restart(box, FakeUv(box, sync_check=[broken]))
    assert err.value.kind == "damaged_in_use"
    assert "control.py shutdown" in err.value.remedy


def test_inspect_reports_an_unrunnable_python_as_damage(box):
    broken = (UNRUNNABLE[0], "", UNRUNNABLE[2].format(python=box.python))
    state = inspect(box, FakeUv(box, sync_check=[broken]))
    assert state["env_state"] == "damaged" and "cannot run its python" in state["problem"]
    assert "control.py shutdown" in state["remedy"]

def test_a_directory_that_is_not_a_whole_environment_is_deleted_never_synced(box):
    """Given a VIRTUAL_ENV without pyvenv.cfg, uv takes the trampoline's base interpreter for
    the environment and installs into the base Python. So the directory goes first, and uv is
    asked nothing until it is gone."""
    (box.env_dir / "pyvenv.cfg").unlink()
    notes = []
    uv, synced = stale_then_current(box, action="create")
    assert ensure(box, uv, log=notes.append) == str(box.python)
    assert uv.kinds() == ["sync", "sync_check", "probe"]
    assert synced == [False]
    assert any("damaged" in n and "pyvenv.cfg" in n for n in notes)


@pytest.mark.parametrize("damage", ["site-packages", "python"])
def test_a_missing_site_packages_or_python_counts_as_damage(box, damage):
    if damage == "site-packages":
        shutil.rmtree(box.site_packages)
    else:
        box.python.unlink()
    uv, synced = stale_then_current(box, action="create")
    notes = []
    assert ensure(box, uv, log=notes.append) == str(box.python)
    assert synced == [False]
    assert any(damage in n for n in notes)


def test_a_check_naming_another_environment_is_refused_and_nothing_is_synced(box):
    """The shape uv answers a directory without pyvenv.cfg with, and the reason every sync
    is preceded by a check whose first line must name the server's environment."""
    base = os.path.join("C:\\", "uv", "python", "cpython-3.12-windows-x86_64-none")
    uv = FakeUv(box, sync_check=[_check("use", base, outdated=True)])
    with pytest.raises(ServerEnvError) as err:
        ensure(box, uv)
    assert err.value.kind == "not_a_script_env"
    assert base in str(err.value) and "nothing was synced" in str(err.value)
    assert "sync" not in uv.kinds()


def test_a_sync_that_names_another_environment_is_loud(box):
    uv, _synced = stale_then_current(box)
    uv.answers["sync"] = [(0, "", "Using script environment at: C:\\elsewhere\nInstalled 1\n")]
    with pytest.raises(ServerEnvError) as err:
        ensure(box, uv)
    assert err.value.kind == "not_a_script_env"
    assert "C:\\elsewhere" in str(err.value) and "may have changed" in str(err.value)


def test_an_import_probe_that_fails_counts_as_damage(box):
    broken = (1, "", "ModuleNotFoundError: No module named 'pydantic._internal._validators'\n")
    uv = FakeUv(box, probe=[broken, broken, (0, "", "")], probe_isolated=[broken, broken])
    rebuilt = []

    def rebuild():
        rebuilt.append(box.env_dir.exists())
        box.build()
    uv.on_sync = rebuild
    uv.answers["sync_check"] = [_check("use", box.env_dir), _check("use", box.env_dir),
                                _check("use", box.env_dir)]
    assert ensure(box, uv) == str(box.python)
    assert rebuilt == [False]
    assert uv.kinds() == ["sync_check", "probe", "probe_isolated",
                          "sync_check", "probe", "probe_isolated",
                          "sync", "sync_check", "probe"]


def test_still_broken_after_one_rebuild_is_an_error_not_a_loop(box):
    broken = (1, "", "ModuleNotFoundError: No module named 'mcp'\n")
    uv = FakeUv(box, probe=[broken], probe_isolated=[broken])
    uv.on_sync = box.build
    with pytest.raises(ServerEnvError) as err:
        ensure(box, uv)
    assert err.value.kind == "damaged"
    assert "No module named 'mcp'" in str(err.value)
    assert uv.kinds().count("sync") == 1


def test_a_damaged_environment_that_cannot_be_deleted_says_to_stop_the_server(box, monkeypatch):
    (box.env_dir / "pyvenv.cfg").unlink()

    def locked(path, *a, **k):
        raise PermissionError(13, "Access is denied", str(box.python))
    monkeypatch.setattr(server_env.shutil, "rmtree", locked)
    with pytest.raises(ServerEnvError) as err:
        ensure(box, FakeUv(box))
    assert err.value.kind == "damaged_in_use"
    assert "control.py shutdown" in err.value.remedy
    assert "Access is denied" in str(err.value)


def test_a_probe_timeout_is_not_taken_for_damage(box):
    uv = FakeUv(box, probe=[subprocess.TimeoutExpired(["python"], 60)])
    with pytest.raises(ServerEnvError) as err:
        ensure(box, uv)
    assert err.value.kind == "timeout"
    assert box.env_dir.exists()


# ---- ensure: uv's failures ------------------------------------------------------------

def test_a_failed_sync_is_fatal_and_names_the_command_and_the_environment(box):
    uv, _synced = stale_then_current(box)
    uv.on_sync = None
    uv.answers["sync"] = [(2, "", "error: No interpreter found for Python >=3.12,<3.13\n")]
    with pytest.raises(ServerEnvError) as err:
        ensure(box, uv)
    assert err.value.kind == "sync_failed"
    assert uv.kinds().count("sync") == 1
    assert "No interpreter found" in str(err.value)
    assert server_env.SYNC_COMMAND in err.value.remedy
    assert f"VIRTUAL_ENV={box.env_dir}" in err.value.remedy


def test_a_missing_lock_names_our_lock_command(box):
    uv = FakeUv(box, sync_check=[(2, "", f"Would create script environment at: {box.env_dir}\n"
                                         "error: `uv sync --locked` requires a script "
                                         "lockfile; run `uv lock --script server.py` to lock "
                                         "the script\n")])
    with pytest.raises(ServerEnvError) as err:
        ensure(box, uv)
    assert err.value.kind == "lock_missing"
    assert server_env.LOCK_COMMAND in err.value.remedy
    assert uv.kinds() == ["sync_check"]


def test_a_stale_lock_maps_uvs_misleading_hint_to_our_command(box):
    uv = FakeUv(box, sync_check=[(1, "", f"Would use script environment at: {box.env_dir}\n"
                                         "Resolved in 1ms\nerror: The lockfile at `uv.lock` "
                                         "needs to be updated, but `--check` was provided.\n\n"
                                         "hint: To update the lockfile, run `uv lock`.\n")])
    with pytest.raises(ServerEnvError) as err:
        ensure(box, uv)
    assert err.value.kind == "lock_stale"
    assert "uv lock --script server/server.py" in str(err.value)
    assert "run `uv lock`." not in str(err.value)       # uv's hint, for the wrong command
    assert uv.kinds() == ["sync_check"]


# ---- env-2: a probe the caller's variables break is not damage --------------------------

SHADOWED = (1, "", "File \"C:\\shadow\\psutil.py\", line 1\nImportError: shadowed\n")


def test_imports_only_the_callers_variables_break_delete_nothing(box, monkeypatch):
    """A PYTHONPATH that shadows a package fails the probe, and the server would inherit
    it; deleting the environment fixes nothing and guts a server running from it."""
    deleted = []
    monkeypatch.setattr(server_env.shutil, "rmtree", lambda *a, **k: deleted.append(a))
    uv = FakeUv(box, probe=[SHADOWED])
    with pytest.raises(ServerEnvError) as err:
        ensure(box, uv, environ=box.environ(PYTHONPATH="C:\\shadow", PYTHONUTF8="1"))
    assert err.value.kind == "env_vars_break_imports"
    assert "PYTHONPATH" in err.value.remedy and "Nothing was deleted" in err.value.remedy
    assert "shadow" in str(err.value)
    assert deleted == [] and box.python.exists()
    assert uv.kinds() == ["sync_check", "probe", "probe_isolated"]


def test_the_probe_sees_the_python_variables_the_spawned_server_inherits(box):
    uv = FakeUv(box)
    ensure(box, uv, environ=box.environ(PYTHONPATH="C:\\p", PYTHONHOME="C:\\h"))
    probe_env = next(call[4] for call in uv.calls if call[0] == "probe")
    assert probe_env["PYTHONPATH"] == "C:\\p" and "PYTHONHOME" not in probe_env
    assert server_env._PROBE_ENV_REMOVED == mcp_port.SPAWN_ENV_DENIED


def test_the_isolated_probe_runs_with_dash_capital_i(box):
    broken = (1, "", "ModuleNotFoundError: No module named 'mcp'\n")
    uv = FakeUv(box, probe=[broken], probe_isolated=[broken])
    uv.on_sync = box.build
    with pytest.raises(ServerEnvError):
        ensure(box, uv)
    isolated = next(call[1] for call in uv.calls if call[0] == "probe_isolated")
    assert isolated[:3] == [str(box.python), "-I", "-c"]


@pytest.mark.skipif(os.name != "nt", reason="only Windows locks a running interpreter's file")
def test_an_environment_whose_python_is_held_open_is_refused_before_anything_is_deleted(
        box, monkeypatch):
    """The delete starts with Scripts/python.exe, so a holder is refused before rmtree has
    removed what that process imports."""
    (box.env_dir / "pyvenv.cfg").unlink()
    (box.site_packages / "keep.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(server_env, "IN_USE_GRACE_SECONDS", 0.3)
    with open(box.python, "rb"):
        with pytest.raises(ServerEnvError) as err:
            ensure(box, FakeUv(box))
    assert err.value.kind == "damaged_in_use"
    assert "control.py shutdown" in err.value.remedy
    assert (box.site_packages / "keep.py").exists() and box.python.exists()


# ---- env-3: concurrent builds and repairs -----------------------------------------------

def test_a_file_a_peer_already_deleted_is_not_taken_for_a_process_holding_the_env(
        box, monkeypatch):
    """Two launchers repairing at once: rmtree hits FileNotFoundError on what the other one
    removed first. That is a peer, not a running server: rebuild instead of refusing."""
    (box.env_dir / "pyvenv.cfg").unlink()
    real_rmtree = shutil.rmtree

    def peer_got_there_first(path, *a, **k):
        real_rmtree(path)
        raise FileNotFoundError(2, "The system cannot find the file specified",
                                str(box.site_packages / "idna" / "compat.py"))
    monkeypatch.setattr(server_env.shutil, "rmtree", peer_got_there_first)
    uv, _synced = stale_then_current(box, action="create")
    assert ensure(box, uv) == str(box.python)


def test_the_rmtree_hook_skips_only_vanished_files():
    server_env._skip_vanished(os.unlink, "x", FileNotFoundError(2, "gone"))
    with pytest.raises(PermissionError):
        server_env._skip_vanished(os.unlink, "x", PermissionError(13, "denied"))


def test_the_build_lock_sits_beside_the_environment(box):
    assert server_env.repair_lock_path(str(box.env_dir)) == str(box.state / "server-env.lock")


def test_a_repair_waits_for_the_lock_and_finds_the_peers_repair(box, monkeypatch):
    """Whoever holds the build lock does the work; a launcher that waited re-checks under the
    lock and deletes nothing when the environment is whole again."""
    import threading
    (box.env_dir / "pyvenv.cfg").unlink()
    deleted = []
    monkeypatch.setattr(server_env.shutil, "rmtree", lambda *a, **k: deleted.append(a))
    result = {}

    def waiter():
        try:
            result["python"] = ensure(box, FakeUv(box))
        except Exception as exc:            # reported by the assertion below
            result["error"] = exc

    with server_env._repair_lock(str(box.env_dir)):
        thread = threading.Thread(target=waiter)
        thread.start()
        thread.join(1.0)
        assert thread.is_alive(), "the second repair did not wait for the lock"
        box.build()                         # the holder's repair
    thread.join(10)
    assert result == {"python": str(box.python)}
    assert deleted == []


def test_a_lock_held_too_long_is_a_timeout_not_a_hang(box, monkeypatch):
    (box.env_dir / "pyvenv.cfg").unlink()
    monkeypatch.setattr(server_env, "REPAIR_LOCK_TIMEOUT_SECONDS", 0.3)
    with server_env._repair_lock(str(box.env_dir)):
        import threading
        out = {}
        thread = threading.Thread(target=lambda: out.update(
            kind=_kind_of(lambda: ensure(box, FakeUv(box)))))
        thread.start()
        thread.join(10)
    assert out == {"kind": "timeout"}


def _kind_of(fn):
    try:
        fn()
    except ServerEnvError as exc:
        return exc.kind
    return None


# ---- env-1: python_for_restart never changes the environment it runs from --------------

def for_restart(box, uv, **kw):
    kw.setdefault("environ", box.environ())
    kw.setdefault("running_prefix", str(box.env_dir))
    return server_env.python_for_restart(str(box.root), runner=uv, **kw)


def test_a_restart_from_an_unchanged_environment_asks_one_read_only_question(box):
    uv = FakeUv(box)
    assert for_restart(box, uv) == str(box.python)
    assert uv.kinds() == ["sync_check"]
    argv, env = uv.calls[0][1], uv.calls[0][4]
    assert "--check" in argv and "--locked" in argv and "--active" in argv
    assert env["VIRTUAL_ENV"] == str(box.env_dir)


@pytest.mark.parametrize("action,words", [("use", "packages differ"),
                                          ("replace", "replace it")], ids=["bump", "replace"])
def test_a_restart_whose_environment_would_change_is_refused_without_a_sync(box, action, words):
    """uv swaps packages in place under the live server, and an interpreter change makes it
    delete Lib/ before failing on the locked trampoline: refuse instead."""
    uv = FakeUv(box, sync_check=[_check(action, box.env_dir, outdated=True)])
    with pytest.raises(ServerEnvError) as err:
        for_restart(box, uv)
    assert err.value.kind == "deps_changed"
    assert words in str(err.value)
    assert "control.py shutdown" in err.value.remedy
    assert server_env.START_COMMAND in err.value.remedy
    assert "run `uv sync`" not in str(err.value)          # the very thing that guts it
    assert "sync" not in uv.kinds()
    assert box.site_packages.is_dir()


def test_a_restart_on_a_stale_lock_names_the_relock(box):
    uv = FakeUv(box, sync_check=[_check("use", box.env_dir, outdated=True)],
                lock=[(1, "", "error: The lockfile at `uv.lock` needs to be updated, but "
                              "`--check` was provided.\n")])
    with pytest.raises(ServerEnvError) as err:
        for_restart(box, uv)
    assert err.value.kind == "lock_stale" and server_env.LOCK_COMMAND in err.value.remedy
    assert "sync" not in uv.kinds()


def test_a_restart_from_a_gutted_environment_is_refused_without_asking_uv(box):
    shutil.rmtree(box.site_packages)
    uv = FakeUv(box)
    with pytest.raises(ServerEnvError) as err:
        for_restart(box, uv)
    assert err.value.kind == "damaged_in_use"
    assert uv.calls == []


def test_a_failed_check_names_the_read_only_command_not_the_sync(box):
    uv = FakeUv(box, sync_check=[(2, "", f"Would use script environment at: {box.env_dir}\n"
                                         "error: Failed to read the lockfile\n")])
    with pytest.raises(ServerEnvError) as err:
        for_restart(box, uv)
    assert err.value.kind == "sync_failed"
    assert server_env.SYNC_CHECK_COMMAND in err.value.remedy


def test_a_restart_check_naming_another_environment_is_refused(box):
    uv = FakeUv(box, sync_check=[_check("use", "C:\\elsewhere")])
    with pytest.raises(ServerEnvError) as err:
        for_restart(box, uv)
    assert err.value.kind == "not_a_script_env"


def test_a_server_running_from_another_interpreter_restarts_through_ensure(box, tmp_path):
    uv, synced = stale_then_current(box)
    assert for_restart(box, uv, running_prefix=str(tmp_path / "elsewhere")) == str(box.python)
    assert uv.kinds() == ["sync_check", "sync_check", "sync", "sync_check", "probe"]
    assert synced == [True]


def test_a_server_on_the_old_cache_environment_restarts_onto_the_persistent_one(box, tmp_path):
    """The cutover: a server started from uv's cache restarts onto a freshly built
    environment in the state directory, which nothing runs from yet."""
    shutil.rmtree(box.env_dir)
    cache_env = tmp_path / "uv-cache" / "environments-v2" / "server-0123456789abcdef"
    uv, synced = stale_then_current(box, action="create")
    assert for_restart(box, uv, running_prefix=str(cache_env)) == str(box.python)
    assert synced == [False]


def test_the_seam_restarts_without_asking_uv(box):
    uv = FakeUv(box)
    seam = box.environ(**{server_env.SERVER_PYTHON_ENV: sys.executable})
    assert for_restart(box, uv, environ=seam) == sys.executable
    assert uv.calls == []


def test_a_deadline_cuts_every_call_to_what_is_left(box):
    import time
    uv = FakeUv(box)
    deadline = time.monotonic() + 5.0
    ensure(box, uv, deadline=deadline)
    assert all(0 < call[3] <= 5.0 for call in uv.calls)
    with pytest.raises(ServerEnvError) as err:
        ensure(box, FakeUv(box), deadline=time.monotonic() - 1)
    assert err.value.kind == "timeout"


# ---- existing_server_python: joining a running server ----------------------------------

def test_joining_a_running_server_asks_uv_nothing(box, monkeypatch):
    """No sync (it would change the environment under the running server), no import probe,
    no repair, and no uv call at all: the directory is known, its files are checked."""
    monkeypatch.setattr(server_env, "_run", lambda *a, **k: pytest.fail(f"uv was asked {a}"))
    assert server_env.existing_server_python(str(box.root), environ=box.environ()) == \
        str(box.python)


def test_joining_never_repairs_a_damaged_environment(box, monkeypatch):
    deleted = []
    monkeypatch.setattr(server_env, "_delete_env", lambda *a: deleted.append(a))
    shutil.rmtree(box.site_packages)
    with pytest.raises(ServerEnvError) as err:
        server_env.existing_server_python(str(box.root), environ=box.environ())
    assert err.value.kind == "damaged_in_use"
    assert "control.py shutdown" in err.value.remedy
    assert deleted == []


def test_joining_refuses_an_environment_without_its_python(box):
    box.python.unlink()
    with pytest.raises(ServerEnvError) as err:
        server_env.existing_server_python(str(box.root), environ=box.environ())
    assert err.value.kind == "damaged_in_use"


def test_joining_an_environment_that_was_never_built_says_so(box):
    shutil.rmtree(box.env_dir)
    with pytest.raises(ServerEnvError) as err:
        server_env.existing_server_python(str(box.root), environ=box.environ())
    assert err.value.kind == "not_built"
    assert server_env.START_COMMAND in err.value.remedy


def test_joining_takes_the_seam_first(box):
    seam = box.environ(**{server_env.SERVER_PYTHON_ENV: sys.executable})
    assert server_env.existing_server_python(str(box.root), environ=seam) == sys.executable


# ---- subprocess hygiene ----------------------------------------------------------------

def test_every_uv_call_is_captured_rooted_windowless_and_bounded(box, monkeypatch):
    """The launcher's stdout is the MCP channel, and the client's cwd may hold a uv.toml."""
    seen = []

    def fake_run(argv, **kwargs):
        seen.append((argv, kwargs))
        kind = FakeUv.kind(argv)
        code, out, err = {"sync_check": _check("use", box.env_dir)}.get(kind, (0, "", ""))
        return subprocess.CompletedProcess(argv, code, out, err)
    monkeypatch.setattr(server_env.subprocess, "run", fake_run)
    environ = box.environ(VIRTUAL_ENV="C:\\launcher-env", UV_PYTHON="3.14",
                          UV_RUN_RECURSION_DEPTH="1", uv_project_environment="x",
                          UV_CACHE_DIR="C:\\cache", UV_FROZEN="1", UV_LOCKED="1",
                          UV_NO_SYNC="1", UV_LINK_MODE="symlink")
    assert server_env.ensure_server_python(str(box.root), environ=environ) == str(box.python)
    assert [FakeUv.kind(a) for a, _ in seen] == ["sync_check", "probe"]
    for argv, kw in seen:
        assert kw["stdin"] is subprocess.DEVNULL
        assert kw["stdout"] is subprocess.PIPE and kw["stderr"] is subprocess.PIPE
        assert kw["cwd"] == os.path.realpath(box.root)
        assert kw["timeout"] and kw["timeout"] > 0
        if os.name == "nt":
            assert kw["creationflags"] == mcp_port.CREATE_NO_WINDOW
        names = {k.upper() for k in kw["env"]}
        assert not names & {"UV_PYTHON", "UV_RUN_RECURSION_DEPTH", "UV_PROJECT_ENVIRONMENT",
                            "UV_FROZEN", "UV_LOCKED", "UV_NO_SYNC", "UV_LINK_MODE"}
        assert kw["env"]["UV_CACHE_DIR"] == "C:\\cache"
    check_env, probe_env = seen[0][1]["env"], seen[1][1]["env"]
    assert check_env["VIRTUAL_ENV"] == str(box.env_dir)        # never the caller's
    assert "VIRTUAL_ENV" not in probe_env
    timeouts = {FakeUv.kind(a): kw["timeout"] for a, kw in seen}
    assert timeouts["sync_check"] == server_env.SYNC_TIMEOUT_SECONDS
    assert timeouts["probe"] == server_env.PROBE_TIMEOUT_SECONDS


def test_the_exceptions_carry_their_remedy_in_the_text():
    err = ServerEnvError("thing failed", kind="sync_failed", remedy="do X",
                         uv_output="error: Y")
    assert str(err) == "thing failed. uv said: error: Y Fix: do X"


# ---- inspect_server_env: preflight's read-only view -------------------------------------

def inspect(box, uv, **kw):
    kw.setdefault("environ", box.environ())
    (box.root / "server" / "server.py.lock").write_text("", encoding="utf-8")
    return server_env.inspect_server_env(str(box.root), runner=uv, **kw)


def test_inspect_asks_only_read_only_questions(box):
    uv = FakeUv(box)
    state = inspect(box, uv)
    assert state["problem"] is None and state["python"] == str(box.python)
    assert state["env_dir"] == str(box.env_dir) and state["env_state"] == "current"
    assert uv.kinds() == ["lock", "sync_check"]
    for kind, argv, *_ in uv.calls:
        if kind == "lock":
            assert "--check" in argv
        if kind.startswith("sync"):
            assert "--check" in argv and "--locked" in argv


def test_inspect_reports_an_environment_that_is_not_built_yet(box):
    shutil.rmtree(box.env_dir)
    uv = FakeUv(box, sync_check=[_check("create", box.env_dir)])
    state = inspect(box, uv)
    assert state["env_state"] == "not_built"
    assert "has not been built yet" in state["problem"]
    assert server_env.START_COMMAND in state["remedy"] and str(box.env_dir) in state["remedy"]
    assert state["python"] is None


def test_inspect_reports_a_stale_lock(box):
    uv = FakeUv(box, lock=[(1, "", "error: The lockfile at `uv.lock` needs to be updated, but "
                                    "`--check` was provided.\n\nhint: To update the lockfile, "
                                    "run `uv lock`.\n")])
    state = inspect(box, uv)
    assert state["lock_current"] is False
    assert server_env.LOCK_COMMAND in state["remedy"]


@pytest.mark.parametrize("damage", ["pyvenv.cfg", "site-packages"])
def test_inspect_reports_a_gutted_environment_without_asking_uv_about_it(box, damage):
    """uv answers a directory without pyvenv.cfg for the base interpreter, so the files are
    judged first."""
    if damage == "pyvenv.cfg":
        (box.env_dir / "pyvenv.cfg").unlink()
    else:
        shutil.rmtree(box.site_packages)
    uv = FakeUv(box)
    state = inspect(box, uv)
    assert state["env_state"] == "damaged"
    assert damage in state["problem"] and "control.py shutdown" in state["remedy"]
    assert uv.kinds() == ["lock"]


def test_inspect_never_raises(box):
    uv = FakeUv(box, lock=[RuntimeError("boom")])
    state = inspect(box, uv)
    assert "boom" in state["problem"]


def test_inspect_reports_a_missing_lock_without_calling_uv(box):
    uv = FakeUv(box)
    state = server_env.inspect_server_env(str(box.root), runner=uv, environ=box.environ())
    assert "server.py.lock is missing" in state["problem"]
    assert uv.calls == []


@pytest.mark.parametrize("action,outdated,expected", [
    ("use", False, "current"),
    ("create", True, "not_built"),
    ("use", True, "out_of_date"),
    ("replace", True, "replace"),
], ids=["current", "not_built", "out_of_date", "replace"])
def test_inspect_names_what_a_start_would_do_to_the_environment(box, action, outdated,
                                                                expected):
    state = inspect(box, FakeUv(box, sync_check=[_check(action, box.env_dir, outdated)]))
    assert state["env_state"] == expected
    assert os.path.normcase(state["env_dir"]) == os.path.normcase(str(box.env_dir))
    assert (state["problem"] is None) == (expected == "current")
    if expected == "replace":
        assert "control.py shutdown" in state["remedy"]


def test_inspect_tells_a_failed_check_from_an_outdated_environment(box):
    uv = FakeUv(box, sync_check=[(2, "", "error: No interpreter found for Python >=3.12\n")])
    state = inspect(box, uv)
    assert state["env_state"] == "error"
    assert "exit code 2" in state["problem"] and "No interpreter" in state["uv_output"]
    assert server_env.SYNC_CHECK_COMMAND in state["remedy"]


def test_inspect_reports_a_check_that_names_another_environment(box):
    state = inspect(box, FakeUv(box, sync_check=[_check("use", "C:\\elsewhere")]))
    assert state["env_state"] == "error" and "C:\\elsewhere" in state["problem"]


# ---- the running server's own environment ---------------------------------------------

def test_running_env_damage(tmp_path):
    env_dir = tmp_path / "env"
    env_dir.mkdir()
    assert server_env.running_env_damage(str(tmp_path), str(tmp_path)) is None   # not a venv
    assert "pyvenv.cfg" in server_env.running_env_damage(str(env_dir), str(tmp_path))
    assert "control.py shutdown" in server_env.running_env_damage(str(env_dir), str(tmp_path))
    (env_dir / "pyvenv.cfg").write_text("home = x\n", encoding="utf-8")
    # A failed uv replace deletes Lib/ and keeps pyvenv.cfg.
    assert "site-packages" in server_env.running_env_damage(str(env_dir), str(tmp_path))
    env_dir.joinpath(*SITE).mkdir(parents=True)
    assert server_env.running_env_damage(str(env_dir), str(tmp_path)) is None


# ---- the interpreter a client registers ------------------------------------------------

@pytest.mark.parametrize("path,expected", [
    (JUNCTION, ("link", "3.12")),
    ("C:\\uv\\python\\cpython-3.12.14-windows-x86_64-none\\python.exe", ("patch", "3.12.14")),
    ("/home/me/.local/share/uv/python/cpython-3.12-linux-x86_64-gnu/bin/python3.12",
     ("link", "3.12")),
    ("/home/me/.local/share/uv/python/cpython-3.13.0rc1-macos-aarch64-none/bin/python3",
     ("patch", "3.13.0")),
    ("C:\\Python312\\python.exe", (None, None)),
    ("C:\\x\\cpython-3.12+freethreaded-windows-x86_64-none\\python.exe", (None, None)),
])
def test_uv_python_dir_tells_the_minor_link_from_a_patch_directory(path, expected):
    assert server_env.uv_python_dir(path) == expected


def test_the_server_minor_comes_from_its_requires_python(box, tmp_path):
    assert server_env.server_python_minor(str(box.root)) == "3.12"
    assert server_env.server_python_minor(str(tmp_path / "nowhere")) is None


def test_launcher_python_answers_uvs_minor_version_link(box):
    uv = FakeUv(box)
    assert server_env.launcher_python(str(box.root), uv=str(box.uv), runner=uv) == \
        (JUNCTION, None)
    [(_kind, argv, cwd, _timeout, env)] = uv.calls
    assert argv[1:] == ["python", "find", "--system", "--managed-python",
                        "--no-python-downloads", "3.12"]
    assert cwd == os.path.realpath(box.root) and "VIRTUAL_ENV" not in env


@pytest.mark.parametrize("answer,words", [
    ((0, "C:\\uv\\python\\cpython-3.12.14-windows-x86_64-none\\python.exe\n", ""),
     "not its Python 3.12 minor-version link"),
    ((2, "", "error: No interpreter found for Python 3.12\n"), "no managed Python 3.12"),
], ids=["patch-directory", "none"])
def test_launcher_python_says_how_to_get_the_link(box, answer, words):
    python, why = server_env.launcher_python(str(box.root), uv=str(box.uv),
                                             runner=FakeUv(box, find=[answer]))
    assert python is None
    assert words in why and "uv python install 3.12" in why


def test_launcher_python_never_raises(box):
    python, why = server_env.launcher_python(str(box.root), uv=str(box.uv),
                                             runner=FakeUv(box, find=[RuntimeError("boom")]))
    assert python is None and "boom" in why


# The launcher is stdlib-only, so its interpreter may be uv's minor-version link for ANY minor
# inside the server's requires-python. These tests read the REAL server/server.py block (the
# floor is 3.11, the ceiling excludes 3.13), so a one-minor assumption cannot hide behind a
# fixture; the boxes below differ only in what uv has installed and what the server
# environment was built on.

REAL_SERVER = pathlib.Path(__file__).resolve().parents[2] / "server" / "server.py"


def _link_dir(tmp_path, minor):
    """A real-looking uv minor-version link directory holding an interpreter file."""
    home = tmp_path / "uvpy" / f"cpython-{minor}-windows-x86_64-none"
    names = ("python.exe",) if os.name == "nt" else (f"python{minor}", "python3")
    home.mkdir(parents=True, exist_ok=True)
    for name in names:
        (home / name).write_text("", encoding="utf-8")
    return home, str(home / names[0])


class UvBox:
    """The real server block, a uv with only `installed` minors, a server environment whose
    pyvenv.cfg says `env_cfg`."""

    def __init__(self, box, tmp_path, installed, env_cfg="home = x\n"):
        shutil.copyfile(REAL_SERVER, box.root / "server" / "server.py")
        self.box, self.links, self.calls = box, {}, []
        for minor in ("3.11", "3.12", "3.13"):
            home, python = _link_dir(tmp_path, minor)
            self.links[minor] = (home, python)
        self.installed = installed
        (box.env_dir / "pyvenv.cfg").write_text(
            env_cfg.format(**{f"home{m.replace('.', '')}": str(h)
                              for m, (h, _p) in self.links.items()}), encoding="utf-8")

    def __call__(self, argv, *, cwd, timeout, env):
        assert argv[1:3] == ["python", "find"], argv
        minor = argv[-1]
        self.calls.append(minor)
        if minor in self.installed:
            return 0, self.links[minor][1] + "\n", ""
        return 2, "", f"error: No interpreter found for Python {minor}\n"

    def launcher(self, **kw):
        return server_env.launcher_python(str(self.box.root), uv=str(self.box.uv),
                                          runner=self, **kw)


def test_the_real_server_block_admits_3_11_and_3_12():
    root = str(REAL_SERVER.parents[1])
    assert server_env.server_python_minors(root) == ["3.11", "3.12"]
    assert server_env.server_python_minor(root) == "3.11"      # the floor, kept for callers


@pytest.mark.parametrize("spec,minors", [
    (">=3.11,<3.13", ["3.11", "3.12"]),
    (">=3.12,<3.13", ["3.12"]),
    (">=3.11.2,<3.12.4", ["3.11", "3.12"]),
    (">=3.11,<=3.13", ["3.11", "3.12", "3.13"]),
    (">=3.10", ["3.10"]),
    ("<3.13", []),
])
def test_the_range_lists_every_minor_the_block_admits(tmp_path, spec, minors):
    (tmp_path / "server").mkdir()
    (tmp_path / "server" / "server.py").write_text(
        f'# /// script\n# requires-python = "{spec}"\n# ///\n', encoding="utf-8")
    assert server_env.server_python_minors(str(tmp_path)) == minors


def test_a_3_12_environment_on_a_box_with_only_3_12_recommends_3_12(box, tmp_path):
    uv = UvBox(box, tmp_path, {"3.12"}, "home = {home312}\nversion_info = 3.12.14\n")
    # Read from the environment's pyvenv.cfg: no uv process, even when uv may be asked.
    assert uv.launcher() == (uv.links["3.12"][1], None) and uv.calls == []
    assert uv.launcher(ask_uv=False) == (uv.links["3.12"][1], None)


def test_only_3_12_installed_and_no_environment_never_recommends_3_11(box, tmp_path):
    uv = UvBox(box, tmp_path, {"3.12"})
    assert uv.launcher() == (uv.links["3.12"][1], None)
    assert uv.calls == ["3.12"], "the highest minor is asked first"
    _python, why = uv.launcher(ask_uv=False)
    assert "3.11" in why and "uv python find --managed-python 3.12" in why


def test_only_3_11_installed_recommends_3_11(box, tmp_path):
    uv = UvBox(box, tmp_path, {"3.11"})
    assert uv.launcher() == (uv.links["3.11"][1], None)
    assert uv.calls == ["3.12", "3.11"]


def test_both_installed_prefers_the_highest_in_range(box, tmp_path):
    uv = UvBox(box, tmp_path, {"3.11", "3.12", "3.13"})
    assert uv.launcher() == (uv.links["3.12"][1], None), "3.13 is outside the block"
    assert "3.13" not in uv.calls


def test_the_environments_own_minor_wins_over_a_higher_one(box, tmp_path):
    uv = UvBox(box, tmp_path, {"3.11", "3.12"}, "home = {home311}\n")
    assert uv.launcher() == (uv.links["3.11"][1], None) and uv.calls == []
    # An environment built on a patch directory still names its minor, so uv is asked for
    # that minor first.
    box_uv = UvBox(box, tmp_path, {"3.11", "3.12"},
                   "home = C:\\uv\\cpython-3.11.9-windows-x86_64-none\nversion_info = 3.11.9\n")
    assert box_uv.launcher() == (box_uv.links["3.11"][1], None)
    assert box_uv.calls == ["3.11"]


def test_an_environment_outside_the_range_is_ignored(box, tmp_path):
    uv = UvBox(box, tmp_path, {"3.12", "3.13"}, "home = {home313}\n")
    assert uv.launcher() == (uv.links["3.12"][1], None)


def test_nothing_installed_suggests_installing_the_highest_in_range(box, tmp_path):
    python, why = UvBox(box, tmp_path, set()).launcher()
    assert python is None
    assert "uv python install 3.12" in why and "uv python install 3.11" not in why


# ---- mcp_port's thin wrapper and the spawn environment ---------------------------------

def test_resolve_server_python_is_server_env(box, monkeypatch):
    seen = {}

    def fake(root, **kw):
        seen["root"], seen["log"] = root, kw.get("log")
        return "resolved"
    monkeypatch.setattr(server_env, "ensure_server_python", fake)
    warn = [].append
    assert mcp_port.resolve_server_python(str(box.root), warn=warn) == "resolved"
    assert seen == {"root": str(box.root), "log": warn}


def test_the_wrappers_pass_the_deadline_through(box, monkeypatch):
    seen = []
    monkeypatch.setattr(server_env, "ensure_server_python",
                        lambda root, **kw: seen.append(("ensure", kw["deadline"])) or "e")
    monkeypatch.setattr(server_env, "existing_server_python",
                        lambda root, **kw: seen.append(("existing", kw["deadline"])) or "x")
    assert mcp_port.resolve_server_python(str(box.root), deadline=12.5) == "e"
    assert mcp_port.existing_server_python(str(box.root), deadline=7.5) == "x"
    assert seen == [("ensure", 12.5), ("existing", 7.5)]


def test_the_spawn_environment_names_uv_and_carries_only_named_uv_settings(box):
    env = mcp_port.server_spawn_env(box.environ(
        UV_CACHE_DIR="c", UV_INDEX_URL="i", UV_PYTHON="3.14", UV_PROJECT_ENVIRONMENT="p",
        VIRTUAL_ENV="v", PYTHONHOME="h", PYTHONUTF8="1", SSL_CERT_FILE="s",
        UV_LINK_MODE="symlink"))
    assert env[server_env.UV_ENV] == str(box.uv)
    assert (env["UV_CACHE_DIR"], env["UV_INDEX_URL"], env["SSL_CERT_FILE"]) == ("c", "i", "s")
    assert env["PYTHONUTF8"] == "1"
    for name in ("UV_PYTHON", "UV_PROJECT_ENVIRONMENT", "VIRTUAL_ENV", "PYTHONHOME",
                 "UV_LINK_MODE"):
        assert name not in env, name


def test_an_exported_uv_is_carried_as_is(box, tmp_path):
    env = mcp_port.server_spawn_env({"PATH": "", server_env.UV_ENV: "C:\\pinned\\uv.exe"})
    assert env[server_env.UV_ENV] == "C:\\pinned\\uv.exe"


# ---- the tests that run the real uv ------------------------------------------------------

SCRATCH_SERVER = textwrap.dedent('''\
    # /// script
    # requires-python = ">=3.12,<3.13"
    # dependencies = []
    # ///
    print("scratch server")
''')


class RealUv:
    """A scratch checkout, uv cache and state directory, and the real uv, offline."""

    def __init__(self, tmp_path, monkeypatch):
        self.uv = server_env.find_uv()
        assert self.uv, "uv is required by the server environment and was not found (find_uv)"
        self.tmp = tmp_path
        self.root = tmp_path / "checkout"
        (self.root / "server").mkdir(parents=True)
        self.script = self.root / "server" / "server.py"
        self.cache = tmp_path / "uv-cache"
        monkeypatch.setenv(STATE_DIR_ENV, str(tmp_path / "state"))
        self.env_dir = tmp_path / "state" / server_env.SERVER_ENV_DIR_NAME
        self.environ = {k: v for k, v in os.environ.items()
                        if k.upper() not in (server_env.SERVER_PYTHON_ENV, "VIRTUAL_ENV",
                                             "UV_LINK_MODE")}
        self.environ.update(UV_CACHE_DIR=str(self.cache), UV_OFFLINE="1")
        self.run = dict(cwd=self.root, env=self.environ, capture_output=True, text=True,
                        timeout=120)

    def declare_and_lock(self, version=None, requires=">=3.12,<3.13"):
        text = SCRATCH_SERVER.replace(">=3.12,<3.13", requires)
        if version:
            text = text.replace("dependencies = []", f'dependencies = ["tinypkg @ '
                                f'{_tiny_wheel(self.tmp, version).as_uri()}"]')
        self.script.write_text(text, encoding="utf-8")
        locked = subprocess.run([self.uv, "lock", "--script", str(self.script)], **self.run)
        assert locked.returncode == 0, locked.stderr

    def ensure(self, notes=None, imports=("json",)):
        return server_env.ensure_server_python(str(self.root), environ=self.environ,
                                               log=(notes.append if notes is not None
                                                    else None), probe_imports=imports)

    def installed(self):
        return sorted(n for n in os.listdir(server_env._site_packages(str(self.env_dir)))
                      if n.endswith(".dist-info") and n.startswith("tinypkg"))

    def uv_cache(self, *args):
        # --cache-dir as well as UV_CACHE_DIR: these commands must only ever see the scratch
        # cache.
        out = subprocess.run([self.uv, "cache", *args, "--cache-dir", str(self.cache)],
                             **self.run)
        assert out.returncode == 0, out.stderr
        return out


def _tiny_wheel(directory, version):
    """A one-module wheel, so a package can be locked and synced offline. Built once per
    version: a rebuilt wheel would not match the hash the lock recorded."""
    import zipfile
    path = directory / f"tinypkg-{version}-py3-none-any.whl"
    if path.exists():
        return path
    info = f"tinypkg-{version}.dist-info"
    with zipfile.ZipFile(path, "w") as wheel:
        wheel.writestr("tinypkg/__init__.py", f"VERSION = {version!r}\n")
        wheel.writestr(f"{info}/METADATA",
                       f"Metadata-Version: 2.1\nName: tinypkg\nVersion: {version}\n")
        wheel.writestr(f"{info}/WHEEL", "Wheel-Version: 1.0\nGenerator: test\n"
                                        "Root-Is-Purelib: true\nTag: py3-none-any\n")
        wheel.writestr(f"{info}/RECORD", f"tinypkg/__init__.py,,\n{info}/METADATA,,\n"
                                         f"{info}/WHEEL,,\n{info}/RECORD,,\n")
    return path


@pytest.fixture
def real(tmp_path, monkeypatch):
    real = RealUv(tmp_path, monkeypatch)
    yield real
    shutil.rmtree(real.cache, ignore_errors=True)


def test_real_uv_builds_validates_repairs_and_refuses_a_stale_lock(real):
    """The whole ensure path on the real uv: the environment lands in the state directory,
    on the minor-version link of the block's Python, and a gutted one is rebuilt."""
    real.declare_and_lock()
    notes = []
    python = real.ensure(notes)
    assert os.path.normcase(python) == os.path.normcase(str(real.env_dir.joinpath(*PY)))
    cfg = (real.env_dir / "pyvenv.cfg").read_text(encoding="utf-8")
    assert server_env.uv_python_dir(next(ln for ln in cfg.splitlines()
                                         if ln.startswith("home")))[0] == "link"
    prefix = subprocess.run([python, "-c", "import sys; print(sys.prefix)"],
                            **real.run).stdout.strip()
    assert os.path.normcase(os.path.realpath(prefix)) == os.path.normcase(
        os.path.realpath(real.env_dir))
    assert any("built the server environment" in n for n in notes)

    # Gutted (no pyvenv.cfg): deleted and rebuilt, never handed to uv as it is.
    os.remove(real.env_dir / "pyvenv.cfg")
    notes.clear()
    assert real.ensure(notes) == python
    assert (real.env_dir / "pyvenv.cfg").is_file()
    assert any("damaged" in n for n in notes)

    # A declaration the lock does not match: refused with OUR command, never a rewrite.
    real.script.write_text(SCRATCH_SERVER.replace(">=3.12,<3.13", ">=3.11,<3.13"),
                           encoding="utf-8")
    lock_before = (real.root / "server" / "server.py.lock").read_bytes()
    with pytest.raises(ServerEnvError) as err:
        real.ensure()
    assert err.value.kind == "lock_stale"
    assert "uv lock --script server/server.py" in str(err.value)
    assert (real.root / "server" / "server.py.lock").read_bytes() == lock_before


def test_real_uv_cache_prune_and_clean_leave_the_environment_whole(real):
    """The environment hard-links its files from uv's cache, so emptying the cache leaves
    them in place: no rebuild, and the package still imports."""
    real.declare_and_lock("1.0")
    python = real.ensure(imports=("tinypkg",))
    real.uv_cache("prune")
    real.uv_cache("clean")
    assert not real.cache.exists() or not any(real.cache.iterdir())
    out = subprocess.run([python, "-I", "-c", "import tinypkg; print(tinypkg.VERSION)"],
                         **real.run)
    assert out.stdout.strip() == "1.0", out.stderr
    notes = []
    assert real.ensure(notes, imports=("tinypkg",)) == python
    assert notes == []


def test_real_uv_restart_never_syncs_the_environment_it_runs_from(real):
    """A /restart from the server environment after a package bump: refused, the installed
    version untouched, and the next start (not running from it) then applies it."""
    kw = dict(environ=real.environ, probe_imports=("tinypkg",))
    real.declare_and_lock("1.0")
    python = server_env.ensure_server_python(str(real.root), **kw)
    env_dir = server_env.env_dir_of(python)
    assert server_env.python_for_restart(str(real.root), running_prefix=env_dir, **kw) == python

    real.declare_and_lock("2.0")
    with pytest.raises(ServerEnvError) as err:
        server_env.python_for_restart(str(real.root), running_prefix=env_dir, **kw)
    assert err.value.kind == "deps_changed"
    assert real.installed() == ["tinypkg-1.0.dist-info"]

    assert server_env.ensure_server_python(str(real.root), **kw) == python
    assert real.installed() == ["tinypkg-2.0.dist-info"]


def test_real_uv_never_syncs_an_environment_a_process_runs_from(real, monkeypatch):
    """The in-use test against a real process: a python started from the environment holds
    it, so a bump is refused and the installed version stays; once it exits, the bump
    lands."""
    monkeypatch.setattr(server_env, "IN_USE_GRACE_SECONDS", 0.5)
    real.declare_and_lock("1.0")
    python = real.ensure(imports=("tinypkg",))
    holder = subprocess.Popen([python, "-c", "import sys, tinypkg; sys.stdin.read()"],
                              stdin=subprocess.PIPE, cwd=real.root, env=real.environ)
    try:
        real.declare_and_lock("2.0")
        assert server_env.env_in_use(str(real.env_dir)) is not None
        with pytest.raises(ServerEnvError) as err:
            real.ensure(imports=("tinypkg",))
        assert err.value.kind == "env_in_use"
        assert real.installed() == ["tinypkg-1.0.dist-info"]
    finally:
        holder.stdin.close()
        holder.wait(30)
    assert server_env.env_in_use(str(real.env_dir)) is None
    assert real.ensure(imports=("tinypkg",)) == python
    assert real.installed() == ["tinypkg-2.0.dist-info"]


def test_real_uv_ignores_a_venv_in_the_checkout(real):
    """A `.venv` beside the script is uv's own default home for an environment. The sync
    names the state directory through VIRTUAL_ENV (--active), so the checkout's is left
    exactly as it was and nothing lands in it."""
    real.declare_and_lock("1.0")
    made = subprocess.run([real.uv, "venv", str(real.root / ".venv"), "--python", "3.12"],
                          **real.run)
    assert made.returncode == 0, made.stderr
    checkout_site = server_env._site_packages(str(real.root / ".venv"))
    before = sorted(os.listdir(checkout_site))
    python = real.ensure(imports=("tinypkg",))
    assert os.path.normcase(python) == os.path.normcase(str(real.env_dir.joinpath(*PY)))
    assert real.installed() == ["tinypkg-1.0.dist-info"]
    assert sorted(os.listdir(checkout_site)) == before


def test_real_uv_inspect_creates_nothing_and_names_the_state(real):
    """The doctor's read-only question about an environment that is not there yet must not
    build it, and about a built one names the python a start would use."""
    real.declare_and_lock("1.0")
    state = server_env.inspect_server_env(str(real.root), environ=real.environ)
    assert state["env_state"] == "not_built" and state["problem"], state
    assert state["env_dir"] == str(real.env_dir) and not real.env_dir.exists()
    python = real.ensure(imports=("tinypkg",))
    state = server_env.inspect_server_env(str(real.root), environ=real.environ)
    assert state["env_state"] == "current" and state["problem"] is None, state
    assert os.path.normcase(state["python"]) == os.path.normcase(python)


@pytest.mark.parametrize("name,value", [("FORCE_COLOR", "1"), ("FORCE_COLOR", "0"),
                                        ("CLICOLOR_FORCE", "1")])
def test_real_uv_with_forced_colour_in_the_environment_is_still_understood(real, name, value):
    """uv colours its output when FORCE_COLOR or CLICOLOR_FORCE is set to anything, "0"
    included, and many Node tools set it for their children. The parser reads uv's first
    line, so every check failed with a misleading `not_a_script_env`."""
    real.declare_and_lock()
    real.environ[name] = value
    python = real.ensure()
    assert real.ensure() == python         # the second call goes through --check alone
    state = server_env.inspect_server_env(str(real.root), environ=real.environ)
    assert state["env_state"] == "current", state
    assert server_env.python_for_restart(str(real.root), running_prefix=str(real.env_dir),
                                         environ=real.environ,
                                         probe_imports=("json",)) == python


@pytest.mark.parametrize("damage", ["empty", "junk", "half"])
def test_real_uv_rebuilds_an_environment_whose_python_is_truncated_or_junk(real, damage):
    real.declare_and_lock()
    python = real.ensure()
    exe = real.env_dir.joinpath(*PY)
    original = exe.read_bytes()
    exe.unlink()       # never write through a symlink to uv's own interpreter (POSIX)
    exe.write_bytes({"empty": b"", "junk": b"not an executable" * 20,
                     "half": original[:len(original) // 2]}[damage])
    notes = []
    assert real.ensure(notes) == python
    assert any("damaged" in n for n in notes), notes
    out = subprocess.run([python, "-I", "-c", "print(1)"], **real.run)
    assert out.stdout.strip() == "1", out.stderr


@pytest.mark.skipif(os.name != "nt", reason="a sharing violation on the exe is a Windows thing")
def test_real_uv_asking_whether_the_env_is_in_use_never_fails_a_launch_of_its_python(real):
    """The probe write-opened Scripts/python.exe; a process starting that exe while the
    handle was open failed with WinError 32 (a third of the launches, polling in a loop)."""
    import threading
    real.declare_and_lock()
    python = real.ensure()
    stop = threading.Event()

    def poll():
        while not stop.is_set():
            server_env.env_in_use(str(real.env_dir))
    poller = threading.Thread(target=poll, daemon=True)
    poller.start()
    failures = []
    try:
        for _ in range(40):
            try:
                subprocess.run([python, "-I", "-c", "pass"], check=True, timeout=60,
                               capture_output=True, creationflags=0x08000000)
            except (OSError, subprocess.CalledProcessError) as exc:
                failures.append(repr(exc))
    finally:
        stop.set()
        poller.join(10)
    assert failures == []
