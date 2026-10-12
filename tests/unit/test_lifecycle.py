"""server/runtime/lifecycle.py: logging setup, the restart argv and interpreter, and the ways
to start the server."""

import asyncio
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import test_imports  # noqa: F401
from browserplug_shared import server_env
from server.runtime import lifecycle
from server.runtime.log_handler import SafeRotatingFileHandler
from server.server import BrowserPlugServer

ROOT = Path(__file__).resolve().parents[2]


def _strip(root):
    """Remove every handler from `root`. pytest re-attaches its capture handlers at the
    start of the test call, after fixtures ran, and logging.basicConfig does nothing when
    the root has handlers, so a test that exercises setup_logging strips them itself."""
    for h in list(root.handlers):
        root.removeHandler(h)


@pytest.fixture
def clean_root_logger():
    """The root logger, with its handlers and level put back afterwards."""
    root = logging.getLogger()
    saved, level = list(root.handlers), root.level
    try:
        yield root
    finally:
        for h in list(root.handlers):
            root.removeHandler(h)
            if isinstance(h, SafeRotatingFileHandler):
                h.close()
        for h in saved:
            root.addHandler(h)
        root.setLevel(level)


def _console_handlers(root):
    """Handlers writing to stderr (pytest's own capture handlers write elsewhere)."""
    return [h for h in root.handlers
            if isinstance(h, logging.StreamHandler)
            and not isinstance(h, logging.FileHandler)
            and getattr(h, "stream", None) is sys.stderr]


def test_setup_logging_attaches_the_rotating_file_once(clean_root_logger, tmp_path):
    log = tmp_path / "server.log"
    env = {"BROWSERPLUG_LOG_FILE": str(log)}
    _strip(clean_root_logger)

    lifecycle.setup_logging(env)
    lifecycle.setup_logging(env)

    files = [h for h in clean_root_logger.handlers if isinstance(h, SafeRotatingFileHandler)]
    assert len(files) == 1, "a second call doubled the handler, and so every log line"
    assert files[0].baseFilename == os.path.abspath(str(log))
    logging.getLogger("t").info("hello")
    files[0].flush()
    assert "hello" in log.read_text(encoding="utf-8")


def test_setup_logging_keeps_the_console_unless_detached(clean_root_logger, tmp_path):
    _strip(clean_root_logger)
    lifecycle.setup_logging({"BROWSERPLUG_LOG_FILE": str(tmp_path / "a.log")})
    console = _console_handlers(clean_root_logger)
    assert len(console) == 1


def test_setup_logging_drops_the_console_when_a_spawner_captured_stderr(
        clean_root_logger, tmp_path):
    _strip(clean_root_logger)
    lifecycle.setup_logging({"BROWSERPLUG_LOG_FILE": str(tmp_path / "b.log"),
                             "BROWSERPLUG_DETACHED": "1"})
    console = _console_handlers(clean_root_logger)
    assert console == []
    assert any(isinstance(h, SafeRotatingFileHandler) for h in clean_root_logger.handlers)


class _FakePopen:
    def __init__(self):
        self.args = None
        self.kwargs = None

    def __call__(self, args, **kwargs):
        self.args, self.kwargs = args, kwargs
        return SimpleNamespace(pid=1234)


def _spawn(server, tmp_path):
    fake = _FakePopen()
    with patch.dict(os.environ, {"BROWSERPLUG_LOG_FILE": str(tmp_path / "s.log")}), \
            patch.object(subprocess, "Popen", fake):
        server.lifecycle.spawn_restart_process()
    if fake.kwargs["stdout"] is not subprocess.DEVNULL:
        fake.kwargs["stdout"].close()
    return fake


