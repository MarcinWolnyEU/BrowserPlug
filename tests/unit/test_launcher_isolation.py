"""The shared server must not inherit its configuration from whichever client spawned it
(review L12), and a launcher must not adopt a BrowserPlug-shaped /health that belongs to
another checkout or another user (review S5, launcher half).
"""
import json
import logging
import os
import socket
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

import pytest

from server.runtime import lifecycle
from browserplug_shared import mcp_port, server_env
from launcher import launcher


@pytest.fixture
def spawn_env(tmp_path, monkeypatch):
    """Run spawn_detached_server with a fake Popen and return what it was given."""
    monkeypatch.setenv("BROWSERPLUG_LOG_FILE", str(tmp_path / "browserplug-server.log"))
    monkeypatch.delenv("BROWSERPLUG_MCP_PORT", raising=False)
    # The hermetic seam: resolve_server_python answers without uv (the fake Popen below
    # would otherwise swallow uv's own subprocess calls).
    monkeypatch.setenv(server_env.SERVER_PYTHON_ENV, sys.executable)
    seen = {}

    def fake_popen(args, **kwargs):
        seen["args"], seen["env"] = args, kwargs["env"]
        return object()

    def run():
        with patch.object(launcher.subprocess, "Popen", fake_popen):
            launcher.spawn_detached_server()
        return seen

    return run


class TestSpawnEnvironment:
    def test_client_secrets_do_not_reach_the_shared_server(self, spawn_env, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "not-a-real-key")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "not-a-real-secret")
        env = spawn_env()["env"]
        assert "ANTHROPIC_API_KEY" not in env
        assert "AWS_SECRET_ACCESS_KEY" not in env

    def test_what_the_server_needs_still_flows(self, spawn_env, monkeypatch):
        monkeypatch.setenv("BROWSERPLUG_ADMISSION_LIMIT", "3")
        monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example.org:3128")
        monkeypatch.setenv("PYTHONUTF8", "1")
        env = spawn_env()["env"]
        assert env["BROWSERPLUG_ADMISSION_LIMIT"] == "3"
        assert env["HTTPS_PROXY"] == "http://proxy.example.org:3128"
        assert env["PYTHONUTF8"] == "1"
        assert env.get("PATH") or env.get("Path")
        assert env[mcp_port.DETACHED_ENV] == "1"
        assert env["BROWSERPLUG_LOG_FILE"]

    def test_allowlist_is_case_insensitive_for_windows_names(self):
        out = mcp_port.server_spawn_env({"Path": "x", "SystemRoot": "C:\\Windows",
                                         "Secret": "s", "browserplug_x": "1"})
        assert out == {"Path": "x", "SystemRoot": "C:\\Windows", "browserplug_x": "1"}


