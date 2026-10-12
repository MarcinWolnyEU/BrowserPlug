"""
Restart safety: a restart must not strand clients (L3), must speak as a restart while it
drains (C8), and must be single-flight (S2-guard).

  L3   A restart used to count as successful once Popen returned. A replacement with a
       syntax error dies during import within about a second, yet the old process still
       closed its sockets and os._exit(0)'d, so every client was left with nothing.
       The replacement now proves it reached the instance-lock wait (a marker file) before
       anything is torn down; a dead or silent child aborts the restart and the old server
       keeps serving.
  C8   During the drain a new call got the ordinary path and an in-flight one failed with
       the transient "disconnected, safe to retry" text (each handler's `finally` runs
       before close_registry), so idempotent reads then started an 8 s reconnect poll that
       outlived the shutdown. The server is now marked "restarting" from the start of the
       drain.
  S2   _begin_restart had no re-entry guard: N POSTs spawned N replacement pairs.

The L3 tests spawn REAL children (a script that fails to import, one that hangs, one that
writes the marker); everything else runs on fake hooks or fake sockets.
"""

import asyncio
import dataclasses
import json
import threading
import time

import pytest

from server.runtime import graceful_restart as gr
from server.runtime import lifecycle
from server.runtime.graceful_restart import PENDING_REASON, RestartHooks, perform_restart
from server.server import BrowserPlugServer

_EOF = object()


class FakeWS:
    def __init__(self, peer=("127.0.0.1", 5555)):
        self.remote_address = peer
        self.close_code = None
        self.sent = []
        self._q = asyncio.Queue()

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    async def close(self, code=1000, reason=""):
        if self.close_code is None:
            self.close_code = code
        self._q.put_nowait(_EOF)

    def feed(self, message):
        self._q.put_nowait(json.dumps(message))

    def drop(self, code=1006):
        self.close_code = code
        self._q.put_nowait(_EOF)

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self._q.get()
        if item is _EOF:
            raise StopAsyncIteration
        return item


