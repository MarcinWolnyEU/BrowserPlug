"""The Kilo, pi and Copilot Chat detectors sit behind one boundary (server/usage_panels/usage_panels.py)
and one switch, the popup's "Load Kilo session details".

The switch used to gate only the extension's own poll. GET /health and server_info called
get_connections_info() with the default and read other agents' logs and transcripts
regardless. The tests below hold the switch to every caller, and they do it with a fake
filesystem that records every touch, because the detectors swallow their own errors and an
assertion raised inside one would vanish.
"""
import asyncio
import builtins
import dataclasses
import logging
import os
from datetime import datetime

import psutil
import pytest

from server.server import BrowserPlugServer


AGENT_PIDS = (4242, 4343, 4444)


class Touched:
    """Everything a detector may use to look at another agent's state."""

    def __init__(self, monkeypatch):
        self.calls = []
        touched = self.calls

        real_process = psutil.Process

        def recorder(pid=None, *a, **k):
            # Only the agents' pids count; the server inspects its own and its
            # launchers' processes for unrelated reasons.
            if pid in AGENT_PIDS:
                touched.append(("psutil.Process", pid))
                raise psutil.NoSuchProcess(pid)
            return real_process(pid, *a, **k)

        monkeypatch.setattr(psutil, "Process", recorder)
        from server.usage_panels import handle_scan

        def scan_recorder(pid, workspace=False):
            # The Kilo and Copilot detectors ask handle_scan, not psutil; answering None
            # (unknown) keeps the test from starting a real scan.
            if pid in AGENT_PIDS:
                touched.append(("handle_scan.lookup", pid))
            return None

        monkeypatch.setattr(handle_scan, "lookup", scan_recorder)
        real_open = builtins.open

        def guarded_open(file, *a, **k):
            text = str(file).lower()
            if any(m in text for m in ("kilo", ".pi", "copilot")):
                touched.append(("open", str(file)))
                raise OSError("fake fs: touched")
            return real_open(file, *a, **k)

        monkeypatch.setattr(builtins, "open", guarded_open)
        for name in ("scandir", "listdir"):
            real = getattr(os, name)

            def make(real, name):
                def guarded(path=".", *a, **k):
                    if any(m in str(path).lower() for m in ("kilo", ".pi", "copilot")):
                        touched.append((name, str(path)))
                        raise OSError("fake fs: touched")
                    return real(path, *a, **k)
                return guarded
            monkeypatch.setattr(os, name, make(real, name))


@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.setenv("BROWSERPLUG_STATE_DIR", str(tmp_path))
    from server.usage_panels import usage_panels
    usage_panels._reset()
    for module in (usage_panels.kilocode_data, usage_panels.pi_data,
                   usage_panels.copilot_data):
        module._cache.clear()  # 10 s TTL caches would hide a fresh read
    srv = BrowserPlugServer(host="localhost", port=8772, start_mcp=False)
    now = datetime.now()
    for sid, pid, friendly in (("sid-kilo", 4242, "Kilo CLI"), ("sid-pi", 4343, "pi"),
                               ("sid-cp", 4444, "GitHub Copilot Chat")):
        srv.http_clients[sid] = {
            "pid": pid, "friendly": friendly, "cwd": r"C:\work", "exe_path": r"C:\x\agent.exe",
            "start_time": now, "last_activity": now, "history": [],
        }
    yield srv
    usage_panels._reset()


def _poll(server, **payload):
    """The extension's server.get_connections request, through the real handler."""
    sent = []

    async def send(message, browser=None):
        sent.append(message)

    server.ws_rpc._h = dataclasses.replace(server.ws_rpc._h, send=send)
    asyncio.run(server.ws_rpc._action_get_connections({"data": payload}, None, "m1"))
    return sent[0]["data"]


def test_health_reads_no_transcripts_after_the_popup_switched_the_details_off(
        server, monkeypatch):
    touched = Touched(monkeypatch)
    _poll(server, kilo_details=False)
    touched.calls.clear()

    server.get_health_info()
    server.get_connections_info()  # what server_info calls
    assert touched.calls == []


def test_health_still_reads_them_while_the_switch_is_on(server, monkeypatch):
    """The other half: the off-test above must not pass because nothing ever runs."""
    touched = Touched(monkeypatch)
    _poll(server, kilo_details=True)
    server.get_connections_info()
    assert any(kind == "psutil.Process" for kind, _ in touched.calls)
    looked_up = {pid for kind, pid in touched.calls if kind == "handle_scan.lookup"}
    assert looked_up == {4242, 4444}, "the Kilo and Copilot detectors ask handle_scan"


def test_the_switch_survives_a_restart(server, monkeypatch):
    from server.usage_panels import usage_panels
    _poll(server, kilo_details=False)
    usage_panels._reset()  # forget memory; config.json stays
    touched = Touched(monkeypatch)
    server.get_connections_info()
    assert touched.calls == []


def test_a_detector_failure_logs_a_warning_once(server, monkeypatch, caplog):
    from server.usage_panels import usage_panels

    def boom(pid):
        raise RuntimeError("private format changed")

    monkeypatch.setattr(usage_panels.kilocode_data, "detect_provider_model", boom)
    with caplog.at_level(logging.DEBUG):
        server.get_connections_info()
        server.get_connections_info()
    warnings = [r for r in caplog.records
                if r.levelno == logging.WARNING and "Kilo" in r.getMessage()]
    assert len(warnings) == 1
    assert "private format changed" in warnings[0].getMessage()


def test_detect_needs_no_bare_module_names_on_sys_path():
    """No string-keyed import: the facade names the detectors as ordinary imports."""
    import inspect
    from server.usage_panels import usage_panels
    assert "import_module" not in inspect.getsource(usage_panels)
    assert usage_panels.detect("Claude Code", 1) == {}