def test_the_restart_replacement_runs_server_py_on_the_same_ports(tmp_path):
    """Whatever started this process, the replacement is `python server/server.py`, told
    to wait for our ports. Pinned because the launcher, the scheduled task and /restart
    all rely on that form, and a moved __file__ would silently change it."""
    server = BrowserPlugServer(host="localhost", port=8772, mcp_port=2841, start_mcp=False,
                               ws_token="t" * 32)
    fake = _spawn(server, tmp_path)

    assert fake.args[0] == sys.executable
    assert os.path.samefile(fake.args[1], ROOT / "server" / "server.py")
    assert fake.args[2:] == ["--port", str(server.port), "--mcp-port", str(server.mcp_port),
                             "--wait-for-port"]
    assert fake.kwargs["env"]["BROWSERPLUG_DETACHED"] == "1"


def test_a_ws_host_given_on_the_command_line_survives_a_restart(tmp_path):
    server = BrowserPlugServer(host="localhost", port=8772, mcp_port=2842, start_mcp=False,
                               ws_host="0.0.0.0", ws_token="t" * 32)
    assert server.ws_host_source == "arg"
    fake = _spawn(server, tmp_path)
    assert fake.args[-2:] == ["--ws-host", "0.0.0.0"]


def test_the_lifecycle_reads_the_servers_current_ports(tmp_path):
    """The hooks are read per use: a server that moved ports after construction (tests do,
    set_ws_host() does for the bind) is restarted onto what it has NOW."""
    server = BrowserPlugServer(host="localhost", port=8772, mcp_port=2843, start_mcp=False,
                               ws_token="t" * 32)
    server.port, server.mcp_port = 9001, 9002
    fake = _spawn(server, tmp_path)
    assert fake.args[2:6] == ["--port", "9001", "--mcp-port", "9002"]


def test_the_replacement_runs_on_the_interpreter_resolved_before_the_restart(tmp_path):
    """begin_restart resolves the interpreter; the spawn hook is a plain Popen of it."""
    server = BrowserPlugServer(host="localhost", port=8772, mcp_port=2844, start_mcp=False,
                               ws_token="t" * 32)
    server.lifecycle._restart_python = "C:\\env\\Scripts\\python.exe"
    fake = _FakePopen()
    with patch.dict(os.environ, {"BROWSERPLUG_LOG_FILE": str(tmp_path / "s.log")}), \
            patch.object(subprocess, "Popen", fake):
        server.lifecycle.restart_hooks().spawn_replacement()
    if fake.kwargs["stdout"] is not subprocess.DEVNULL:
        fake.kwargs["stdout"].close()
    assert fake.args[0] == "C:\\env\\Scripts\\python.exe"
    assert os.path.samefile(fake.args[1], ROOT / "server" / "server.py")


def test_restart_python_asks_server_env_rather_than_trusting_sys_executable(tmp_path,
                                                                            monkeypatch):
    """The environment this process started from may have been pruned since. It is asked of
    python_for_restart, never ensure_server_python, whose sync would change the environment
    under this very process; the deadline bounds every uv call it makes. A server running
    from a `.venv` left in the checkout is no exception."""
    seen = {}

    def for_restart(root, **kw):
        seen.update(kw, root=root)
        return "C:\\fresh\\Scripts\\python.exe"

    def ensure(*a, **k):
        raise AssertionError("a /restart must not sync the environment it runs from")
    monkeypatch.setattr(server_env, "python_for_restart", for_restart)
    monkeypatch.setattr(server_env, "ensure_server_python", ensure)
    venv = tmp_path / ".venv"
    monkeypatch.setattr(lifecycle.sys, "executable", str(venv / "Scripts" / "python.exe"))
    before = time.monotonic()
    got = lifecycle.restart_python(str(tmp_path), prefix=str(venv))
    assert got == "C:\\fresh\\Scripts\\python.exe"
    assert seen["root"] == str(tmp_path) and seen["running_prefix"] == str(venv)
    budget = seen["deadline"] - before
    assert 0 < budget <= lifecycle.RESTART_ENV_BUDGET_SECONDS + 1


