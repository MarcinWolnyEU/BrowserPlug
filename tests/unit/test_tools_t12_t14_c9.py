"""Review items T12, T14 and C9 (server-launcher-review-2026-09-29).

T12: the "target may be in an iframe" hint must reach an agent whose page_act target matched
     nothing in the top document (FRAMES_LEAD_NO_MATCH was defined and tested but never used).
T14: `error_of` must survive `{"type": "error", "data": None}`.
C9:  replacing tabs/navigate/browsers must never leave a window in which the tool is absent,
     because the WebSocket thread runs the refresh while uvicorn serves list/call.
"""

import asyncio
import sys
import threading

import pytest

import test_imports  # noqa: F401
from facade_double import FacadeDouble
from server.tools.mcp_tools import BrowserPlugTools
from server.tools import mcp_tools as mcp_tools_mod
from server.tools.response_hints import FRAMES_LEAD_NO_MATCH


class MockServer(FacadeDouble):
    def __init__(self):
        super().__init__()
        self.sent = []
        self.responses = {}

    async def send_request_and_wait(self, request, timeout=30.0, browser=None):
        self.sent.append(request)
        action = request.get("action", "")
        if action in self.responses:
            return self.responses[action]
        return {"type": "response", "data": {"ok": True}}


def resp(data):
    return {"type": "response", "data": data}


@pytest.fixture
def server():
    return MockServer()


@pytest.fixture
def tools(server):
    return BrowserPlugTools(server)


async def _fn(tools, name):
    return (await tools.mcp.get_tool(name)).fn


FRAMES = resp({"frames": [{"frameId": 0, "url": "http://example.org/"},
                          {"frameId": 4, "url": "http://example.org/inner"}]})


def _act_result(error, **extra):
    step = {"ok": False, "step": 1, "stepAction": "click", "error": error}
    step.update(extra)
    return resp({"ok": False, "completedSteps": 0, "totalSteps": 1, "results": [step]})


# ---------------------------------------------------------------- T12

@pytest.mark.asyncio
@pytest.mark.parametrize("error", ["No element matched", "not found"])
async def test_page_act_no_match_lists_iframes(tools, server, error):
    server.responses["content.act"] = _act_result(error)
    server.responses["content.frames"] = FRAMES
    out = await (await _fn(tools, "page_act"))(steps=[{"action": "click", "target": "#x"}],
                                               tab_id=3)
    assert FRAMES_LEAD_NO_MATCH in out
    assert "frame_id=4 http://example.org/inner" in out
    frames_req = [r for r in server.sent if r["action"] == "content.frames"]
    assert frames_req and frames_req[0]["data"] == {"tabId": 3}


@pytest.mark.asyncio
async def test_page_act_no_match_without_subframes_adds_nothing(tools, server):
    server.responses["content.act"] = _act_result("No element matched")
    server.responses["content.frames"] = resp({"frames": [{"frameId": 0, "url": "u"}]})
    out = await (await _fn(tools, "page_act"))(steps=[{"action": "click", "target": "#x"}])
    assert "iframe" not in out


@pytest.mark.asyncio
async def test_page_act_inside_a_frame_does_not_probe_for_frames(tools, server):
    server.responses["content.act"] = _act_result("No element matched")
    server.responses["content.frames"] = FRAMES
    out = await (await _fn(tools, "page_act"))(steps=[{"action": "click", "target": "#x"}],
                                               frame_id=4)
    assert FRAMES_LEAD_NO_MATCH not in out
    assert not [r for r in server.sent if r["action"] == "content.frames"]


@pytest.mark.asyncio
async def test_page_act_other_failures_do_not_probe_for_frames(tools, server):
    server.responses["content.act"] = _act_result("Element is disabled")
    server.responses["content.frames"] = FRAMES
    out = await (await _fn(tools, "page_act"))(steps=[{"action": "click", "target": "#x"}])
    assert FRAMES_LEAD_NO_MATCH not in out


@pytest.mark.asyncio
async def test_page_act_frames_probe_failure_keeps_the_real_result(tools, server):
    server.responses["content.act"] = _act_result("No element matched")
    server.responses["content.frames"] = {"error": "boom"}
    out = await (await _fn(tools, "page_act"))(steps=[{"action": "click", "target": "#x"}])
    assert "Step 1 click: FAILED — No element matched" in out


def test_history_time_ms_is_gone():
    from server.tools import formatting
    assert not hasattr(formatting, "history_time_ms")


# ---------------------------------------------------------------- T14

@pytest.mark.parametrize("payload", [
    {"type": "error", "data": None},
    {"type": "error"},
    {"type": "error", "data": "plain string"},
    {"type": "error", "data": {}},
])
def test_error_of_survives_an_error_envelope_without_a_message(tools, payload):
    err = tools.calls.error_of(payload)
    assert isinstance(err, str) and err


def test_error_of_still_reads_the_message(tools):
    assert tools.calls.error_of({"type": "error", "data": {"message": "kaput"}}) == "kaput"


@pytest.mark.asyncio
async def test_page_act_reports_a_null_error_envelope_instead_of_raising(tools, server):
    server.responses["content.act"] = {"type": "error", "data": None}
    out = await (await _fn(tools, "page_act"))(steps=[{"action": "click", "target": "#x"}])
    assert out.startswith("Error executing steps:")


# ---------------------------------------------------------------- C9

@pytest.fixture(autouse=True)
def _fast_thread_switching():
    """Two threads that each finish inside one 5 ms GIL slice never interleave, which would
    hide the race. A tiny switch interval makes the window reachable in a short test."""
    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-5)
    yield
    sys.setswitchinterval(old)


def _hammer(tools, refresh, rounds=150):
    """Run `refresh` on one thread while another lists tools. Returns the sets of tool
    names that were ever missing a name the surface must always carry."""
    stop = threading.Event()
    errors = []

    def refresher():
        try:
            for i in range(rounds):
                refresh(i)
        except Exception as e:  # pragma: no cover - surfaced below
            errors.append(e)
        finally:
            stop.set()

    missing = []
    t = threading.Thread(target=refresher)
    t.start()
    loop = asyncio.new_event_loop()
    try:
        while not stop.is_set():
            names = {x.name for x in loop.run_until_complete(tools.mcp.list_tools())}
            for need in ("tabs", "navigate"):
                if need not in names:
                    missing.append(need)
    finally:
        loop.close()
        t.join()
    assert not errors, errors
    return missing


def test_proxy_visibility_refresh_never_hides_tabs_or_navigate(tools):
    def refresh(i):
        tools._proxy_tools_enabled = bool(i % 2)
        tools._register_browser_proxy_tools()

    assert _hammer(tools, refresh) == []


def test_browsers_tool_replacement_never_drops_it(tools, monkeypatch):
    calls = {"n": 0}

    def fake_desc(_calls):
        calls["n"] += 1
        return f"roster {calls['n']}"

    monkeypatch.setattr(mcp_tools_mod.browsers_tool, "tool_description", fake_desc)
    tools.refresh_browsers_tool()
    stop = threading.Event()
    gone = []

    def refresher():
        for _ in range(150):
            tools.refresh_browsers_tool()
        stop.set()

    t = threading.Thread(target=refresher)
    t.start()
    loop = asyncio.new_event_loop()
    try:
        while not stop.is_set():
            if "browsers" not in {x.name for x in loop.run_until_complete(tools.mcp.list_tools())}:
                gone.append(1)
    finally:
        loop.close()
        t.join()
    assert gone == []
