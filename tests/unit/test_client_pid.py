"""Which pid the launcher reports as its client.

The policy is pure over a probe, so a fake covers every launch mode. Real-process tests then
run the launcher's helper under a venv redirector, a bare interpreter and `uv run --script`,
since the fake cannot show that the ctypes calls work.
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from launcher import client_pid as cp

ROOT = Path(__file__).resolve().parents[2]

PREFIX = r"C:\proj\.venv"
BASE = r"C:\uv\python\cpython-3.12"
REDIRECTOR = PREFIX + r"\Scripts\python.exe"
CLAUDE = r"C:\Users\me\.local\bin\claude.exe"
UV = r"C:\Users\me\.local\bin\uv.exe"
UV_ENV = r"C:\Users\me\AppData\Local\uv\cache\environments-v2\launcher-0123"
CMD = r"C:\Windows\System32\cmd.exe"
POWERSHELL = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
OWN, PPID, CLIENT = 200, 100, 50
UV_PID = 150


class FakeProbe:
    """A process table: pid -> (image, command line, creation time, parent pid)."""

    def __init__(self, table):
        self.table = table

    def _row(self, pid):
        if pid not in self.table:
            raise OSError(f"cannot open pid {pid}")
        return self.table[pid]

    def image_path(self, pid):
        return self._row(pid)[0]

    def command_line(self, pid):
        return self._row(pid)[1]

    def created(self, pid):
        return self._row(pid)[2]

    def parent(self, pid):
        return self._row(pid)[3]


def proc(image, cmdline=None, created=1000.0, parent=0):
    return (image, image if cmdline is None else cmdline, created, parent)


def pick(table, prefix=PREFIX, base_prefix=BASE):
    return cp.pick_client_pid(OWN, PPID, FakeProbe(table), prefix, base_prefix)


def behind_redirector(**over):
    """client -> redirector -> launcher, the shape every uv or stock venv gives."""
    return {OWN: proc(BASE + r"\python.exe"),
            PPID: proc(over.get("image", REDIRECTOR), created=over.get("created", 1000.0),
                       parent=over.get("parent", CLIENT)),
            CLIENT: proc(CLAUDE, created=500.0)}


class TestVenvRedirector:
    def test_is_skipped(self):
        assert pick(behind_redirector()) == CLIENT

    def test_path_case_and_pythonw_do_not_matter(self):
        assert pick(behind_redirector(image=PREFIX.upper() + r"\SCRIPTS\PYTHONW.EXE")) == CLIENT

    def test_a_parent_outside_a_venv_is_kept_without_probing_it(self):
        # A system or uv-managed interpreter started directly: nothing sits above it.
        table = {OWN: proc(BASE + r"\python.exe")}    # PPID absent: any probe of it raises
        assert pick(table, prefix=BASE, base_prefix=BASE) == PPID

    def test_a_parent_that_is_not_this_venvs_python_is_kept(self):
        assert pick(behind_redirector(image=CLAUDE)) == PPID
        assert pick(behind_redirector(image=r"C:\other\.venv\Scripts\python.exe")) == PPID

    def test_a_python_in_the_same_venv_that_is_far_older_is_kept(self):
        assert pick(behind_redirector(created=100.0)) == PPID

    def test_a_redirector_with_no_readable_parent_is_kept(self):
        assert pick(behind_redirector(parent=0)) == PPID


class TestUvWrapper:
    """`uv run --script server/launcher.py`: client -> uv.exe -> env trampoline -> launcher,
    or client -> uv.exe -> launcher when the script runs on a bare interpreter."""

    def _through_uv(self, uv_created=999.5, uv_image=UV, parent=CLIENT):
        return {OWN: proc(BASE + r"\python.exe", created=1000.0),
                PPID: proc(UV_ENV + r"\Scripts\python.exe", created=1000.0, parent=UV_PID),
                UV_PID: proc(uv_image, "uv run --script C:\\dr\\server\\launcher.py",
                             created=uv_created, parent=parent),
                CLIENT: proc(CLAUDE, created=500.0)}

    def test_uv_above_the_env_trampoline_is_skipped(self):
        assert pick(self._through_uv(), prefix=UV_ENV) == CLIENT

    def test_uvx_is_skipped_too(self):
        assert pick(self._through_uv(uv_image=r"C:\bin\UVX.EXE"), prefix=UV_ENV) == CLIENT

    def test_uv_directly_above_a_bare_interpreter_is_skipped(self):
        table = {OWN: proc(BASE + r"\python.exe", created=1000.0),
                 PPID: proc(UV, created=999.9, parent=CLIENT), CLIENT: proc(CLAUDE)}
        assert pick(table, prefix=BASE, base_prefix=BASE) == CLIENT

    def test_a_first_run_that_built_the_env_is_still_inside_the_window(self):
        # uv resolved and installed psutil for ~40 s before it started the interpreter.
        assert pick(self._through_uv(uv_created=960.0), prefix=UV_ENV) == CLIENT

    def test_a_uv_younger_than_this_process_is_a_recycled_pid_and_kept(self):
        assert pick(self._through_uv(uv_created=1001.0), prefix=UV_ENV) == UV_PID

    def test_a_uv_far_older_than_the_window_is_kept(self):
        assert pick(self._through_uv(uv_created=1000.0 - cp.UV_SPAWN_WINDOW_SECONDS - 1),
                    prefix=UV_ENV) == UV_PID

    def test_an_unreadable_uv_keeps_the_last_readable_pid(self):
        table = self._through_uv()
        del table[UV_PID]
        assert pick(table, prefix=UV_ENV) == UV_PID

    def test_a_shell_above_uv_is_crossed(self):
        table = self._through_uv(parent=70)
        table[70] = proc(CMD, "cmd.exe /c uv run --script launcher.py", parent=CLIENT)
        assert pick(table, prefix=UV_ENV) == CLIENT


class TestWrapperShells:
    def test_a_command_shell_between_client_and_launcher_is_crossed(self):
        for image, cmdline in ((CMD, r"cmd.exe /d /s /c python launcher.py"),
                               (POWERSHELL, r"powershell -NoProfile -Command & python x.py")):
            table = {OWN: proc(BASE + r"\python.exe"),
                     PPID: proc(image, cmdline, parent=CLIENT),
                     CLIENT: proc(CLAUDE)}
            assert pick(table, prefix=BASE, base_prefix=BASE) == CLIENT

    def test_a_shell_above_the_redirector_is_crossed_too(self):
        table = behind_redirector(parent=70)
        table[70] = proc(CMD, "cmd.exe /c python launcher.py", parent=CLIENT)
        assert pick(table) == CLIENT

    def test_an_interactive_shell_is_the_client(self):
        table = {OWN: proc(BASE + r"\python.exe"),
                 PPID: proc(POWERSHELL, "powershell -NoLogo", parent=CLIENT),
                 CLIENT: proc(CLAUDE)}
        assert pick(table, prefix=BASE, base_prefix=BASE) == PPID

    def test_flags_match_whatever_their_case(self):
        table = {OWN: proc(BASE + r"\python.exe"),
                 PPID: proc(CMD, "CMD.EXE /C python x.py", parent=CLIENT),
                 CLIENT: proc(CLAUDE)}
        assert pick(table, prefix=BASE, base_prefix=BASE) == CLIENT

    def test_the_walk_is_bounded(self):
        table = {OWN: proc(BASE + r"\python.exe")}
        for pid in range(1, 7):
            table[pid] = proc(CMD, "cmd.exe /c x", parent=pid + 1)
        table[7] = proc(CLAUDE)
        assert cp.pick_client_pid(OWN, 1, FakeProbe(table), BASE, BASE) == 1 + cp.MAX_SHELL_HOPS

    def test_an_unreadable_shell_keeps_the_last_readable_pid(self):
        table = {OWN: proc(BASE + r"\python.exe"), PPID: proc(CMD, "cmd.exe /c x", parent=70)}
        # pid 70 is absent from the table: probing it raises, the walk stops on it.
        assert pick(table, prefix=BASE, base_prefix=BASE) == 70

    def test_the_names_and_flags_equal_the_servers(self):
        from server.reporting import client_identity
        assert cp.SHELL_WRAPPER_NAMES == client_identity.SHELL_WRAPPER_NAMES
        assert cp.SHELL_COMMAND_FLAGS == client_identity.SHELL_COMMAND_FLAGS
        assert cp.UV_WRAPPER_NAMES == client_identity.UV_WRAPPER_NAMES


class TestClientPidEntry:
    def test_a_failing_probe_falls_back_to_the_parent(self, monkeypatch):
        def boom():
            raise OSError("no access")
        monkeypatch.setattr(cp, "_WindowsProbe", boom)
        cp.client_pid.cache_clear()
        try:
            assert cp.client_pid() == os.getppid()
        finally:
            cp.client_pid.cache_clear()

    def test_the_answer_is_computed_once(self, monkeypatch):
        calls = []
        monkeypatch.setattr(cp, "pick_client_pid", lambda *a: calls.append(a) or 7)
        monkeypatch.setattr(cp, "_WindowsProbe", lambda: object())
        monkeypatch.setattr(cp, "_IS_WINDOWS", True)
        cp.client_pid.cache_clear()
        try:
            assert (cp.client_pid(), cp.client_pid()) == (7, 7)
            assert len(calls) == 1
        finally:
            cp.client_pid.cache_clear()


ASK = ("from launcher.client_pid import client_pid\n"
       "import os\n"
       "print(client_pid(), os.getppid())\n")


def _run(python, wrapper=()):
    """(reported pid, plain parent pid) of a process started through `wrapper`, if any."""
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(ROOT), str(ROOT / "shared-py")])}
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "ask.py"
        script.write_text(ASK)
        if wrapper and wrapper[0] == "powershell":
            argv = [*wrapper, f"& '{python}' '{script}'"]
        else:
            argv = [*wrapper, python, str(script)]
        out = subprocess.run(argv, env=env, capture_output=True, text=True,
                             timeout=120, check=True).stdout.split()
    return int(out[0]), int(out[1])


UV_ASK_METADATA = """\
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""


