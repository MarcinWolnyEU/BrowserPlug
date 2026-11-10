"""`python server/control.py start`: what a server starter runs.

It reports a server that already answers, and otherwise runs the real launcher with nothing
on its stdin, so the seek, the environment build and the detached spawn are the launcher's
own. The last test runs it end to end against a real server.py on a sandbox's ports
(test_server_process_lifecycle.Sandbox fences it off from the live server); the only
substitution is the launcher's spawn target, a stub that adds the sandbox's WebSocket port,
because the launcher leaves that port at the live server's 8772.
"""

import json
import os
import subprocess
import sys

import pytest

import test_imports  # noqa: F401
from browserplug_shared import local_auth, proc_facts
from browserplug_shared.mcp_port import DETACHED_ENV, MCP_PORT_ENV, browserplug_health
from browserplug_shared.ws_token import WS_HOST_ENV, WS_TOKEN_ENV
from server import control

from test_server_process_lifecycle import ROOT, SERVER_SCRIPT, Sandbox, sandbox  # noqa: F401

HEALTH = {"service": "browserplug", "status": "ok", "server_pid": 4242,
          "server_version": "1.0"}


@pytest.fixture(autouse=True)
def _no_settle(monkeypatch):
    """`start` pauses before it calls a found server running; the tests that are not about
    that pause do not wait for it."""
    monkeypatch.setattr(control, "START_SETTLE_SECONDS", 0)


class _Launcher:
    """A stand-in for subprocess.Popen of the launcher: records how it was started."""

    def __init__(self, on_run=None, stderr=b"[browserplug-launcher] launcher v1.0\r\n"):
        self.calls, self.on_run, self.lines = [], on_run, [stderr]

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if self.on_run:
            self.on_run()
        launcher = self

        class Child:
            stderr = iter(launcher.lines)
            returncode = 0

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False
        return Child()


def test_a_running_server_is_reported_and_no_launcher_runs(monkeypatch, capsys):
    monkeypatch.setattr(control, "find_our_server", lambda: (2833, HEALTH))
    launcher = _Launcher()
    monkeypatch.setattr(control.subprocess, "Popen", launcher)
    assert control.main(["start"]) == 0
    assert launcher.calls == []
    assert json.loads(capsys.readouterr().out) == {
        "status": "running", "port": 2833, "server_pid": 4242, "server_version": "1.0"}


def test_with_no_server_the_launcher_runs_with_nothing_on_its_stdin(monkeypatch, capsys):
    answers = [(None, None), (2830, HEALTH)]
    monkeypatch.setattr(control, "find_our_server", lambda: answers.pop(0))
    launcher = _Launcher()
    monkeypatch.setattr(control.subprocess, "Popen", launcher)
    assert control.main(["start"]) == 0
    [(argv, kwargs)] = launcher.calls
    assert argv == [sys.executable, os.path.join(str(ROOT), "launcher", "launcher.py")]
    assert kwargs["stdin"] is subprocess.DEVNULL and kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.PIPE and kwargs["cwd"] == str(ROOT)
    out, err = capsys.readouterr()
    assert json.loads(out)["status"] == "started"
    assert "[browserplug-launcher] launcher v1.0" in err
    assert "\r" not in err        # the launcher's text-mode CRLF must not become blank lines


def test_a_launcher_that_brought_up_no_server_fails_the_start(monkeypatch, capsys):
    monkeypatch.setattr(control, "find_our_server", lambda: (None, None))
    monkeypatch.setattr(control.subprocess, "Popen", _Launcher(
        stderr=b"[browserplug-launcher] ERROR: spawn failed: env_in_use\n"))
    assert control.main(["start"]) == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert "spawn failed: env_in_use" in err and "start failed" in err


