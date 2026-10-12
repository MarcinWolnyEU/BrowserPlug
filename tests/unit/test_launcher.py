"""launcher/launcher.py's side of the proxy transport (BROWSERPLUG_LAUNCHER_RELAY=0).

The proxy itself is server/proxy/ (test_proxy_*.py). What the launcher owns, and what is
pinned here without a live server:

  serve_via_proxy()  runs `<server env python> server/proxy/__main__.py <args>` as a child
                     with this process's own stdin and stdout, the checkout as its cwd, no
                     console window, and returns the child's exit code. The interpreter is
                     server_env's; when there is none (a ServerEnvError) or the child cannot
                     start, the client's initialize is refused with the reason instead.
  main()             picks the port (the discovered one, else the pin, else 2830) and, when
                     nothing answered, says in an ERROR that the session will fail and where
                     to look.
  ensure_server()    a spawn that never started a child (a ServerEnvError) is the spawn
                     failure at once, without the ~20 s wait for a server that cannot come.

Kept from the pre-SDK version of this file: the version-number test, TestProxyWiring
(the port wiring through main(), the backend URL) and TestBackendlessProxyHandshake, the
last now completing a real handshake against the SDK-built proxy. The rest of that file
moved with its code: the /timing and /health tests to test_proxy_side_channel.py, the
backend client's to test_proxy_backend.py, the middleware tests to test_proxy_front.py.
"""

import _thread
import asyncio
import io
import json
import os
import socket
import subprocess
import sys
import textwrap
import threading
import time
from unittest.mock import AsyncMock, patch

import pytest
from mcp import Client

from browserplug_shared import server_env
from launcher import launcher
from launcher.client_pid import client_pid
from server.proxy import policy
from server.proxy.front import ProxyServer


@pytest.fixture(autouse=True)
def _select_the_proxy_transport(monkeypatch):
    """Every test in this file is about the proxy transport, so ask for it by name.

    main() dispatches on BROWSERPLUG_LAUNCHER_RELAY; without this the main()-driven tests
    would route to the relay and block on a real stdin read instead of exercising the wiring
    they assert. Pinning the variable rather than reaching past main() keeps them testing
    the entry point a client actually starts.
    """
    monkeypatch.setenv(launcher.RELAY_ENV, "0")


@pytest.fixture(autouse=True)
def _no_state_from_another_test(monkeypatch):
    """main() records whether it found a server, and a failed spawn keeps its error, in
    module globals that serve_via_proxy reads. Each test starts with neither."""
    monkeypatch.setattr(launcher, "server_found", None)
    monkeypatch.setattr(launcher, "spawn_error", None)
    monkeypatch.setattr(launcher, "spawn_failure", None)


class _Child:
    def __init__(self, code=0):
        self.code = code
        self.terminated = False

    def wait(self, timeout=None):
        return self.code

    def terminate(self):
        self.terminated = True


class _Popen:
    """subprocess.Popen as the launcher calls it, recorded."""

    def __init__(self, code=0, error=None):
        self.calls = []
        self.code = code
        self.error = error

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        if self.error is not None:
            raise self.error
        return _Child(self.code)


@pytest.fixture
def popen(monkeypatch):
    fake = _Popen()
    monkeypatch.setattr(launcher.subprocess, "Popen", fake)
    monkeypatch.setattr(launcher, "resolve_server_python", lambda *a, **k: "server-env-python")
    monkeypatch.setattr(launcher, "existing_server_python", lambda *a, **k: "server-env-python")
    monkeypatch.setattr(launcher, "browserplug_health", lambda port: {})
    return fake


def _dead_port():
    with socket.socket() as probe:                      # a port with nothing behind it
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _leaf_messages(error):
    if isinstance(error, BaseExceptionGroup):
        return [m for inner in error.exceptions for m in _leaf_messages(inner)]
    return [str(error)]


def _port_of(command):
    url = command[command.index("--backend-url") + 1]
    return int(url.rsplit(":", 1)[1].split("/")[0])


def test_the_launcher_and_the_server_share_one_version_number():
    from server.reporting import reports
    assert launcher.BROWSERPLUG_VERSION == reports.BROWSERPLUG_VERSION, (
        "both import shared-py/browserplug_shared/version.py; two different values here "
        "would mean someone re-declared it locally")


# --------------------------------------------------------------------------- #
# the child
# --------------------------------------------------------------------------- #