def test_the_restart_wait_outlasts_the_budget_server_env_is_held_to():
    """A wait shorter than the work reports 'refused' while the worker thread carries on."""
    assert lifecycle.RESTART_ENV_TIMEOUT_SECONDS > lifecycle.RESTART_ENV_BUDGET_SECONDS
    # From the server's own environment a restart asks read-only questions only: the
    # check and, on a change, the lock check, each within the budget.
    assert lifecycle.RESTART_ENV_BUDGET_SECONDS > (server_env.SYNC_TIMEOUT_SECONDS
                                                   + server_env.FIND_TIMEOUT_SECONDS)


def _server_with_fake_sequence(monkeypatch, resolve):
    """A server whose restart sequence is recorded instead of run."""
    server = BrowserPlugServer(host="localhost", port=8772, start_mcp=False,
                               ws_token="t" * 32)
    ran, flags = [], []
    server.lifecycle.resolve_restart_python = resolve
    server.lifecycle.hooks.set_restarting = flags.append

    async def fake_perform_restart(hooks, *, trigger):
        ran.append(server.lifecycle._restart_python)
        hooks.announce(None)
    monkeypatch.setattr(lifecycle, "perform_restart", fake_perform_restart)
    return server, ran, flags


def test_a_restart_resolves_its_interpreter_before_the_sequence_begins(monkeypatch):
    server, ran, _flags = _server_with_fake_sequence(monkeypatch, lambda: "resolved-python")
    assert server.lifecycle.begin_restart("test").result(10) is None
    assert ran == ["resolved-python"]


def test_a_restart_with_no_interpreter_is_refused_while_the_server_is_whole(monkeypatch):
    """Refused BEFORE set_restarting(True) and the drain: nothing was closed, nothing was
    refused to callers, and the reason (uv's) is the verdict /restart answers with."""
    def refuse():
        raise server_env.ServerEnvError("server/server.py.lock does not match",
                                        kind="lock_stale",
                                        remedy="run `uv lock --script server/server.py`")
    server, ran, flags = _server_with_fake_sequence(monkeypatch, refuse)
    verdict = server.lifecycle.begin_restart("POST /restart").result(10)
    assert verdict.startswith("refused: no interpreter for the replacement")
    assert "uv lock --script server/server.py" in verdict
    assert ran == [] and flags == []
    assert server.lifecycle.last_restart["outcome"] == "aborted"
    assert server.lifecycle.restarting is False


def test_a_restart_whose_environment_would_change_is_refused_and_says_why(monkeypatch):
    def changed():
        raise server_env.ServerEnvError(
            "the server environment at E would change (its packages differ from the lock)",
            kind="deps_changed", remedy="stop the server (python server/control.py shutdown)")
    server, ran, flags = _server_with_fake_sequence(monkeypatch, changed)
    verdict = server.lifecycle.begin_restart("POST /restart").result(10)
    assert verdict.startswith("refused: the server environment at E would change")
    assert "no interpreter" not in verdict and "control.py shutdown" in verdict
    assert ran == [] and flags == []


def test_a_resolution_that_hangs_is_refused_within_its_bound(monkeypatch):
    monkeypatch.setattr(lifecycle, "RESTART_ENV_TIMEOUT_SECONDS", 0.2)
    server, ran, flags = _server_with_fake_sequence(monkeypatch,
                                                    lambda: time.sleep(2) or "late")
    verdict = server.lifecycle.begin_restart("POST /restart").result(10)
    assert "was not ready within" in verdict
    assert ran == [] and flags == []


def test_stopping_arms_the_death_guard_and_restart_can_disarm_it():
    server = BrowserPlugServer(host="localhost", port=8772, start_mcp=False,
                               ws_token="t" * 32)
    hooks = server.lifecycle.restart_hooks()
    assert server.lifecycle.stopping is False
    hooks.arm()
    assert server.lifecycle.stopping is True
    hooks.disarm()
    assert server.lifecycle.stopping is False