def test_a_server_that_is_going_away_is_not_reported_running(monkeypatch, capsys):
    """`shutdown` answers before the server exits, and /health keeps answering for a moment.
    A `start` that trusted that one read printed "running" and left no server behind. It
    reads again after a pause, and a server that is gone by then is started."""
    monkeypatch.setattr(control, "START_SETTLE_SECONDS", 0.05)
    answers = [(2830, HEALTH), (None, None), (2830, HEALTH)]
    monkeypatch.setattr(control, "find_our_server", lambda: answers.pop(0))
    launcher = _Launcher()
    monkeypatch.setattr(control.subprocess, "Popen", launcher)
    assert control.main(["start"]) == 0
    assert len(launcher.calls) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "started"


class _Lines:
    """The launcher's stderr pipe: records how far it was read."""

    def __init__(self, lines):
        self.lines, self.read = list(lines), 0

    def __iter__(self):
        for line in self.lines:
            self.read += 1
            yield line


@pytest.mark.parametrize("broken", ["none", "raises"])
def test_start_reads_the_launchers_log_to_the_end_even_with_no_stderr(
        broken, monkeypatch, capsys):
    """Under pythonw.exe, or a starter with no std handles, sys.stderr is None. Writing to it
    crashed `start`, the pipe closed, and the launcher died on its next log line before it
    had spawned the server. The pipe is drained whatever happens to the copy."""
    class Broken:
        def write(self, text):
            raise OSError("handle closed")

        def flush(self):
            pass

    monkeypatch.setattr(sys, "stderr", None if broken == "none" else Broken())
    answers = [(None, None), (2830, HEALTH)]
    monkeypatch.setattr(control, "find_our_server", lambda: answers.pop(0))
    pipe = _Lines([b"[browserplug-launcher] one\r\n", b"[browserplug-launcher] two\r\n",
                   b"[browserplug-launcher] spawned\r\n"])
    launcher = _Launcher()
    launcher.lines = pipe
    monkeypatch.setattr(control.subprocess, "Popen", launcher)
    assert control.main(["start"]) == 0
    assert pipe.read == 3
    assert json.loads(capsys.readouterr().out)["status"] == "started"


def test_only_this_checkouts_server_counts(monkeypatch):
    """The launcher's own test: a BrowserPlug-shaped /health of another process is not a
    running server to report."""
    seen = []
    monkeypatch.delenv(MCP_PORT_ENV, raising=False)

    def seek(accept=None, **_kw):
        seen.append(accept({"server_pid": os.getpid()}))
        return None, None
    monkeypatch.setattr(control.mcp_port, "seek_browserplug", seek)
    assert control.find_our_server() == (None, None)
    assert seen == [False]          # this pytest process names no server.py


STUB = """\
import subprocess, sys
sys.exit(subprocess.call([sys.executable, {server!r}, *sys.argv[1:], "--port", {ws!r}]))
"""

WRAPPER = """\
import sys
sys.path.insert(0, {root!r})
import launcher.launcher as launcher
launcher.SERVER = {stub!r}
sys.exit(launcher.main())
"""


def _wire_launcher(sandbox, tmp_path, monkeypatch):  # noqa: F811
    """Point `start` at the sandbox: its launcher spawns a server on the sandbox's ports."""
    stub = tmp_path / "spawn_stub.py"
    stub.write_text(STUB.format(server=str(SERVER_SCRIPT), ws=str(sandbox.ws_port)))
    wrapper = tmp_path / "launcher_wrapper.py"
    wrapper.write_text(WRAPPER.format(root=str(ROOT), stub=str(stub)))
    monkeypatch.setattr(control, "LAUNCHER", str(wrapper))
    for name in (WS_TOKEN_ENV, WS_HOST_ENV, DETACHED_ENV):
        monkeypatch.delenv(name, raising=False)
    for name, value in sandbox.env(**{MCP_PORT_ENV: str(sandbox.mcp_port)}).items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(local_auth, "_master_cache", None)