def _run_uv(tmp_path, metadata):
    """(reported pid, plain parent pid) of the helper run by `uv run --script`, the way a
    client registered as `uv run --script server/launcher.py` runs the launcher. A scratch
    UV_CACHE_DIR and UV_OFFLINE keep the user's uv cache and the network out of it."""
    from browserplug_shared.server_env import find_uv
    uv = find_uv()
    assert uv, "uv is required (client registrations run the launcher through it)"
    script = tmp_path / "ask.py"
    script.write_text((UV_ASK_METADATA if metadata else "") + ASK)
    env = {k: v for k, v in os.environ.items() if k.upper() != "VIRTUAL_ENV"}
    env.update(PYTHONPATH=os.pathsep.join([str(ROOT), str(ROOT / "shared-py")]),
               UV_CACHE_DIR=str(tmp_path / "uv-cache"), UV_OFFLINE="1")
    base_python = Path(sys.base_prefix) / "python.exe"
    out = subprocess.run([uv, "run", "--python", str(base_python), "--script", str(script)],
                         env=env, cwd=tmp_path, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    reported, plain_parent = out.stdout.split()
    return int(reported), int(plain_parent)


def _make_stock_venv(path):
    base_python = Path(sys.base_prefix) / "python.exe"
    subprocess.run([str(base_python), "-m", "venv", "--without-pip", str(path)],
                   check=True, timeout=120)
    return path


@pytest.mark.skipif(os.name != "nt", reason="the redirector exists only on Windows")
class TestRealProcesses:
    def test_a_venv_launcher_reports_the_process_that_started_it(self):
        if sys.prefix == sys.base_prefix:
            pytest.fail("the unit suite must run from a venv interpreter: "
                        "uv run --locked --script tests/run.py")
        reported, plain_parent = _run(sys.executable)
        # Started from this process through the venv's python.exe: the redirector sits
        # between us, and the report must step over it.
        assert reported == os.getpid()
        assert plain_parent != os.getpid()

    def test_a_bare_interpreter_reports_its_plain_parent(self):
        base_python = Path(sys.base_prefix) / "python.exe"
        assert base_python.is_file(), base_python
        reported, plain_parent = _run(str(base_python))
        assert reported == plain_parent == os.getpid()

    def test_a_stock_venv_redirector_is_skipped_too(self, tmp_path):
        # A uv environment's python is a uv trampoline; `python -m venv` makes a different
        # redirector with the same shape, and the policy must not depend on which.
        venv = _make_stock_venv(tmp_path / "stock")
        reported, plain_parent = _run(str(venv / "Scripts" / "python.exe"))
        assert reported == os.getpid()
        assert plain_parent != os.getpid()

    def test_an_8_3_short_path_does_not_defeat_the_match(self, tmp_path):
        import ctypes
        venv = _make_stock_venv(tmp_path / "a venv with a long name")
        long_path = str(venv / "Scripts" / "python.exe")
        buf = ctypes.create_unicode_buffer(1024)
        assert ctypes.windll.kernel32.GetShortPathNameW(long_path, buf, 1024)
        assert buf.value != long_path, "8.3 names are off on this volume; the test proves nothing"
        reported, plain_parent = _run(buf.value)
        assert reported == os.getpid()
        assert plain_parent != os.getpid()

    def test_a_cmd_wrapper_is_crossed_for_real(self):
        reported, plain_parent = _run(sys.executable, wrapper=("cmd", "/d", "/c"))
        assert reported == os.getpid()
        assert plain_parent != os.getpid()

    @pytest.mark.parametrize("metadata", [True, False], ids=["script-env", "no-metadata"])
    def test_uv_run_script_reports_the_process_that_started_it(self, tmp_path, metadata):
        # With a PEP 723 block: us -> uv.exe -> env trampoline -> interpreter. Without one:
        # us -> uv.exe -> bare interpreter. Either way the report is this process, not uv.
        reported, plain_parent = _run_uv(tmp_path, metadata)
        assert reported == os.getpid()
        assert plain_parent != os.getpid()

    def test_a_powershell_wrapper_is_crossed_for_real(self):
        reported, plain_parent = _run(sys.executable,
                                      wrapper=("powershell", "-NoProfile", "-NonInteractive",
                                               "-Command"))
        assert reported == os.getpid()
        assert plain_parent != os.getpid()