@pytest.mark.parametrize("argv", [
    [sys.executable, str(ROOT / "server" / "server.py"), "--help"],
    [sys.executable, "-m", "server", "--help"],
], ids=["script", "module"])
def test_both_ways_of_starting_the_server_reach_the_same_parser(argv, tmp_path):
    env = dict(os.environ)
    env["BROWSERPLUG_LOG_FILE"] = str(tmp_path / "help.log")
    env["BROWSERPLUG_STATE_DIR"] = str(tmp_path / "state")
    out = subprocess.run(argv, capture_output=True, text=True, env=env, cwd=str(ROOT),
                         timeout=120)
    assert out.returncode == 0, out.stderr
    for flag in ("--wait-for-port", "--mcp-port", "--ws-host", "--stdio"):
        assert flag in out.stdout


# --- the restart spawner's capture files (2026-10-02 review: one fixed file, truncated) -----

def _capture_name(fake):
    return fake.kwargs["stdout"].name


def test_each_restart_replacement_gets_a_capture_file_of_its_own(tmp_path):
    """A second /restart used to truncate the one fixed `.startup.log`, destroying the only
    traceback of a replacement that had died on the first."""
    server = BrowserPlugServer(host="localhost", port=8772, mcp_port=2844, start_mcp=False,
                               ws_token="t" * 32)
    first = _spawn(server, tmp_path)
    first_path = _capture_name(first)
    Path(first_path).write_text("traceback of the first replacement\n", encoding="utf-8")
    time.sleep(0.05)
    second = _spawn(server, tmp_path)
    assert _capture_name(second) != first_path
    assert first_path.endswith(".startup.log") and "restart-" in os.path.basename(first_path)
    assert "traceback of the first" in Path(first_path).read_text(encoding="utf-8"), (
        "the second spawn must leave the first capture alone")


def test_old_restart_captures_are_pruned_but_the_newest_few_stay(tmp_path):
    server = BrowserPlugServer(host="localhost", port=8772, mcp_port=2845, start_mcp=False,
                               ws_token="t" * 32)
    for i in range(9):
        old = tmp_path / f"s.restart-1-{i}.startup.log"
        old.write_text("x", encoding="utf-8")
        os.utime(old, (1_000_000 + i, 1_000_000 + i))
    fake = _spawn(server, tmp_path)
    left = sorted(p.name for p in tmp_path.glob("s.*.startup.log"))
    assert len(left) == lifecycle.STARTUP_LOGS_KEPT
    assert os.path.basename(_capture_name(fake)) in left
    assert "s.restart-1-8.startup.log" in left and "s.restart-1-0.startup.log" not in left


# --- the watchdog's wiring ---------------------------------------------------------------

def _standalone(**kw):
    server = BrowserPlugServer(host="localhost", port=8772, mcp_port=2846, start_mcp=False,
                               ws_token="t" * 32, **kw)
    return server


def test_an_embedded_server_gets_no_watchdog():
    """The dead-man timer exits the process it runs in; tests build servers in-process."""
    server = _standalone()
    assert server.fatal_on_mcp_failure is False
    with patch("server.runtime.watchdog.start") as start:
        server.lifecycle.start_watchdog()
    start.assert_not_called()
    assert server.lifecycle.watchdog is None


def test_the_standalone_server_starts_the_watchdog_with_the_lifecycles_own_hooks(monkeypatch):
    server = _standalone(fatal_on_mcp_failure=True)
    life = server.lifecycle
    life.ws_loop = object()
    seen = {}

    class FakeDog:
        hang_file = None

        def attach_loop(self, name, loop):
            seen.setdefault("attached", []).append((name, loop))

    def fake_start(cfg, **hooks):
        seen["hooks"] = hooks
        return FakeDog()

    monkeypatch.setattr("server.runtime.watchdog.start", fake_start)
    monkeypatch.setattr(lifecycle, "_open_hang_log", lambda: None)
    mcp_loop = object()
    life.attach_mcp_loop(mcp_loop)          # the lifespan can run before the watchdog exists
    life.start_watchdog()
    hooks = seen["hooks"]
    assert hooks["ws_loop"] is life.ws_loop
    assert hooks["begin_restart"] == life.begin_restart
    assert seen["attached"] == [("mcp", mcp_loop)], "a loop reported early is attached later"
    life.stopping, life.restarting = True, True
    assert hooks["leaving"]() is True and hooks["restarting"]() is True
    server.start_mcp = True
    assert hooks["mcp_port"]() == server.mcp_port
    server.use_stdio = True
    assert hooks["mcp_port"]() is None, "no HTTP side, nothing to probe"
    life.attach_mcp_loop("later")
    assert seen["attached"][-1] == ("mcp", "later")