class TestProxyWiring:

    def test_backend_url_is_the_http_mcp_endpoint(self):
        assert launcher.backend_url(2830) == "http://127.0.0.1:2830/mcp"
        assert launcher.health_url(2830) == "http://127.0.0.1:2830/health"

    def test_main_wires_the_proxy_to_the_discovered_port(self, popen):
        """Drive main() with the server discovery stubbed and the child stubbed out: what
        is left is exactly the wiring under test."""
        with patch.object(launcher, "ensure_server", return_value=2833):
            assert launcher.main() == 0

        [(command, kwargs)] = popen.calls
        assert command == ["server-env-python", launcher.PROXY_ENTRY,
                           "--backend-url", "http://127.0.0.1:2833/mcp",
                           "--client-pid", str(client_pid()),
                           "--launcher-pid", str(os.getpid()),
                           "--launcher-version", launcher.BROWSERPLUG_VERSION]
        assert kwargs["stdin"] == 0 and kwargs["stdout"] == 1, (
            "the child serves the client's MCP session on THIS process's stdio")
        assert "stderr" not in kwargs, "stderr is inherited: the launcher's log"
        assert kwargs["cwd"] == launcher.PROJECT_ROOT, "never the client's project dir"
        if os.name == "nt":
            assert kwargs["creationflags"] == launcher.CREATE_NO_WINDOW

    def test_the_childs_exit_code_is_the_launchers(self, popen, monkeypatch):
        """Past the startup window; a child that dies inside it is refused instead
        (test_a_child_that_dies_at_startup_gets_the_initialize_refused)."""
        monkeypatch.setattr(launcher, "PROXY_STARTUP_SECONDS", 0)
        popen.code = 5
        with patch.object(launcher, "ensure_server", return_value=2833):
            assert launcher.main() == 5

    @pytest.mark.parametrize("found", [True, False], ids=["server_running", "no_server"])
    def test_the_interpreter_is_the_server_environments(self, popen, monkeypatch, found):
        """With a server running, the lookup that changes nothing; with none, the one a
        spawn runs. Both under a deadline, inside the client's connect budget."""
        asked = []

        def lookup(name):
            def answer(root, warn=None, deadline=None):
                asked.append((name, root))
                assert deadline is not None and 0 < deadline - time.monotonic() <= (
                    launcher.PROXY_PYTHON_SECONDS)
                return "env-python"
            return answer

        monkeypatch.setattr(launcher, "existing_server_python", lookup("existing"))
        monkeypatch.setattr(launcher, "resolve_server_python", lookup("ensure"))
        with patch.object(launcher, "ensure_server", return_value=2833 if found else None):
            launcher.main()
        assert asked == [("existing" if found else "ensure", launcher.PROJECT_ROOT)]
        assert popen.calls[0][0][0] == "env-python"

    def test_main_falls_back_to_the_default_port_when_nothing_answers(self, popen):
        """No healthy server anywhere: main() still starts the proxy (to the pin, else
        2830), whose handshake gate then fails the client's connect."""
        with patch.object(launcher, "ensure_server", return_value=None), \
             patch.object(launcher, "pinned_mcp_port", return_value=None):
            launcher.main()
        assert _port_of(popen.calls[0][0]) == launcher.MCP_PORT_DEFAULT

    def test_main_honours_a_pinned_port_in_the_fallback(self, popen):
        with patch.object(launcher, "ensure_server", return_value=None), \
             patch.object(launcher, "pinned_mcp_port", return_value=2900):
            launcher.main()
        assert _port_of(popen.calls[0][0]) == 2900

    def test_a_ctrl_c_in_the_launcher_ends_the_child(self, monkeypatch):
        """With a console of its own (CREATE_NO_WINDOW) the child never sees the launcher's
        Ctrl-C, so the launcher must end it, and must notice the Ctrl-C while it waits. On
        Windows a plain Popen.wait() is one WaitForSingleObject(INFINITE) that no Ctrl-C
        interrupts: the interrupt would land only when the 10 s child ended by itself.

        A real child, and the interrupt raised in this (the main) thread the way a Ctrl-C
        is, armed only once the child runs so it cannot land anywhere else."""
        monkeypatch.setattr(launcher, "_proxy_python", lambda: sys.executable)
        monkeypatch.setattr(launcher, "proxy_command",
                            lambda python, port: [python, "-c", "import time; time.sleep(10)"])
        real_popen = subprocess.Popen
        children, timers = [], []

        def popen_then_ctrl_c(command, **kwargs):
            children.append(real_popen(command, **kwargs))
            timers.append(threading.Timer(0.3, _thread.interrupt_main))
            timers[-1].start()
            return children[-1]

        monkeypatch.setattr(launcher.subprocess, "Popen", popen_then_ctrl_c)
        started = time.monotonic()
        try:
            code = launcher.serve_via_proxy(2830)
            took = time.monotonic() - started
            alive_after = [child.poll() is None for child in children]
        except KeyboardInterrupt:
            pytest.fail("the Ctrl-C escaped serve_via_proxy")
        finally:
            for timer in timers:
                timer.cancel()      # a child that died at once must not leave it armed
                timer.join()
            for child in children:
                if child.poll() is None:
                    child.kill()
                    child.wait()
        assert code == 130
        assert took < 5, f"the Ctrl-C was handled only after {took:.1f} s"
        assert alive_after == [False], "the child was ended, not left running"


