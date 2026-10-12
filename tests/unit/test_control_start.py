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