def test_start_brings_up_a_real_server_and_then_reports_it(sandbox, tmp_path,  # noqa: F811
                                                           monkeypatch, capsys):
    _wire_launcher(sandbox, tmp_path, monkeypatch)
    assert control.main(["start"]) == 0, sandbox.diagnostics()
    started = json.loads(capsys.readouterr().out)
    health = sandbox.wait_healthy()
    assert started["status"] == "started" and started["port"] == sandbox.mcp_port
    assert started["server_pid"] == health["server_pid"]

    assert control.main(["start"]) == 0
    again = json.loads(capsys.readouterr().out)
    assert again["status"] == "running" and again["server_pid"] == health["server_pid"]


def test_shutdown_returns_only_when_the_server_is_gone_so_start_works_at_once(
        sandbox, tmp_path, monkeypatch, capsys):  # noqa: F811
    """The sequence the environment errors tell people to run, back to back with no pause:
    `shutdown`, then `start`. `shutdown` used to return while /health still answered for a
    third of a second; `start` took that for a running server and the server then exited."""
    _wire_launcher(sandbox, tmp_path, monkeypatch)
    assert control.main(["start"]) == 0, sandbox.diagnostics()
    first = json.loads(capsys.readouterr().out)

    assert control.main(["shutdown"]) == 0, sandbox.diagnostics()
    capsys.readouterr()
    # Returned: nothing answers and the process is gone, with no waiting here.
    assert browserplug_health(sandbox.mcp_port, timeout=1.0) is None
    assert not proc_facts.pid_alive(first["server_pid"])

    assert control.main(["start"]) == 0, sandbox.diagnostics()
    second = json.loads(capsys.readouterr().out)
    assert second["status"] == "started" and second["server_pid"] != first["server_pid"]
    assert sandbox.wait_healthy()["server_pid"] == second["server_pid"]


def test_a_shutdown_the_server_does_not_act_on_fails_instead_of_claiming_success(
        monkeypatch, capsys):
    monkeypatch.setattr(control, "SHUTDOWN_WAIT_SECONDS", 0.3)
    monkeypatch.setattr(control, "find_port", lambda: 2830)
    monkeypatch.setattr(control, "master_token", lambda: "token")
    monkeypatch.setattr(control, "call", lambda action, port, timeout=30: (200, {"ok": 1}))
    monkeypatch.setattr(control.mcp_port, "browserplug_health", lambda port, **kw: HEALTH)
    monkeypatch.setattr(control.proc_facts, "pid_alive", lambda pid: True)
    assert control.main(["shutdown"]) == 1
    out, err = capsys.readouterr()
    assert out == "" and "still answers on port 2830" in err


def test_shutdown_waits_for_the_process_as_well_as_for_health(monkeypatch, capsys):
    """/health can go quiet before the process has exited; its locks are held until it has."""
    monkeypatch.setattr(control, "find_port", lambda: 2830)
    monkeypatch.setattr(control, "master_token", lambda: "token")
    monkeypatch.setattr(control, "call", lambda action, port, timeout=30: (200, {"ok": 1}))
    health = [HEALTH, None]            # one read before the call, then it is gone
    monkeypatch.setattr(control.mcp_port, "browserplug_health",
                        lambda port, **kw: health.pop(0) if len(health) > 1 else None)
    alive = [True, True, False]
    seen = []
    monkeypatch.setattr(control.proc_facts, "pid_alive",
                        lambda pid: seen.append(pid) or alive.pop(0))
    assert control.main(["shutdown"]) == 0
    assert seen == [4242, 4242, 4242]
    assert json.loads(capsys.readouterr().out) == {"ok": 1}