class TestBackendlessProxyHandshake:
    """The proxy's handshake against a dead port, which every test above stubs away.

    The old proxy library changed what a launcher with no backend did three times (3.3.1
    came up with an empty tool list, 3.4 failed the handshake, 4.0 an empty list again)
    without one assertion moving, because every test stubbed the handshake. This one
    completes it with the real proxy (server/proxy/front.py) and the SDK client, in both
    protocol eras.
    """

    @pytest.mark.parametrize("mode", ["legacy", "auto"])
    async def test_a_proxy_with_no_backend_fails_the_client_handshake(self, mode):
        """Pins what launcher.main()'s fallback message describes, in both protocol eras.

        "legacy" is the initialize handshake (Claude Code); "auto" negotiates the 2026-07-28
        era through server/discover. The failure must come from the handshake itself, not
        from a later tools/list: a session that opens and lists nothing looks connected."""
        port = _dead_port()
        url = launcher.backend_url(port)
        # The config the launcher would hand its proxy child, parsed by the proxy.
        server = ProxyServer(policy.parse_args(launcher.proxy_command("python", port)[2:]),
                             report_timing=AsyncMock())
        handshake_completed = False
        with pytest.raises(BaseException) as failure:
            async with asyncio.timeout(30):
                async with Client(server, mode=mode) as client:
                    handshake_completed = True
                    await client.list_tools()
        assert not isinstance(failure.value, asyncio.TimeoutError), (
            "the handshake hung instead of failing: the fallback path would stall a "
            "client for its whole connect budget rather than reporting anything")
        assert not handshake_completed, (
            f"the {mode} handshake succeeded with no backend; the client sees a "
            f"connected server with no tools ({failure.value!r})")
        if mode == "legacy":
            # The gate's own text reaches an initialize client (the SDK client raises it
            # inside its task groups). At the 2026-07-28 era only the failure is pinned.
            messages = " | ".join(_leaf_messages(failure.value))
            assert "no MCP server answered at" in messages, messages
            assert url in messages

    def test_the_backendless_fallback_says_the_session_will_fail_and_where_to_look(
            self, popen, capsys):
        """A launcher that found no server must say so in terms a human can act on.

        This is the only channel that survives: the MCP session itself fails at handshake,
        so whatever it would have carried never reaches the client. The message therefore
        has to name the startup log and the doctor."""
        with patch.object(launcher, "ensure_server", return_value=None), \
             patch.object(launcher, "pinned_mcp_port", return_value=None):
            launcher.main()

        captured = capsys.readouterr()
        err = captured.err
        assert "ERROR" in err, "a doomed session must not be logged as a WARNING"
        assert "FAIL to initialize" in err
        assert ".startup.log" in err, "name the file that holds a pre-logging crash"
        assert launcher.RESTART_STARTUP_HINT in err, "and where a /restart's replacement writes"
        assert "preflight.py" in err, "name the doctor that explains the whole chain"
        assert captured.out == "", "stdout is the MCP channel"


# --------------------------------------------------------------------------- #
# when the proxy cannot start
# --------------------------------------------------------------------------- #

def _frames(text):
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _stdio(monkeypatch, *frames):
    """Point the launcher's stdin at `frames` and capture its stdout, both binary."""
    data = b"".join(json.dumps(f).encode() + b"\n" for f in frames) + b"not json\n"
    stdin = io.TextIOWrapper(io.BytesIO(data))
    stdout = io.TextIOWrapper(io.BytesIO())
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(sys, "stdout", stdout)
    return stdout


