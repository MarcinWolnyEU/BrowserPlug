"""GET /health reports `last_restart`, so an abandoned restart is visible afterwards.

POST /restart answers the abort only when the verdict arrives within 4 s, and the WebSocket
path acks before the verdict. The outcome used to live only in the log.
"""

import time

import pytest

from server.runtime import lifecycle
from server.runtime.graceful_restart import RestartHooks
from server.server import BrowserPlugServer


def _server(monkeypatch, spawn):
    server = BrowserPlugServer(host="localhost", port=8772, start_mcp=False)
    monkeypatch.setattr(server.lifecycle, "restart_hooks", lambda: RestartHooks(
        count_in_flight=lambda: 0, connections=lambda: [], spawn_replacement=spawn,
        close_registry=lambda reason: None, stop_http=lambda: None,
        exit_process=lambda: None))
    return server


def _no_spawn():
    raise OSError("no spawn")


def test_a_server_that_never_restarted_reports_none():
    server = BrowserPlugServer(host="localhost", port=8772, start_mcp=False)
    assert server.get_health_info()["last_restart"] is None


def test_an_aborted_restart_stays_visible_in_health(monkeypatch):
    server = _server(monkeypatch, _no_spawn)
    assert server._begin_restart("ws server.restart").result(5) == "no spawn"
    last = server.get_health_info()["last_restart"]
    assert last["trigger"] == "ws server.restart"
    assert last["outcome"] == "aborted"
    assert last["reason"] == "no spawn"
    assert isinstance(last["time"], str) and last["time"]


def test_a_restart_in_progress_reports_pending(monkeypatch):
    import threading
    release = threading.Event()
    server = BrowserPlugServer(host="localhost", port=8772, start_mcp=False)
    monkeypatch.setattr(server.lifecycle, "restart_hooks", lambda: RestartHooks(
        count_in_flight=lambda: 0 if release.is_set() else 1, connections=lambda: [],
        spawn_replacement=lambda: None, close_registry=lambda reason: None,
        stop_http=lambda: None, exit_process=lambda: None))
    monkeypatch.setenv("BROWSERPLUG_RESTART_DRAIN_SECONDS", "5")
    server._begin_restart("POST /restart")
    try:
        last = server.get_health_info()["last_restart"]
        assert last["outcome"] == "pending"
        assert last["trigger"] == "POST /restart"
        assert last["reason"] is None
    finally:
        release.set()
        deadline = time.monotonic() + 5
        while server.lifecycle.restarting and time.monotonic() < deadline:
            time.sleep(0.01)


def test_a_confirmed_restart_reports_ok(monkeypatch):
    server = _server(monkeypatch, lambda: None)
    assert server._begin_restart("POST /restart").result(5) is None
    assert server.get_health_info()["last_restart"]["outcome"] == "ok"


def test_a_refused_second_restart_does_not_overwrite_the_record(monkeypatch):
    server = _server(monkeypatch, _no_spawn)
    server._begin_restart("first").result(5)
    deadline = time.monotonic() + 3
    while server.lifecycle.restarting and time.monotonic() < deadline:
        time.sleep(0.01)
    server.lifecycle.restarting = True
    with pytest.raises(lifecycle.RestartInProgress):
        server._begin_restart("second")
    server.lifecycle.restarting = False
    assert server.get_health_info()["last_restart"]["trigger"] == "first"


# --- control.py status ---------------------------------------------------------------------

def _status(monkeypatch, capsys, body):
    from server import control
    monkeypatch.setattr(control, "find_port", lambda: 2830)
    monkeypatch.setattr(control, "call", lambda action, port, timeout=30: (200, body))
    assert control.main(["status"]) == 0
    return capsys.readouterr()


def test_status_prints_an_aborted_restart(monkeypatch, capsys):
    out = _status(monkeypatch, capsys, {"status": "ok", "last_restart": {
        "trigger": "POST /restart", "time": "2026-09-30 10:00:00",
        "outcome": "aborted", "reason": "the replacement exited with code 1"}})
    assert "last restart" in out.err
    assert "POST /restart" in out.err and "aborted" in out.err
    assert "the replacement exited with code 1" in out.err
    assert '"last_restart"' in out.out       # the JSON on stdout is unchanged


def test_status_stays_quiet_when_the_server_never_restarted(monkeypatch, capsys):
    out = _status(monkeypatch, capsys, {"status": "ok", "last_restart": None})
    assert "last restart" not in out.err