class _Restarting:
    """A server that is restarting: its /shutdown answers 409 `refusals` times, then the old
    process is gone for `gap` calls (connection refused), then the replacement answers."""

    def __init__(self, refusals=2, gap=1):
        self.refusals, self.gap, self.posts = refusals, gap, []

    def call(self, action, port, timeout=30):
        self.posts.append((action, port))
        if self.refusals:
            self.refusals -= 1
            return 409, {"error": "a restart is in progress; retry the shutdown"}
        if self.gap:
            self.gap -= 1
            raise ConnectionRefusedError("old server gone, replacement not bound yet")
        return 200, {"status": "shutdowning", "pid": 5151}

    def health(self, port, **kw):
        """The old server (pid 4242) until the connection is refused, then the replacement."""
        return {**HEALTH, "server_pid": 5151} if len(self.posts) >= 3 else HEALTH


def test_shutdown_during_a_restart_waits_and_stops_the_replacement(monkeypatch, capsys):
    """`control.py restart` then `shutdown` at once used to stop only the old server, and the
    replacement it had spawned kept the ports. The 409 is retried until the replacement
    answers, and that is the server `shutdown` stops and waits for."""
    fake = _Restarting()
    monkeypatch.setattr(control, "RESTART_RETRY_SECONDS", 0.01)
    monkeypatch.setattr(control, "find_port", lambda: 2830)
    monkeypatch.setattr(control, "master_token", lambda: "token")
    monkeypatch.setattr(control, "call", fake.call)
    monkeypatch.setattr(control.mcp_port, "browserplug_health", fake.health)
    waited = []

    def wait_stopped(port, pid, timeout=None):
        waited.append(pid)
        return True

    monkeypatch.setattr(control, "wait_stopped", wait_stopped)
    assert control.main(["shutdown"]) == 0
    assert [a for a, _ in fake.posts] == ["shutdown"] * 4        # 409, 409, refused, accepted
    assert waited == [5151], "it waits for the replacement, not for the old pid"
    assert json.loads(capsys.readouterr().out)["pid"] == 5151


def test_shutdown_gives_up_on_a_restart_that_never_ends(monkeypatch, capsys):
    monkeypatch.setattr(control, "RESTART_WAIT_SECONDS", 0.1)
    monkeypatch.setattr(control, "RESTART_RETRY_SECONDS", 0.01)
    monkeypatch.setattr(control, "find_port", lambda: 2830)
    monkeypatch.setattr(control, "master_token", lambda: "token")
    monkeypatch.setattr(control, "call", lambda action, port, timeout=30: (
        409, {"error": "a restart is in progress"}))
    monkeypatch.setattr(control.mcp_port, "browserplug_health", lambda port, **kw: HEALTH)
    assert control.main(["shutdown"]) == 1
    out, err = capsys.readouterr()
    assert out == "" and "refused with HTTP 409" in err and "restart is in progress" in err


class _Scripted:
    """/shutdown answers and /health pids, step by step. A step is an (answer, health pid)
    pair: `answer` is a (status, body) tuple or an exception to raise, and the pid is what
    /health reports just before that call (None = /health does not answer). The last step
    repeats."""

    def __init__(self, *steps):
        self.steps, self.index = list(steps), 0

    def _step(self):
        return self.steps[min(self.index, len(self.steps) - 1)]

    def health(self, port, **kw):
        pid = self._step()[1]
        return None if pid is None else {**HEALTH, "server_pid": pid}

    def call(self, action, port, timeout=30):
        answer = self._step()[0]
        self.index += 1
        if isinstance(answer, Exception):
            raise answer
        return answer


def _wire_scripted(monkeypatch, fake, waited):
    monkeypatch.setattr(control, "RESTART_RETRY_SECONDS", 0.01)
    monkeypatch.setattr(control, "find_port", lambda: 2830)
    monkeypatch.setattr(control, "master_token", lambda: "token")
    monkeypatch.setattr(control, "call", fake.call)
    monkeypatch.setattr(control.mcp_port, "browserplug_health", fake.health)

    def wait_stopped(port, pid, timeout=None):
        waited.append(pid)
        return True

    monkeypatch.setattr(control, "wait_stopped", wait_stopped)


_BUSY = (409, {"error": "a restart is in progress; retry the shutdown"})