INITIALIZE = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                         "clientInfo": {"name": "t", "version": "0"}}}
INITIALIZED = {"jsonrpc": "2.0", "method": "notifications/initialized"}
TOOLS_LIST = {"jsonrpc": "2.0", "id": "two", "method": "tools/list"}


class TestProxyCannotStart:

    def test_no_server_environment_refuses_the_handshake_with_the_remedy(self, popen,
                                                                        monkeypatch, capsys):
        """No python for the proxy (no uv, a stale lock): the client's initialize fails
        with the reason and its remedy, the way a backendless proxy's does, instead of a
        launcher that just exits and leaves the client a closed pipe."""
        def refuse(*a, **k):
            raise server_env.ServerEnvError("uv was not found", kind="uv_missing",
                                            remedy="install uv")
        monkeypatch.setattr(launcher, "resolve_server_python", refuse)
        stdout = _stdio(monkeypatch, INITIALIZE, INITIALIZED, TOOLS_LIST)

        assert launcher.serve_via_proxy(2830) == 1

        assert popen.calls == [], "nothing to start"
        stdout.flush()
        replies = _frames(stdout.buffer.getvalue().decode())
        assert [r["id"] for r in replies] == [1, "two"], "requests only, never the notification"
        for reply in replies:
            assert reply["error"]["code"] == -32603
            message = reply["error"]["message"]
            assert "uv was not found" in message and "Fix: install uv" in message
            assert "BROWSERPLUG_LAUNCHER_RELAY" in message, "and how to fall back to the relay"
        err = capsys.readouterr().err
        assert "ERROR" in err and "uv was not found" in err

    def test_a_child_that_cannot_be_started_refuses_the_handshake_too(self, popen,
                                                                      monkeypatch):
        popen.error = FileNotFoundError(2, "No such file or directory")
        stdout = _stdio(monkeypatch, INITIALIZE)
        assert launcher.serve_via_proxy(2830) == 1
        stdout.flush()
        [reply] = _frames(stdout.buffer.getvalue().decode())
        assert "did not start" in reply["error"]["message"]
        assert "server-env-python" in reply["error"]["message"]


# --------------------------------------------------------------------------- #
# a spawn that never started (ensure_server)
# --------------------------------------------------------------------------- #

class TestSpawnThatNeverStarted:

    @pytest.fixture
    def no_server(self, monkeypatch):
        monkeypatch.delenv(launcher.MCP_PORT_ENV, raising=False)

        def no_sleep(seconds):
            raise AssertionError(f"waited {seconds}s for a server that was never spawned")
        monkeypatch.setattr(launcher.time, "sleep", no_sleep)

        def refuse(*a, **k):
            raise server_env.ServerEnvError("server/server.py.lock does not match",
                                            kind="lock_stale",
                                            remedy="run `uv lock --script server/server.py`")
        monkeypatch.setattr(launcher, "resolve_server_python", refuse)

    def test_a_server_env_error_is_the_spawn_failure_at_once(self, no_server, monkeypatch,
                                                             capsys):
        monkeypatch.setattr(launcher, "find_server_port", lambda *a, **k: None)
        assert launcher.ensure_server() is None
        assert "server/server.py.lock does not match" in launcher.spawn_failure
        assert "uv lock --script server/server.py" in launcher.spawn_failure, "the remedy"
        captured = capsys.readouterr()
        assert "spawn failed" in captured.err and "lock does not match" in captured.err
        assert captured.out == "", "stdout is the MCP channel"

    def test_the_failure_reaches_mains_error(self, no_server, monkeypatch, capsys):
        monkeypatch.setattr(launcher, "find_server_port", lambda *a, **k: None)
        monkeypatch.setattr(launcher, "serve_via_proxy", lambda port: 0)
        launcher.main()
        err = capsys.readouterr().err
        line = next(ln for ln in err.splitlines() if "no healthy BrowserPlug server" in ln)
        assert "uv lock --script server/server.py" in line

    def test_a_peer_server_found_by_the_last_seek_is_used(self, no_server, monkeypatch):
        answers = iter([None, 2840])
        monkeypatch.setattr(launcher, "find_server_port", lambda *a, **k: next(answers))
        assert launcher.ensure_server() == 2840
        assert launcher.spawn_failure is None


# --------------------------------------------------------------------------- #
# a real child on real pipes
# --------------------------------------------------------------------------- #