class TestLockPath:
    def test_lock_lives_in_the_state_dir_not_tmpdir(self, tmp_path, monkeypatch):
        state = tmp_path / "state"
        other_tmp = tmp_path / "client-tmp"
        other_tmp.mkdir()
        monkeypatch.setenv("BROWSERPLUG_STATE_DIR", str(state))
        for name in ("TMPDIR", "TEMP", "TMP"):
            monkeypatch.setenv(name, str(other_tmp))
        monkeypatch.setattr(tempfile, "tempdir", None)
        path = lifecycle._instance_lock_path(2830)
        assert os.path.dirname(path) == str(state)
        assert os.path.basename(path) == "browserplug-instance-2830.lock"
        assert os.path.isdir(state)

    def test_two_clients_with_different_tmpdirs_get_one_lock(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BROWSERPLUG_STATE_DIR", str(tmp_path / "state"))
        paths = set()
        for name in ("a", "b"):
            d = tmp_path / name
            d.mkdir()
            monkeypatch.setenv("TMPDIR", str(d))
            monkeypatch.setenv("TEMP", str(d))
            monkeypatch.setattr(tempfile, "tempdir", None)
            paths.add(lifecycle._instance_lock_path(2830))
        assert len(paths) == 1


class TestServerInterpreter:
    """The launcher's interpreter is not the server's: a client may start the launcher with
    any Python (usually uv's bare interpreter, with no site-packages at all)."""

    def _venv(self, root):
        sub = "Scripts" if os.name == "nt" else "bin"
        exe = root / ".venv" / sub / ("python.exe" if os.name == "nt" else "python")
        exe.parent.mkdir(parents=True)
        exe.write_text("")
        return exe

    def test_a_venv_in_the_checkout_is_never_spawned(self, spawn_env, tmp_path, monkeypatch):
        """A `.venv` left in the checkout is ignored: the launcher spawns the server
        environment in the state directory, after uv (a fake one here) said it is current."""
        root = tmp_path / "checkout"
        (root / "server").mkdir(parents=True)
        (root / "server" / "server.py").write_text("# stand-in\n")
        venv_python = self._venv(root)
        monkeypatch.setenv("BROWSERPLUG_STATE_DIR", str(tmp_path / "state"))
        env_dir = tmp_path / "state" / server_env.SERVER_ENV_DIR_NAME
        env_python = env_dir.joinpath(*(("Scripts", "python.exe") if os.name == "nt"
                                        else ("bin", "python")))
        env_python.parent.mkdir(parents=True)
        env_python.write_text("")
        (env_dir / "pyvenv.cfg").write_text("home = x\n")
        env_dir.joinpath(*(("Lib", "site-packages") if os.name == "nt"
                           else ("lib", "python3.12", "site-packages"))).mkdir(parents=True)
        calls = []

        def fake_uv(argv, *, cwd, timeout, env):
            calls.append([str(part) for part in argv])
            if argv[1:2] == ["sync"] and "--check" in argv:
                return 0, "", f"Would use script environment at: {env_dir}\n"
            return 0, "", ""            # the import probe

        monkeypatch.setattr(server_env, "_run", fake_uv)
        monkeypatch.delenv(server_env.SERVER_PYTHON_ENV)
        monkeypatch.setenv(server_env.UV_ENV, sys.executable)     # any existing file
        monkeypatch.setattr(launcher, "PROJECT_ROOT", str(root))
        args = spawn_env()["args"]
        assert os.path.normcase(args[0]) == os.path.normcase(str(env_python))
        assert calls[0][1:3] == ["sync", "--active"] and "--check" in calls[0]
        assert not [argv for argv in calls if str(venv_python) in argv]

    def test_no_environment_and_no_uv_is_an_error_not_a_fallback(self, tmp_path, monkeypatch):
        """The old fallback started the server on the launcher's own interpreter, which then
        died at import with nothing but a startup log to show for it. Now the spawn fails
        with the reason, in the client's log."""
        monkeypatch.delenv(server_env.SERVER_PYTHON_ENV, raising=False)
        for name in (server_env.UV_ENV, "UV", "XDG_BIN_HOME"):
            monkeypatch.delenv(name, raising=False)
        for name in ("PATH", "USERPROFILE", "HOME", "LOCALAPPDATA"):
            monkeypatch.setenv(name, "" if name == "PATH" else str(tmp_path))
        with pytest.raises(server_env.ServerEnvError) as err:
            mcp_port.resolve_server_python(str(tmp_path))
        assert err.value.kind == "uv_missing"

    def test_the_seam_names_the_interpreter(self, tmp_path, monkeypatch):
        monkeypatch.setenv(server_env.SERVER_PYTHON_ENV, sys.executable)
        self._venv(tmp_path)
        assert mcp_port.resolve_server_python(str(tmp_path)) == sys.executable

    def test_spawn_uses_the_resolved_interpreter(self, spawn_env, monkeypatch):
        monkeypatch.setattr(launcher, "resolve_server_python",
                            lambda *a, **k: "resolved-python")
        assert spawn_env()["args"][0] == "resolved-python"

    def test_a_failed_resolution_is_logged_as_the_spawn_failure(self, spawn_env, monkeypatch,
                                                                 capsys):
        def refuse(*a, **k):
            raise server_env.ServerEnvError("uv was not found", kind="uv_missing",
                                            remedy="install uv")
        monkeypatch.setattr(launcher, "resolve_server_python", refuse)
        assert launcher._spawn_logged() is None
        assert "uv was not found" in capsys.readouterr().err


def _proc(cmdline, user="me", cwd="nowhere"):
    """What proc_facts.facts answers for a process: (same_user, argv, cwd)."""
    return (True, cmdline, cwd) if user == "me" else (False, [], None)


class TestServerOwnership:
    ROOT = os.path.abspath("checkout")

    def _check(self, procs, pid=10):
        def probe(pid):
            if pid not in procs:
                raise OSError(f"no such process {pid}")
            return procs[pid]
        return mcp_port.server_pid_is_ours(pid, self.ROOT, probe=probe)

    def test_our_server_py_same_user(self):
        entry = os.path.join(self.ROOT, "server", "server.py")
        assert self._check({10: _proc(["python.exe", entry])}) is True

    def test_another_checkouts_server_is_foreign(self):
        other = os.path.abspath(os.path.join("elsewhere", "server", "server.py"))
        assert self._check({10: _proc(["python.exe", other])}) is False

    def test_unrelated_process_is_foreign(self):
        assert self._check({10: _proc(["node.exe", "app.js"])}) is False

    def test_other_user_is_foreign(self):
        entry = os.path.join(self.ROOT, "server", "server.py")
        assert self._check({10: _proc(["python.exe", entry], user="them")}) is False

    def test_dead_or_missing_pid_is_foreign(self):
        assert self._check({}, pid=10) is False
        assert self._check({}, pid=None) is False

    def test_module_form_needs_our_cwd(self):
        assert self._check({10: _proc(["python", "-m", "server.server"],
                                      cwd=self.ROOT)}) is True
        assert self._check({10: _proc(["python", "-m", "server.server"],
                                      cwd=os.path.abspath("elsewhere"))}) is False

    def test_a_relative_entry_is_placed_by_the_processs_cwd_never_by_ours(self, tmp_path,
                                                                         monkeypatch):
        """An unreadable cwd (None) leaves a relative `server/server.py` unplaced: resolving it
        against the launcher's own cwd would adopt any process started that way anywhere."""
        root = str(tmp_path)
        relative = os.path.join("server", "server.py")

        def probe(cwd):
            return lambda pid: (True, ["python", relative], cwd)
        assert mcp_port.server_pid_is_ours(10, root, probe=probe(root)) is True
        monkeypatch.chdir(tmp_path)          # this process sits in the checkout
        assert mcp_port.server_pid_is_ours(10, root, probe=probe(None)) is False
        assert self._check({10: _proc(["python", "-m", "server"], cwd=None)}) is False

    def test_the_uv_env_trampoline_and_the_real_python_both_name_the_entry(self):
        """Under the server environment the pair is <env>/Scripts/python.exe server.py (the
        trampoline) and <base python> server.py: the script argument is what counts."""
        entry = os.path.join(self.ROOT, "server", "server.py")
        env_python = os.path.join("browserplug", "server-env", "Scripts", "python.exe")
        assert self._check({10: _proc([env_python, entry])}) is True
        assert self._check({10: _proc([os.path.join("cpython-3.12", "python.exe"),
                                           entry])}) is True

    @pytest.mark.parametrize("registered", ["link", "real"])
    def test_a_junction_or_symlink_to_the_checkout_is_the_same_checkout(self, tmp_path,
                                                                       registered):
        """A client registered through a link to the checkout (the README suggests one) and a
        server started under the real path, or the other way round, must adopt each other:
        paths are compared by what they resolve to, not by how they are spelled."""
        real = tmp_path / "real-checkout"
        (real / "server").mkdir(parents=True)
        (real / "server" / "server.py").write_text("", encoding="utf-8")
        link = tmp_path / "link-to-checkout"
        if os.name == "nt":
            made = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(real)],
                                  capture_output=True, text=True)
            assert made.returncode == 0, made.stdout + made.stderr
        else:
            os.symlink(real, link)
        try:
            root, server_arg = ((str(link), real) if registered == "link"
                                else (str(real), link))
            argv = ["python", str(server_arg / "server" / "server.py")]
            assert mcp_port.server_pid_is_ours(
                10, root, probe=lambda pid: (True, argv, str(tmp_path))) is True
        finally:
            if os.name == "nt":
                os.rmdir(link)       # the junction only; the target stays

    def test_a_pid_that_does_not_own_the_listener_is_foreign(self):
        """/health's server_pid is a claim. With the port that answered, the pid must also
        own that port's listener."""
        entry = os.path.join(self.ROOT, "server", "server.py")
        probe = lambda pid: _proc(["python", entry])           # noqa: E731
        owns = {10: True}
        listener = lambda pid, port: owns.get(pid, False)       # noqa: E731
        assert mcp_port.server_pid_is_ours(10, self.ROOT, probe=probe, port=2830,
                                           listener_probe=listener) is True
        owns[10] = False
        assert mcp_port.server_pid_is_ours(10, self.ROOT, probe=probe, port=2830,
                                           listener_probe=listener) is False
        # No port, no listener question: the callers that cannot name one keep working.
        assert mcp_port.server_pid_is_ours(10, self.ROOT, probe=probe) is True

    def test_an_unreadable_or_unavailable_socket_table_skips_only_the_listener_half(
            self, monkeypatch, caplog):
        """A table that cannot be read says nothing about the server. Refusing on it would
        make every launcher spawn a duplicate of a healthy server, so the listener question
        is skipped (a failed read warns once) while the pid, argv and cwd checks still hold."""
        from browserplug_shared import proc_facts
        entry = os.path.join(self.ROOT, "server", "server.py")
        probe = lambda pid: _proc(["python", entry])           # noqa: E731
        stranger = lambda pid: _proc(["python", "other.py"])   # noqa: E731

        def unavailable(pid, port):
            raise proc_facts.ProbeUnavailable("no socket table here")

        def unreadable(pid, port):
            raise OSError("table unreadable")
        monkeypatch.setattr(mcp_port, "_warned_no_socket_table", False)
        assert mcp_port.server_pid_is_ours(10, self.ROOT, probe=probe, port=2830,
                                           listener_probe=unavailable) is True
        with caplog.at_level(logging.WARNING, logger=mcp_port.__name__):
            assert mcp_port.server_pid_is_ours(10, self.ROOT, probe=probe, port=2830,
                                               listener_probe=unreadable) is True
            assert mcp_port.server_pid_is_ours(11, self.ROOT, probe=probe, port=2830,
                                               listener_probe=unreadable) is True
        warnings = [r for r in caplog.records if "socket table" in r.getMessage()]
        assert len(warnings) == 1
        assert "table unreadable" in warnings[0].getMessage()
        # The other checks still decide.
        assert mcp_port.server_pid_is_ours(10, self.ROOT, probe=stranger, port=2830,
                                           listener_probe=unreadable) is False
        assert mcp_port.server_pid_is_ours(10, self.ROOT, probe=lambda pid: (False, [], None),
                                           port=2830, listener_probe=unreadable) is False

    def test_with_no_backend_it_trusts_health_and_says_so_once(self, monkeypatch, caplog):
        """A platform with no native probe and no psutil cannot check. It must not turn the
        check off silently."""
        from browserplug_shared import proc_facts

        def unavailable(pid):
            raise proc_facts.ProbeUnavailable("no backend")
        monkeypatch.setattr(mcp_port, "_warned_no_probe", False)
        with caplog.at_level(logging.WARNING, logger=mcp_port.__name__):
            assert mcp_port.server_pid_is_ours(10, self.ROOT, probe=unavailable) is True
            assert mcp_port.server_pid_is_ours(11, self.ROOT, probe=unavailable) is True
        warnings = [r for r in caplog.records if "psutil" in r.getMessage()]
        assert len(warnings) == 1
        assert warnings[0].levelno == logging.WARNING
        assert "no native process probe" in warnings[0].getMessage()
        assert "uv run" not in warnings[0].getMessage()

    def test_the_psutil_fallback_is_only_for_platforms_without_a_native_probe(self,
                                                                             monkeypatch):
        """On Windows and Linux the check reads /proc or the kernel itself, so a launcher on
        a bare interpreter, with psutil nowhere, still refuses what is not its server."""
        from browserplug_shared import proc_facts
        monkeypatch.setitem(sys.modules, "psutil", None)      # import psutil -> ImportError
        if os.name == "nt" or sys.platform.startswith("linux"):
            assert mcp_port.server_pid_is_ours(os.getpid(), self.ROOT) is False
            assert proc_facts.facts(os.getpid())[0] is True
        else:
            with pytest.raises(proc_facts.ProbeUnavailable):
                proc_facts.facts(os.getpid())