def test_the_watchdog_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("BROWSERPLUG_WATCHDOG", "0")
    server = _standalone(fatal_on_mcp_failure=True)
    with patch("server.runtime.watchdog.start") as start:
        server.lifecycle.start_watchdog()
    start.assert_not_called()


def test_the_watchdogs_exit_is_terminate_process_and_nothing_else():
    """Second half of the last resort. It must not wait on the interpreter or the loader
    lock, so it is the system call and no Python-level cleanup (the lock was released by the
    hand-over; the OS drops it anyway)."""
    from server.runtime import watchdog
    calls = []
    with patch.object(watchdog, "terminate_process", lambda code=1: calls.append(code)), \
            patch.object(lifecycle, "release_instance_lock",
                         lambda: calls.append("release")):
        lifecycle.ServerLifecycle._watchdog_exit()
    assert calls == [1]


def test_the_watchdogs_hand_over_frees_the_instance_lock_before_it_spawns_the_successor():
    server = _standalone(fatal_on_mcp_failure=True)
    order = []
    with patch.object(lifecycle, "release_instance_lock", lambda: order.append("release")), \
            patch.object(server.lifecycle, "spawn_restart_process",
                         lambda *a, **k: order.append("spawn")):
        server.lifecycle._watchdog_hand_over()
    assert order == ["release", "spawn"]


def test_the_watchdogs_hand_over_does_not_spawn_a_second_replacement():
    """A graceful restart the watchdog started may already have spawned one."""
    server = _standalone(fatal_on_mcp_failure=True)
    order = []
    running = SimpleNamespace(poll=lambda: None)
    exited = SimpleNamespace(poll=lambda: 1)
    with patch.object(lifecycle, "release_instance_lock", lambda: order.append("release")), \
            patch.object(server.lifecycle, "spawn_restart_process",
                         lambda *a, **k: order.append("spawn")):
        server.lifecycle._replacement = (running, None, None)
        server.lifecycle._watchdog_hand_over()
        assert order == ["release"]
        server.lifecycle._replacement = (exited, None, None)
        server.lifecycle._watchdog_hand_over()
    assert order == ["release", "release", "spawn"]


def test_the_standalone_server_gives_the_watchdog_the_hand_over_and_the_exit(monkeypatch):
    server = _standalone(fatal_on_mcp_failure=True)
    life = server.lifecycle
    life.ws_loop = object()
    seen = {}
    monkeypatch.setattr("server.runtime.watchdog.start",
                        lambda cfg, **hooks: seen.update(hooks) or SimpleNamespace(hang_file=None))
    monkeypatch.setattr(lifecycle, "_open_hang_log", lambda: None)
    life.start_watchdog()
    assert seen["hand_over"] == life._watchdog_hand_over
    assert seen["exit_process"] == life._watchdog_exit


@pytest.mark.asyncio
async def test_the_lifespan_reports_the_serving_loop_to_the_lifecycle():
    """uvicorn does not expose its loop; the ASGI lifespan is the one place it can be had."""
    server = _standalone()
    got = []
    server.lifecycle.attach_mcp_loop = got.append

    async def inner(scope, receive, send):
        return None

    wrapped = server._wrap_client_tracking(inner)
    await wrapped({"type": "lifespan"}, None, None)
    assert got == [asyncio.get_running_loop()]