STUB_PROXY = textwrap.dedent("""
    import os, sys
    line = sys.stdin.readline()
    sys.stdout.write(line.upper())
    sys.stdout.write(os.getcwd() + "\\n")
    sys.stdout.write(" ".join(sys.argv[1:]) + "\\n")
    sys.stdout.flush()
    sys.exit(7)
""")

DRIVER = textwrap.dedent("""
    import sys
    sys.path[:0] = [{root!r}, {root!r} + "/shared-py"]
    import launcher.launcher as launcher
    launcher.PROXY_ENTRY = {stub!r}
    launcher.resolve_server_python = lambda *a, **k: sys.executable
    launcher.PROXY_STARTUP_SECONDS = 0      # the stub exits at once; count it as a long life
    sys.exit(launcher.serve_via_proxy(2851))
""")


def test_a_real_child_serves_the_launchers_own_stdio_and_its_exit_code_is_returned(tmp_path):
    """The child reads the launcher's stdin and writes its stdout (inherited, not
    relayed), runs in the checkout, gets the arguments, and once it has lived past the
    startup window its exit status is the launcher's."""
    stub = tmp_path / "stub_proxy.py"
    stub.write_text(STUB_PROXY)
    driver = tmp_path / "driver.py"
    driver.write_text(DRIVER.format(root=launcher.PROJECT_ROOT, stub=str(stub)))
    out = subprocess.run([sys.executable, str(driver)], input=b"hello proxy\n",
                         capture_output=True, timeout=120, cwd=str(tmp_path))
    assert out.returncode == 7, out.stderr
    lines = out.stdout.decode().splitlines()
    assert lines[0] == "HELLO PROXY"
    assert os.path.normcase(lines[1]) == os.path.normcase(launcher.PROJECT_ROOT)
    assert "--backend-url http://127.0.0.1:2851/mcp" in lines[2]


# A proxy child that dies before it reads anything: an import error in server/proxy/, a
# server environment without the SDK, a PYTHONHOME.
DYING_DRIVER = textwrap.dedent("""
    import os, sys
    sys.path[:0] = [{root!r}, {root!r} + "/shared-py"]
    import launcher.launcher as launcher
    launcher.resolve_server_python = lambda *a, **k: sys.executable
    launcher.proxy_command = lambda python, port: [python, "-c", {child!r}]
    os.environ.update({extra_env!r})
    sys.exit(launcher.serve_via_proxy(2851))
""")


def _run_driver(tmp_path, child, stdin, extra_env=None):
    driver = tmp_path / "proxy_driver.py"
    driver.write_text(DYING_DRIVER.format(root=launcher.PROJECT_ROOT, child=child,
                                          extra_env=extra_env or {}))
    return subprocess.run([sys.executable, str(driver)], input=stdin, capture_output=True,
                          timeout=120, cwd=str(tmp_path))


def test_a_child_that_dies_at_startup_gets_the_initialize_refused(tmp_path):
    """The client's initialize is still unread on stdin when the child dies, so the
    launcher answers it with the reason, as for a child that could not start at all,
    instead of exiting and leaving the client a closed pipe and no error."""
    initialize = json.dumps(INITIALIZE).encode() + b"\n"
    out = _run_driver(tmp_path, "import sys; sys.stderr.write('boom\\n'); sys.exit(3)",
                      initialize)
    assert out.returncode == 1, out.stderr
    [reply] = _frames(out.stdout.decode())
    assert reply["id"] == 1 and reply["error"]["code"] == -32603
    message = reply["error"]["message"]
    assert "exited with code 3" in message and "stderr log" in message, message
    assert "BROWSERPLUG_LAUNCHER_RELAY" in message
    assert b"boom" in out.stderr, "the child's own stderr is the launcher's log"