async def _wait_until(predicate, what, timeout=3.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out after {timeout}s waiting until {what}")
        await asyncio.sleep(0.01)


# --- (L3) real children ------------------------------------------------------------------

@pytest.fixture
def restart_env(tmp_path, monkeypatch):
    """A server whose replacement is whatever script the test writes, with the log and the
    marker directory in tmp. Returns (server, hooks, log, set_script)."""
    monkeypatch.setenv("BROWSERPLUG_LOG_FILE", str(tmp_path / "s.log"))
    monkeypatch.setenv("BROWSERPLUG_STATE_DIR", str(tmp_path / "state"))
    server = BrowserPlugServer(host="localhost", port=8772, mcp_port=2851, start_mcp=False,
                               ws_token="t" * 32)
    log = []

    def set_script(source):
        script = tmp_path / "replacement.py"
        script.write_text(source, encoding="utf-8")
        monkeypatch.setattr(lifecycle, "SERVER_SCRIPT", str(script))

    hooks = dataclasses.replace(
        server.lifecycle.restart_hooks(),
        close_registry=lambda reason: log.append("close_registry"),
        stop_http=lambda: log.append("stop_http"),
        exit_process=lambda: log.append("exit"))

    yield server, hooks, log, set_script
    # Reap anything the test left running.
    proc = getattr(server.lifecycle, "_replacement", None)
    if proc is not None:
        child = proc[0] if isinstance(proc, tuple) else proc
        try:
            child.kill()
            # Reap it too: a killed child that is never waited for leaves Popen without a
            # returncode, and its finaliser reports it as "still running".
            child.wait(timeout=10)
        except Exception:
            pass


def _run(hooks, **kw):
    return asyncio.run(perform_restart(hooks, trigger="test", drain_timeout=0, ack_flush=0,
                                       **kw))


def test_a_replacement_that_dies_on_import_aborts_the_restart(restart_env):
    """L3: the replacement has a syntax error and exits at once. The old server must keep
    every socket, log the child's own traceback, and never exit."""
    server, hooks, log, set_script = restart_env
    set_script("def broken(:\n    pass\n")

    report = _run(hooks)

    assert report.aborted, "a replacement that died was counted as a successful restart"
    assert "exit" not in log and "close_registry" not in log and "stop_http" not in log
    assert "SyntaxError" in report.error, "the startup log's tail must reach the caller"
    assert server.lifecycle.stopping is False, "the death guard must be disarmed again"


def test_a_replacement_that_never_reaches_the_lock_wait_aborts_the_restart(
        restart_env, monkeypatch):
    """L3: a child that stays alive but never writes the marker (wedged in an import) is
    given a bounded time, then the restart is abandoned and the child is stopped."""
    server, hooks, log, set_script = restart_env
    monkeypatch.setattr(lifecycle, "REPLACEMENT_READY_SECONDS", 0.6, raising=False)
    set_script("import time\ntime.sleep(60)\n")

    started = time.monotonic()
    report = _run(hooks)

    assert time.monotonic() - started < 5
    assert report.aborted
    assert "exit" not in log


def test_a_replacement_that_reaches_the_lock_wait_lets_the_restart_proceed(restart_env):
    """The healthy path: the marker appears, teardown runs in order and the process exits."""
    server, hooks, log, set_script = restart_env
    set_script(
        "import os, time\n"
        f"open(os.environ[{lifecycle.RESTART_READY_ENV!r}], 'w').write('1')\n"
        "time.sleep(5)\n")

    report = _run(hooks)

    assert not report.aborted, report.error
    assert log == ["close_registry", "stop_http", "exit"]


# --- (L3) the pure policy: fake hooks ------------------------------------------------------

def _fake_hooks(log, **overrides):
    hooks = RestartHooks(
        count_in_flight=lambda: 0,
        connections=lambda: [],
        spawn_replacement=lambda: log.append("spawn"),
        close_registry=lambda reason: log.append("close_registry"),
        stop_http=lambda: log.append("stop_http"),
        exit_process=lambda: log.append("exit"),
        arm=lambda: log.append("arm"),
        disarm=lambda: log.append("disarm"))
    return dataclasses.replace(hooks, **overrides)


def test_policy_confirms_the_replacement_between_the_spawn_and_the_teardown():
    log = []
    hooks = _fake_hooks(log, confirm_replacement=lambda: log.append("confirm"))

    report = _run(hooks)

    assert not report.aborted
    assert log == ["arm", "spawn", "confirm", "close_registry", "stop_http", "exit"]


def test_policy_aborts_and_disarms_when_the_confirmation_fails():
    log = []
    verdicts = []
    hooks = _fake_hooks(log, confirm_replacement=lambda: "replacement exited with code 1",
                        announce=verdicts.append,
                        set_restarting=lambda on: log.append(f"restarting={on}"))

    report = _run(hooks)

    assert report.aborted and "code 1" in report.error
    assert log == ["restarting=True", "arm", "spawn", "disarm", "restarting=False"]
    assert verdicts == ["replacement exited with code 1"]


def test_policy_marks_restarting_from_the_start_of_the_drain():
    """C8 at the policy level: the flag is up before the drain polls once."""
    log = []
    seen = []

    def count():
        seen.append(list(log))
        return 0

    hooks = _fake_hooks(log, count_in_flight=count,
                        set_restarting=lambda on: log.append(f"restarting={on}"))
    _run(hooks)

    assert seen and seen[0] == ["restarting=True"]


# --- (C8) a real server, fake sockets --------------------------------------------------------

@pytest.mark.asyncio
async def test_during_the_drain_new_calls_get_the_restart_message_and_in_flight_ones_fail_fast():
    server = BrowserPlugServer(host="localhost", port=8772, start_mcp=False)
    ws = FakeWS()
    real_probe = server._probe_browser_identity
    server._probe_browser_identity = lambda *a, **k: {}
    task = asyncio.create_task(server.handle_extension_connection(ws))
    await asyncio.sleep(0.01)
    ws.feed({"type": "extension_hello", "data": {"instanceId": "ff-restart"}})
    await asyncio.sleep(0.02)
    browser = server.browsers["ff-restart"]

    # An idempotent read, in flight when the restart begins.
    inflight = asyncio.create_task(server.send_request_and_wait(
        {"id": "r1", "type": "request", "action": "tabs.screenshot", "data": {}},
        timeout=5.0, browser=browser))
    await _wait_until(lambda: ws.sent, "the in-flight request reached the browser")

    exited = []
    hooks = dataclasses.replace(
        server.lifecycle.restart_hooks(),
        spawn_replacement=lambda: None, stop_http=lambda: None,
        exit_process=lambda: exited.append(True))
    restart = asyncio.create_task(perform_restart(hooks, trigger="test", drain_timeout=3.0,
                                                  ack_flush=0))
    await asyncio.sleep(0.15)              # the drain is polling: r1 is still pending

    # (1) a NEW admission during the drain: the restart message, and nothing on the wire.
    sent_before = len(ws.sent)
    fresh = await asyncio.wait_for(server.send_request_and_wait(
        {"id": "r2", "type": "request", "action": "tabs.screenshot", "data": {}},
        timeout=5.0, browser=browser), 1.0)
    # Deliberate change: the refusal now carries a code and a retry verdict (nothing was sent).
    assert fresh == {"error": gr.RESTARTING_REFUSAL, "code": "SERVER_RESTARTING",
                     "retryable": True}
    assert len(ws.sent) == sent_before, "a refused call must not reach the browser"

    # (2) the socket drops mid-drain: the in-flight read fails AT ONCE with the restart
    # wording instead of polling for a reconnect that the shutdown outlives.
    ws.drop()
    await asyncio.wait_for(task, 2.0)
    result = await asyncio.wait_for(inflight, 1.0)
    # A read: the restart wording plus the read-only sentence; retryable is data.
    assert result["error"].startswith(PENDING_REASON)
    assert result["error"].endswith("safe to retry.")
    assert result["retryable"] is True

    await asyncio.wait_for(restart, 5.0)
    assert exited == [True]
    server._probe_browser_identity = real_probe


# --- (S2) single flight -------------------------------------------------------------------

def _server_with_fake_restart(monkeypatch, spawned, release):
    server = BrowserPlugServer(host="localhost", port=8772, start_mcp=False)
    exited = threading.Event()

    def make_hooks():
        return RestartHooks(
            count_in_flight=lambda: 0 if release.is_set() else 1,
            connections=lambda: [],
            spawn_replacement=lambda: spawned.append(1),
            close_registry=lambda reason: None,
            stop_http=lambda: None,
            exit_process=exited.set)

    monkeypatch.setattr(server.lifecycle, "restart_hooks", make_hooks)
    monkeypatch.setenv(gr.DRAIN_SECONDS_ENV, "5")
    return server, exited


def test_a_second_restart_while_one_is_running_spawns_nothing(monkeypatch):
    spawned, release = [], threading.Event()
    server, exited = _server_with_fake_restart(monkeypatch, spawned, release)

    server._begin_restart("first")
    second_error = None
    try:
        server._begin_restart("second")
    except Exception as e:
        second_error = e
    release.set()
    assert exited.wait(5), "the first restart never finished"
    time.sleep(0.5)   # a second sequence, if one was started, would have spawned by now

    assert spawned == [1], f"{len(spawned)} replacements were spawned for two requests"
    assert isinstance(second_error, lifecycle.RestartInProgress)


def test_an_aborted_restart_can_be_retried(monkeypatch):
    server = BrowserPlugServer(host="localhost", port=8772, start_mcp=False)
    attempts = []

    def make_hooks():
        attempts.append(1)
        return RestartHooks(
            count_in_flight=lambda: 0, connections=lambda: [],
            spawn_replacement=lambda: (_ for _ in ()).throw(OSError("no spawn")),
            close_registry=lambda reason: None, stop_http=lambda: None,
            exit_process=lambda: None)

    monkeypatch.setattr(server.lifecycle, "restart_hooks", make_hooks)

    first = server._begin_restart("first")
    assert first.result(5) == "no spawn"
    deadline = time.monotonic() + 3
    while server.lifecycle.restarting and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.lifecycle.restarting is False
    server._begin_restart("again").result(5)
    assert len(attempts) == 2


# --- the HTTP surface ---------------------------------------------------------------------

async def _post(server, path):
    from test_http_endpoints import call
    app = server._wrap_client_tracking(lambda *a: None)
    from secure_ext import signed_headers
    return await call(app, "POST", path, headers=signed_headers(server, "POST", path))


@pytest.mark.asyncio
async def test_post_restart_answers_409_while_a_restart_is_running(monkeypatch):
    server = BrowserPlugServer(host="localhost", port=8772, start_mcp=False)

    def refuse(trigger):
        raise lifecycle.RestartInProgress("a restart is already in progress")

    server._begin_restart = refuse
    status, headers, body = await _post(server, "/restart")
    assert status == 409
    assert "already" in body.decode().lower()


@pytest.mark.asyncio
async def test_post_restart_reports_a_replacement_that_failed_to_start():
    from concurrent.futures import Future
    server = BrowserPlugServer(host="localhost", port=8772, start_mcp=False)
    verdict = Future()
    verdict.set_result("replacement exited with code 1 before reaching the lock wait")
    server._begin_restart = lambda trigger: verdict
    status, headers, body = await _post(server, "/restart")
    assert status == 500
    assert "code 1" in body.decode()