def test_a_gap_that_outlasts_the_budget_after_a_409_is_not_a_shutdown(monkeypatch, capsys):
    """The old server stopped answering and the replacement had not bound when the budget ran
    out: /health fails, so there is no pid to wait for and wait_stopped(None) would return at
    once. That used to print success and exit 0 while the replacement bound and kept running."""
    waited = []
    fake = _Scripted((_BUSY, 4242), (ConnectionRefusedError("gap"), None))
    _wire_scripted(monkeypatch, fake, waited)
    monkeypatch.setattr(control, "RESTART_WAIT_SECONDS", 0.2)
    assert control.main(["shutdown"]) == 1
    out, err = capsys.readouterr()
    assert out == "" and "restart was still in progress" in err
    assert waited == [], "nothing was accepted, so there is nothing to wait for"


def test_a_gap_with_no_health_is_waited_out_and_the_replacement_is_stopped(
        monkeypatch, capsys):
    waited = []
    gap = (ConnectionRefusedError("gap"), None)
    fake = _Scripted((_BUSY, 4242), gap, gap, ((200, {"pid": 5151}), 5151))
    _wire_scripted(monkeypatch, fake, waited)
    assert control.main(["shutdown"]) == 0
    assert waited == [5151]
    assert json.loads(capsys.readouterr().out)["pid"] == 5151


def test_a_connection_the_replacement_drops_while_accepting_counts_as_accepted(
        monkeypatch, capsys):
    """After a 409 the same rule as before it holds for a server that did not say 409: a
    dropped connection is the server leaving. The replacement is the one to wait for."""
    waited = []
    fake = _Scripted((_BUSY, 4242), (ConnectionResetError("reset"), 5151))
    _wire_scripted(monkeypatch, fake, waited)
    assert control.main(["shutdown"]) == 0
    assert waited == [5151]
    assert capsys.readouterr().out.strip() == "shutdown requested"


def test_the_old_server_dropping_connections_as_it_leaves_is_not_an_acceptance(
        monkeypatch, capsys):
    """The server that said 409 resets connections while it closes its HTTP side; that is the
    restart going on, not a stop that went through."""
    waited = []
    fake = _Scripted((_BUSY, 4242), (ConnectionResetError("reset"), 4242),
                     ((200, {"pid": 5151}), 5151))
    _wire_scripted(monkeypatch, fake, waited)
    assert control.main(["shutdown"]) == 0
    assert waited == [5151], "the reset was retried; it was not taken as the stop"


def test_the_restart_wait_outlasts_a_worst_case_restart():
    """`shutdown` keeps retrying a 409 for RESTART_WAIT_SECONDS. A restart can spend its full
    drain, the replacement's ready-marker wait, the socket close and the HTTP stop before the
    replacement binds, so the budget has to cover their sum with room to spare."""
    from server.runtime import graceful_restart, lifecycle
    worst = (graceful_restart.DRAIN_MAX_SECONDS + lifecycle.REPLACEMENT_READY_SECONDS
             + graceful_restart.CLOSE_TIMEOUT_SECONDS
             + graceful_restart.HTTP_STOP_TIMEOUT_SECONDS)
    assert control.RESTART_WAIT_SECONDS >= worst + 15.0


def test_a_dropped_connection_without_a_restart_still_counts_as_a_shutdown(monkeypatch):
    """The old reading stays: no 409 seen, so a dropped connection is the server leaving."""
    def dropped(action, port, timeout=30):
        raise ConnectionResetError("reset while answering")

    monkeypatch.setattr(control, "find_port", lambda: 2830)
    monkeypatch.setattr(control, "master_token", lambda: "token")
    monkeypatch.setattr(control, "call", dropped)
    monkeypatch.setattr(control.mcp_port, "browserplug_health", lambda port, **kw: None)
    monkeypatch.setattr(control, "wait_stopped", lambda port, pid, timeout=None: True)
    assert control.main(["shutdown"]) == 0