class TestProxyChildEnvironment:
    """The proxy child runs the server environment's python, so it gets the variables a
    spawned server gets: never PYTHONHOME or PYTHONEXECUTABLE (mcp_port.SPAWN_ENV_DENIED),
    which server_env's import probe leaves out as well."""

    def test_the_spawn_kwargs_drop_pythonhome_and_keep_the_rest(self, monkeypatch):
        monkeypatch.setenv("PYTHONHOME", "C:\\another\\python")
        monkeypatch.setenv("PYTHONEXECUTABLE", "C:\\another\\python.exe")
        monkeypatch.setenv("BROWSERPLUG_CHILD_ENV_PROBE", "kept")
        env = launcher._proxy_spawn_kwargs()["env"]
        assert not {name.upper() for name in env} & {"PYTHONHOME", "PYTHONEXECUTABLE"}
        assert env["BROWSERPLUG_CHILD_ENV_PROBE"] == "kept"
        assert any(name.upper() == "PATH" for name in env)

    def test_a_pythonhome_meant_for_the_launcher_does_not_reach_the_child(self, tmp_path):
        """End to end: a python that inherits a PYTHONHOME with no standard library under
        it dies before it runs a line."""
        out = _run_driver(tmp_path,
                          "import os; print('child ran', os.environ.get('PYTHONHOME'))", b"",
                          extra_env={"PYTHONHOME": str(tmp_path / "no-stdlib-here")})
        assert out.returncode == 0, out.stderr
        assert out.stdout.decode().split() == ["child", "ran", "None"]


# --------------------------------------------------------------------------- #
# finding the proxy's interpreter
# --------------------------------------------------------------------------- #

class TestProxyInterpreter:
    """The real server_env code with uv replaced by a recording runner and a scratch
    environment in a scratch state directory: nothing on this machine is asked or changed."""

    @pytest.fixture
    def uv(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BROWSERPLUG_STATE_DIR", str(tmp_path / "state"))
        env_dir = tmp_path / "state" / server_env.SERVER_ENV_DIR_NAME
        python = env_dir.joinpath(*(("Scripts", "python.exe") if os.name == "nt"
                                    else ("bin", "python")))
        python.parent.mkdir(parents=True)
        python.write_text("")
        (env_dir / "pyvenv.cfg").write_text("home = x\n")
        env_dir.joinpath(*(("Lib", "site-packages") if os.name == "nt"
                           else ("lib", "python3.12", "site-packages"))).mkdir(parents=True)
        calls = []

        def runner(argv, *, cwd, timeout, env):
            calls.append(list(argv))
            if argv[1:2] == ["sync"] and "--check" in argv:
                return 0, "", f"Would use script environment at: {env_dir}\n"
            return 0, "", ""            # a sync, an import probe: whatever else would run

        monkeypatch.setattr(server_env, "_run", runner)
        monkeypatch.delenv(server_env.SERVER_PYTHON_ENV, raising=False)
        monkeypatch.setenv(server_env.UV_ENV, sys.executable)     # any existing file
        monkeypatch.setattr(launcher, "browserplug_health", lambda port: {})
        fake = _Popen()
        monkeypatch.setattr(launcher.subprocess, "Popen", fake)
        return {"python": str(python), "calls": calls, "popen": fake}

    def test_a_running_server_is_joined_without_syncing_its_environment(self, uv):
        """`uv sync` under a running server swaps its packages in place, and for an
        interpreter change deletes its Lib/ before failing on the locked trampoline. The
        environment's place is known, so joining asks uv nothing at all."""
        with patch.object(launcher, "ensure_server", return_value=2833):
            assert launcher.main() == 0
        assert uv["calls"] == []
        [(command, _kwargs)] = uv["popen"].calls
        assert command[0] == uv["python"]

    def test_a_spawn_that_failed_on_the_environment_is_not_looked_up_again(self, uv,
                                                                           monkeypatch):
        lookups = []

        def refuse(root, **kw):
            lookups.append(root)
            raise server_env.ServerEnvError("server/server.py.lock does not match",
                                            kind="lock_stale",
                                            remedy="run `uv lock --script server/server.py`")

        monkeypatch.setattr(server_env, "ensure_server_python", refuse)
        monkeypatch.delenv(launcher.MCP_PORT_ENV, raising=False)
        monkeypatch.setattr(launcher, "find_server_port", lambda *a, **k: None)
        monkeypatch.setattr(launcher, "browserplug_health", lambda port: None)

        def no_sleep(seconds):
            raise AssertionError(f"waited {seconds}s for a server that was never spawned")
        monkeypatch.setattr(launcher.time, "sleep", no_sleep)
        stdout = _stdio(monkeypatch, INITIALIZE)

        assert launcher.main() == 1
        assert len(lookups) == 1, "the spawn's ServerEnvError is the proxy's answer too"
        assert uv["popen"].calls == []
        stdout.flush()
        [reply] = _frames(stdout.buffer.getvalue().decode())
        assert "lock does not match" in reply["error"]["message"]
        assert "uv lock --script server/server.py" in reply["error"]["message"]