def _health_server(payload):
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = HTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, port


class TestLauncherAdoption:
    def test_launcher_rejects_a_foreign_browserplug_health(self, monkeypatch):
        """This process answers /health as BrowserPlug but is not this checkout's server
        (its cmdline is pytest), so the launcher must treat the port as foreign."""
        monkeypatch.delenv("BROWSERPLUG_MCP_PORT", raising=False)
        srv, port = _health_server({"service": "browserplug", "status": "ok",
                                    "server_pid": os.getpid()})
        try:
            assert launcher.find_server_port(timeout=2.0, start_port=port,
                                             last_port=port) is None
        finally:
            srv.shutdown()
            srv.server_close()

    def test_launcher_adopts_when_ownership_check_passes(self, monkeypatch):
        monkeypatch.delenv("BROWSERPLUG_MCP_PORT", raising=False)
        srv, port = _health_server({"service": "browserplug", "status": "ok",
                                    "server_pid": os.getpid()})
        monkeypatch.setattr(launcher, "server_pid_is_ours", lambda pid, root, **k: True,
                            raising=False)
        try:
            assert launcher.find_server_port(timeout=2.0, start_port=port,
                                             last_port=port) == port
        finally:
            srv.shutdown()
            srv.server_close()


class TestListenerOwnership:
    """The launcher against a real listener in this process, quoting server pids."""

    ROOT = os.path.abspath("checkout")

    @pytest.fixture
    def names_checkout(self, monkeypatch):
        """Every pid reads as a same-user process running this checkout's server.py, so only
        the socket's owner can tell the real server from a squatter quoting its pid."""
        from browserplug_shared import proc_facts
        entry = os.path.join(launcher.PROJECT_ROOT, "server", "server.py")
        monkeypatch.setattr(proc_facts, "facts",
                            lambda pid: (True, ["python", entry], launcher.PROJECT_ROOT))
        monkeypatch.delenv("BROWSERPLUG_MCP_PORT", raising=False)

    @pytest.fixture
    def listener(self):
        made = []

        def make(claimed_pid):
            srv, port = _health_server({"service": "browserplug", "status": "ok",
                                        "server_pid": claimed_pid})
            made.append(srv)
            return port
        yield make
        for srv in made:
            srv.shutdown()
            srv.server_close()

    def test_the_seek_adopts_the_process_that_owns_the_listener(self, names_checkout,
                                                               listener):
        port = listener(os.getpid())
        assert launcher.find_server_port(timeout=2.0, start_port=port, last_port=port) == port

    def test_the_seek_refuses_a_listener_quoting_another_processs_pid(self, names_checkout,
                                                                     listener):
        """The squatter's /health names a real server process (here: the parent of this
        one), which passes every check on the pid. The listener is this process's."""
        port = listener(os.getppid())
        assert launcher.find_server_port(timeout=2.0, start_port=port, last_port=port) is None

    def test_a_pinned_port_is_held_to_the_same_rule(self, names_checkout, listener,
                                                    monkeypatch):
        squatter = listener(os.getppid())
        monkeypatch.setenv("BROWSERPLUG_MCP_PORT", str(squatter))
        assert launcher.find_server_port(timeout=2.0) is None
        owner = listener(os.getpid())
        monkeypatch.setenv("BROWSERPLUG_MCP_PORT", str(owner))
        assert launcher.find_server_port(timeout=2.0) == owner

    def test_the_seek_stamps_the_port_it_reached_into_the_health(self, listener):
        port = listener(1)
        found, health = mcp_port.seek_browserplug(port, port, timeout=2.0)
        assert found == port and health[mcp_port.PROBED_PORT_KEY] == port
