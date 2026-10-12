"""
Unit tests for the BrowserPlug consolidated tool surface (9 tools).

These exercise the server-side wiring with a mock WebSocket server (no browser):
that each tool sends the right protocol action + exact data-key mapping, threads
its timeout budget through to send_request_and_wait, and formats the extension's
response correctly. Complements the Firefox-gated end-to-end coverage in
tests/integration/test_agent_experience.py.
"""

from datetime import datetime

import pytest

import test_imports  # noqa: F401
from facade_double import FacadeDouble
from server.tools import tool_outcome
from server.tools.act import ActStep
from server.tools.formatting import CONSOLE_UNFLUSHED_NOTE
from server.tools.mcp_tools import BrowserPlugTools

# 1x1 PNG
PNG_B64 = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDw"
           "AChwGA60e6kgAAAABJRU5ErkJggg==")

# 9 since the 2026-08-03 removals: page_console and history were called ZERO times in
# 3,112 corpus calls across three clients and 31 days, while their descriptions were
# re-transmitted on all 780 tool listings. Console CAPTURE is unaffected — navigate and
# page_act still attach console errors to their own results, which is where agents read
# them; only the standalone tool is gone. `history` went with its browser permission.
EXPECTED_TOOLS = {
    "tabs", "navigate", "page_read", "page_snapshot", "page_act", "page_eval",
    "page_screenshot", "requests", "server_info",
}


class MockServer(FacadeDouble):
    def __init__(self):
        super().__init__()
        self.sent = []      # full request dicts, in send order
        self.timeouts = []  # the timeout passed for each request, in send order
        self.responses = {}

    def set_response(self, action, response):
        self.responses[action] = response

    async def send_request_and_wait(self, request, timeout=30.0, browser=None):
        self.sent.append(request)
        self.timeouts.append(timeout)
        action = request.get("action", "")
        if action in self.responses:
            return self.responses[action]
        return {"type": "response", "data": {"ok": True, "echo": action}}


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


def _last(server):
    return server.sent[-1]


# =============================================================================
# Tool surface
# =============================================================================

@pytest.mark.asyncio
async def test_tool_surface_is_exactly_the_9_tools(tools):
    names = {t.name for t in await tools.mcp.list_tools()}
    assert names == EXPECTED_TOOLS  # exact equality: every removed 1.x name must be gone


# =============================================================================
# page_read
# =============================================================================

@pytest.mark.asyncio
async def test_page_read_defaults_and_header(tools, server):
    server.set_response("content.read", resp({
        "ok": True, "format": "markdown", "url": "http://x/y", "title": "My Page",
        "totalChars": 5, "content": "hello", "offset": 0,
        "pageState": {"painted": True}}))
    fn = await _fn(tools, "page_read")
    out = await fn()
    req = _last(server)
    assert req["action"] == "content.read"
    assert req["data"] == {"format": "markdown", "maxChars": 20000, "offset": 0}
    assert "tabId" not in req["data"]
    assert server.timeouts[-1] == 60.0
    assert out.startswith("My Page — http://x/y")
    assert "[markdown] chars 0-5 of 5" in out
    assert "continue with offset=" not in out
    assert "hello" in out


@pytest.mark.asyncio
async def test_page_read_argument_mapping(tools, server):
    server.set_response("content.read", resp({
        "ok": True, "format": "html", "url": "u", "title": "t",
        "totalChars": 1, "content": "x", "offset": 0}))
    fn = await _fn(tools, "page_read")
    await fn(tab_id=7, format="html", scope="#main", max_chars=500, offset=100, frame_id=3)
    d = _last(server)["data"]
    assert d == {"format": "html", "maxChars": 500, "offset": 100,
                 "tabId": 7, "scope": "#main", "frameId": 3}


@pytest.mark.asyncio
async def test_page_read_pagination_hint(tools, server):
    content = "a" * 20000
    server.set_response("content.read", resp({
        "ok": True, "format": "text", "url": "u", "title": "t",
        "totalChars": 50000, "content": content, "offset": 0}))
    fn = await _fn(tools, "page_read")
    out = await fn(format="text")
    assert "chars 0-20000 of 50000" in out
    assert "continue with offset=20000" in out


@pytest.mark.asyncio
async def test_page_read_warns_when_unpainted(tools, server):
    server.set_response("content.read", resp({
        "ok": True, "format": "text", "url": "u", "title": "t",
        "totalChars": 1, "content": "x", "offset": 0,
        "pageState": {"painted": False}}))
    fn = await _fn(tools, "page_read")
    out = await fn(tab_id=12)
    assert "WARNING:" in out
    assert "Page is not laid out" in out
    # Work in the background tab first; activation is the last resort, with its cost.
    assert "Stay in this background tab" in out
    assert out.index("Stay in this background tab") < out.index('tabs(action="activate"')
    assert 'tabs(action="activate", tab_id=12)' in out
    assert "front of the user's window" in out
    assert "Remedy" not in out


@pytest.mark.asyncio
async def test_unpainted_warning_ignores_an_older_extensions_remedy_text(tools, server):
    # Builds before the rewording sent their own warning ending "Remedy: tabs
    # action=activate"; the server words it from the flags instead.
    server.set_response("content.snapshot", resp({
        "ok": True, "pageState": {"url": "u", "title": "t", "painted": False,
                                  "warning": 'Page is not laid out/painted. Remedy: tabs '
                                             'action="activate" (focuses the window), then retry.'},
        "count": 1, "elements": [{"ref": "e1", "role": "button", "name": "Go"}]}))
    fn = await _fn(tools, "page_snapshot")
    out = await fn(tab_id=4)
    assert "Remedy" not in out
    assert "Stay in this background tab" in out
    assert 'tabs(action="activate", tab_id=4)' in out


@pytest.mark.asyncio
async def test_page_read_surfaces_fallback_note(tools, server):
    server.set_response("content.read", resp({
        "ok": True, "format": "markdown", "url": "u", "title": "t",
        "totalChars": 4, "content": "text", "offset": 0,
        "fallback": "Defuddle extraction failed; returning plain text"}))
    fn = await _fn(tools, "page_read")
    out = await fn()
    assert "NOTE: Defuddle extraction failed; returning plain text" in out


# =============================================================================
# page_snapshot
# =============================================================================

@pytest.mark.asyncio
async def test_page_snapshot_default_data_and_frames_followup(tools, server):
    server.set_response("content.snapshot", resp({
        "ok": True, "pageState": {"url": "u", "title": "t", "painted": True},
        "count": 0, "elements": []}))
    fn = await _fn(tools, "page_snapshot")
    out = await fn()
    assert len(server.sent) == 2
    assert server.sent[0]["action"] == "content.snapshot"
    assert server.sent[0]["data"] == {"root": None, "max": 150, "includeHidden": False,
                                      "dedupe": True, "mode": "interactive"}
    # the frames follow-up (absorbed page_frames tool)
    assert server.sent[1]["action"] == "content.frames"
    assert server.sent[1]["data"] == {}
    assert server.timeouts[1] == 10.0
    # default frames response has no subframes -> no Frames section
    assert "Frames" not in out


@pytest.mark.asyncio
async def test_page_snapshot_argument_mapping(tools, server):
    server.set_response("content.snapshot", resp({
        "ok": True, "pageState": {}, "count": 0, "elements": []}))
    fn = await _fn(tools, "page_snapshot")
    await fn(tab_id=7, mode="forms", root="#main", max_elements=50,
             include_hidden=True, frame_id=3)
    d = server.sent[0]["data"]
    assert d == {"root": "#main", "max": 50, "includeHidden": True,
                 "dedupe": True, "mode": "forms", "tabId": 7, "frameId": 3}
    # frames follow-up carries the tabId too
    assert server.sent[1]["action"] == "content.frames"
    assert server.sent[1]["data"] == {"tabId": 7}


@pytest.mark.asyncio
async def test_page_snapshot_element_line_formatting(tools, server):
    server.set_response("content.snapshot", resp({
        "ok": True, "pageState": {"url": "http://x/", "title": "T", "painted": True},
        "count": 3, "dedupedDuplicates": 30,
        "elements": [
            {"ref": "e1", "role": "button", "name": "Save", "enabled": False},
            {"ref": "e2", "role": "textbox", "name": "Email", "value": "a@b.co",
             "selector": "#email"},
            {"ref": "e3", "role": "button", "name": "Day", "dupCount": 31},
        ]}))
    fn = await _fn(tools, "page_snapshot")
    out = await fn()
    assert "Page: T — http://x/" in out
    assert "3 elements" in out and "30 duplicates collapsed" in out
    assert '- e1 button "Save" [disabled]' in out
    assert '- e2 textbox "Email" value="a@b.co" sel=#email' in out
    assert '- e3 button "Day" (x31 identical)' in out


@pytest.mark.asyncio
async def test_page_snapshot_lists_subframes_with_frame_ids(tools, server):
    server.set_response("content.snapshot", resp({
        "ok": True, "pageState": {}, "count": 0, "elements": []}))
    server.set_response("content.frames", resp({
        "frames": [{"frameId": 0, "url": "http://top/"},
                   {"frameId": 3, "url": "http://inner/"}]}))
    fn = await _fn(tools, "page_snapshot")
    out = await fn()
    assert "Frames (pass frame_id to page_* tools to reach inside):" in out
    assert "frame_id=3 http://inner/" in out
    assert "frame_id=0" not in out  # top frame is not a subframe


@pytest.mark.asyncio
async def test_page_snapshot_forms_mode_formatting(tools, server):
    server.set_response("content.snapshot", resp({
        "ok": True, "mode": "forms", "pageState": {"url": "u", "title": "t"},
        "radioGroups": [{"name": "size", "selected": ["m"], "options": [
            {"ref": "e1", "value": "s", "label": "Small", "checked": False, "enabled": True},
            {"ref": "e2", "value": "m", "label": "Medium", "checked": True, "enabled": True},
        ]}],
        "inputs": [{"ref": "e3", "label": "Email", "type": "email", "value": "",
                    "required": True, "enabled": True}],
        "buttons": [{"ref": "e9", "label": "Submit", "enabled": False}]}))
    fn = await _fn(tools, "page_snapshot")
    out = await fn(mode="forms")
    assert server.sent[0]["data"]["mode"] == "forms"
    assert 'Radio group "size" (selected: m):' in out
    assert '( ) e1 value=s "Small"' in out
    assert '(x) e2 value=m "Medium"' in out
    assert 'e3 email "Email" value="" [required]' in out
    assert 'e9 "Submit" [disabled]' in out


# =============================================================================
# page_act
# =============================================================================

@pytest.mark.asyncio
async def test_page_act_serializes_steps_snake_case_exclude_none(tools, server):
    server.set_response("content.act", resp({
        "ok": True, "completedSteps": 2, "totalSteps": 2, "results": []}))
    fn = await _fn(tools, "page_act")
    await fn(steps=[
        ActStep(action="fill", target="#email", value="a@b.co", submit=True),
        ActStep(action="click", target="Save", enabled_only=True),
    ], tab_id=4, frame_id=2)
    req = _last(server)
    assert req["action"] == "content.act"
    assert req["data"]["tabId"] == 4 and req["data"]["frameId"] == 2
    # exclude_none + snake_case on the wire — no nulls, no camelCase
    assert req["data"]["steps"] == [
        {"action": "fill", "target": "#email", "value": "a@b.co", "submit": True},
        {"action": "click", "target": "Save", "enabled_only": True},
    ]


@pytest.mark.asyncio
async def test_page_act_normalizes_raw_dict_steps_applying_text_alias(tools, server):
    # Direct callers (test harness, scripts) hand raw dicts, skipping the registry's
    # schema coercion. The tool must normalize them through ActStep anyway:
    # otherwise `text` (meant as the value alias) reaches the extension, where
    # `text` is a TARGETING field and hijacks resolution (fable pre-existing #1).
    server.set_response("content.act", resp({
        "ok": True, "completedSteps": 1, "totalSteps": 1, "results": []}))
    fn = await _fn(tools, "page_act")
    await fn(steps=[{"action": "fill", "target": "#reactive", "text": "via-text-alias"}])
    step = _last(server)["data"]["steps"][0]
    assert step == {"action": "fill", "target": "#reactive", "value": "via-text-alias"}
    assert "text" not in step  # never leaks to the wire as a targeting field


@pytest.mark.asyncio
async def test_page_act_formats_ok_steps(tools, server):
    server.set_response("content.act", resp({
        "ok": True, "completedSteps": 2, "totalSteps": 2, "results": [
            {"ok": True, "step": 1, "stepAction": "fill", "confirmedValue": "a@b.co"},
            {"ok": True, "step": 2, "stepAction": "click", "urlChanged": True,
             "urlAfter": "http://x/done"},
        ]}))
    fn = await _fn(tools, "page_act")
    out = await fn(steps=[{"action": "fill", "target": "#e", "value": "a@b.co"},
                          {"action": "click", "target": "Go"}])
    assert out.startswith("OK: 2/2 steps completed")
    assert 'Step 1 fill: ok — value now "a@b.co"' in out
    assert "Step 2 click: ok — urlChanged; now at http://x/done" in out


@pytest.mark.asyncio
async def test_page_act_formats_failed_step_with_error_detail(tools, server):
    server.set_response("content.act", resp({
        "ok": False, "completedSteps": 0, "totalSteps": 1, "results": [
            {"ok": False, "step": 1, "stepAction": "click",
             "error": "no element matched", "target": "Save", "candidates": 0},
        ]}))
    fn = await _fn(tools, "page_act")
    out = await fn(steps=[{"action": "click", "target": "Save"}])
    assert out.startswith("FAILED: 0/1 steps completed")
    assert "Step 1 click: FAILED — no element matched" in out
    assert '"target":"Save"' in out  # detail line carries the failing target


@pytest.mark.asyncio
async def test_page_act_reports_failed_network_requests(tools, server):
    server.set_response("content.act", resp({
        "ok": True, "completedSteps": 1, "totalSteps": 1, "results": [
            {"ok": True, "step": 1, "stepAction": "click",
             "network": {"failed": [{"method": "POST", "url": "http://x/api/login",
                                     "status": 401}], "pending": 2},
             "consoleErrors": {"errors": 3, "first": ["boom"]}},
        ]}))
    fn = await _fn(tools, "page_act")
    out = await fn(steps=[{"action": "click", "target": "Log in"}])
    assert "FAILED requests: POST http://x/api/login -> 401" in out
    assert "2 request(s) still in flight" in out
    assert "3 console error(s): boom" in out
    # click with no domChanged/urlChanged is called out honestly
    assert "NO visible effect (domChanged=false)" in out


@pytest.mark.asyncio
async def test_page_act_reports_watched_targets(tools, server):
    server.set_response("content.act", resp({
        "ok": True, "completedSteps": 1, "totalSteps": 1, "results": [
            {"ok": True, "step": 1, "stepAction": "fill", "watched": [
                {"watch": "#submit", "found": True, "visible": True, "enabled": False},
                {"watch": "#error", "found": False},
            ]},
        ]}))
    fn = await _fn(tools, "page_act")
    out = await fn(steps=[{"action": "fill", "target": "#e", "value": "x",
                           "watch": ["#submit", "#error"]}])
    assert "watch #submit:" in out and "visible, disabled" in out
    assert "watch #error: NOT FOUND" in out


@pytest.mark.asyncio
async def test_page_act_wait_step_reports_match_and_elapsed(tools, server):
    server.set_response("content.act", resp({
        "ok": True, "completedSteps": 1, "totalSteps": 1, "results": [
            {"ok": True, "step": 1, "stepAction": "wait", "matched": "selector",
             "elapsedMs": 230},
        ]}))
    fn = await _fn(tools, "page_act")
    out = await fn(steps=[{"action": "wait", "target": "[role=dialog]"}])
    assert "Step 1 wait: ok — matched selector in 230ms" in out


@pytest.mark.asyncio
async def test_page_act_wait_step_timeout_uses_timed_out_wording(tools, server):
    # a FAILED wait step with timedOut=True renders the dedicated timeout phrasing
    # (mcp_tools.py: "timed out after <elapsed> (requested <requested>)"), with the
    # predicate error appended when present.
    server.set_response("content.act", resp({
        "ok": False, "completedSteps": 0, "totalSteps": 1, "results": [
            {"ok": False, "step": 1, "stepAction": "wait", "timedOut": True,
             "elapsedMs": 10000, "requestedTimeoutMs": 10000,
             "predicateError": "x is not defined", "waitedFor": "#dialog"},
        ]}))
    fn = await _fn(tools, "page_act")
    out = await fn(steps=[{"action": "wait", "predicate": "x", "timeout_ms": 10000}])
    assert out.startswith("FAILED: 0/1 steps completed")
    assert "Step 1 wait: FAILED — timed out after 10.0s (requested 10.0s)" in out
    assert "predicate error: x is not defined" in out
    assert '"waitedFor":"#dialog"' in out  # detail line carries the wait target


@pytest.mark.asyncio
async def test_page_act_wait_timeout_reports_last_seen_substate(tools, server):
    # R2 (fable-feedback): a timed-out wait says WHY — present-but-hidden vs
    # never-matched — with the selector match count, so recovery is a decision.
    server.set_response("content.act", resp({
        "ok": False, "completedSteps": 0, "totalSteps": 1, "results": [
            {"ok": False, "step": 1, "stepAction": "wait", "timedOut": True,
             "elapsedMs": 6000, "requestedTimeoutMs": 6000,
             "matchCount": 1, "lastSeen": "present-but-hidden",
             "waitedFor": {"selector": ".modal button", "gone": False}},
        ]}))
    fn = await _fn(tools, "page_act")
    out = await fn(steps=[{"action": "wait", "target": ".modal button",
                           "require_visible": True, "timeout_ms": 6000}])
    assert "timed out after 6.0s (requested 6.0s)" in out
    assert "selector present-but-hidden (1 match at last check)" in out

    server.set_response("content.act", resp({
        "ok": False, "completedSteps": 0, "totalSteps": 1, "results": [
            {"ok": False, "step": 1, "stepAction": "wait", "timedOut": True,
             "elapsedMs": 6000, "requestedTimeoutMs": 6000,
             "matchCount": 0, "lastSeen": "never-matched",
             "waitedFor": {"selector": "#nope", "gone": False}},
        ]}))
    out = await fn(steps=[{"action": "wait", "target": "#nope", "timeout_ms": 6000}])
    assert "selector never-matched (0 matches at last check)" in out


@pytest.mark.asyncio
async def test_page_act_wait_timeout_reports_missing_within_scope(tools, server):
    # GATE 1.5/1.6: a SCOPED wait whose `within` container never rendered must not
    # be reported as "the target never matched" — the condition was never evaluated.
    # The extension composes lastSeen="scope-never-appeared" + scopeMissing=<within>;
    # both must reach the agent.
    server.set_response("content.act", resp({
        "ok": False, "completedSteps": 0, "totalSteps": 1, "results": [
            {"ok": False, "step": 1, "stepAction": "wait", "timedOut": True,
             "elapsedMs": 5000, "requestedTimeoutMs": 5000,
             "lastSeen": "scope-never-appeared", "scopeMissing": "#panel-2",
             "waitedFor": {"selector": ".spinner", "gone": True, "within": "#panel-2"}},
        ]}))
    fn = await _fn(tools, "page_act")
    out = await fn(steps=[{"action": "wait", "target": ".spinner", "gone": True,
                           "within": "#panel-2", "timeout_ms": 5000}])
    assert "timed out after 5.0s (requested 5.0s)" in out
    assert "the within-scope #panel-2 never appeared" in out
    assert "the condition was never evaluated inside it" in out
    # must NOT be mis-reported as a selector sub-state
    assert "selector never-matched" not in out and "selector still-present" not in out
    # the raw field survives into the machine-readable detail line
    assert '"scopeMissing":"#panel-2"' in out


@pytest.mark.asyncio
async def test_page_act_wait_success_scope_gone_is_spelled_out(tools, server):
    # The other half of the contract: a scoped wait whose container itself
    # disappeared SUCCEEDS with matched="scope-gone".
    server.set_response("content.act", resp({
        "ok": True, "completedSteps": 1, "totalSteps": 1, "results": [
            {"ok": True, "step": 1, "stepAction": "wait", "matched": "scope-gone",
             "elapsedMs": 400},
        ]}))
    fn = await _fn(tools, "page_act")
    out = await fn(steps=[{"action": "wait", "target": ".spinner", "gone": True,
                           "within": "#modal"}])
    assert "Step 1 wait: ok — matched scope-gone" in out
    assert "the within container is no longer in the DOM" in out
    assert "in 400ms" in out


@pytest.mark.asyncio
async def test_page_act_wait_timeout_without_diagnostics_is_unchanged(tools, server):
    # An older extension build (no matchCount/lastSeen) must format exactly as before.
    server.set_response("content.act", resp({
        "ok": False, "completedSteps": 0, "totalSteps": 1, "results": [
            {"ok": False, "step": 1, "stepAction": "wait", "timedOut": True,
             "elapsedMs": 6000, "requestedTimeoutMs": 6000,
             "waitedFor": {"selector": "#x", "gone": False}},
        ]}))
    fn = await _fn(tools, "page_act")
    out = await fn(steps=[{"action": "wait", "target": "#x", "timeout_ms": 6000}])
    assert "Step 1 wait: FAILED — timed out after 6.0s (requested 6.0s)\n" in out
    assert "selector " not in out.split("\n")[1]  # no lastSeen fragment appended


@pytest.mark.asyncio
async def test_page_act_timeout_budget_covers_waits(tools, server):
    server.set_response("content.act", resp({
        "ok": True, "completedSteps": 2, "totalSteps": 2, "results": []}))
    fn = await _fn(tools, "page_act")
    await fn(steps=[ActStep(action="wait", target="#x", timeout_ms=30000),
                    ActStep(action="click", target="Go")])
    # 20s base + 30000ms wait + 3600ms click reserve. The click figure rose from 2100 when
    # the extension started measuring the page before AND after the step and waiting
    # (bounded) for it to quiesce. Under-reserving here is how the act-timeout regression
    # comes back — the server gives up before the extension's structured report is sent.
    assert server.timeouts[-1] == pytest.approx(20.0 + 33.6)
    assert server.timeouts[-1] > 20


# =============================================================================
# navigate
# =============================================================================

@pytest.mark.asyncio
async def test_navigate_argument_mapping_and_timeout(tools, server):
    server.set_response("navigation.navigate", resp({
        "ok": True, "op": "goto", "tabId": 3, "url": "http://x/", "settled": True,
        "waitedMs": 100}))
    fn = await _fn(tools, "navigate")
    await fn(url="http://x/", tab_id=3, wait="load", wait_for="#app",
             timeout_ms=5000, bypass_cache=True)
    req = _last(server)
    assert req["action"] == "navigation.navigate"
    assert req["data"] == {"op": "goto", "wait": "load", "timeoutMs": 5000,
                           "bypassCache": True, "url": "http://x/", "tabId": 3,
                           "waitFor": "#app"}
    assert server.timeouts[-1] == pytest.approx(5000 / 1000 + 15)


@pytest.mark.asyncio
async def test_navigate_default_timeout_is_budget_plus_15(tools, server):
    fn = await _fn(tools, "navigate")
    await fn(url="http://x/")
    assert _last(server)["data"]["timeoutMs"] == 20000
    assert server.timeouts[-1] == pytest.approx(35.0)


@pytest.mark.asyncio
async def test_navigate_settled_output(tools, server):
    server.set_response("navigation.navigate", resp({
        "ok": True, "op": "goto", "tabId": 3, "url": "http://final/",
        "title": "Final", "settled": True, "waitedMs": 1500}))
    fn = await _fn(tools, "navigate")
    # Requested == landed, so this asserts the happy path AND that a clean navigation adds
    # no landing warning. (It used to request http://start/ and land on http://final/,
    # which is a genuine cross-site landing the landing check now — correctly — flags.)
    out = await fn(url="http://final/", tab_id=3)
    assert 'Navigated tab 3 -> http://final/ — "Final" (settled in 1.5s)' == out


@pytest.mark.asyncio
async def test_navigate_defaults_to_the_settled_wait(tools, server):
    """Omitting wait= asks the extension for the settled wait."""
    server.set_response("navigation.navigate", resp({
        "ok": True, "op": "goto", "tabId": 3, "url": "http://final/", "title": "Final",
        "settled": True, "waitMode": "settled", "waitedMs": 400}))
    await (await _fn(tools, "navigate"))(url="http://final/", tab_id=3)
    assert _last(server)["data"]["wait"] == "settled"


@pytest.mark.asyncio
async def test_navigate_back_sends_op_without_url(tools, server):
    server.set_response("navigation.navigate", resp({
        "ok": True, "op": "back", "tabId": 2, "url": "http://prev/", "settled": True,
        "waitedMs": 80}))
    fn = await _fn(tools, "navigate")
    out = await fn(action="back", tab_id=2)
    d = _last(server)["data"]
    assert d["op"] == "back" and "url" not in d
    assert out.startswith("Went back tab 2 -> http://prev/")


@pytest.mark.asyncio
async def test_navigate_timed_out_output(tools, server):
    server.set_response("navigation.navigate", resp({
        "ok": True, "op": "goto", "tabId": 1, "url": "http://slow/",
        "timedOut": True, "waitedMs": 20000, "status": "loading"}))
    fn = await _fn(tools, "navigate")
    out = await fn(url="http://slow/")
    assert "TIMED OUT after 20.0s waiting to settle (status=loading)" in out
    assert "re-check with page_read or raise timeout_ms" in out


@pytest.mark.asyncio
async def test_navigate_appends_network_and_console_digest(tools, server):
    server.set_response("navigation.navigate", resp({
        "ok": True, "op": "goto", "tabId": 1, "url": "http://x/", "settled": True,
        "waitedMs": 900,
        "network": {"failed": [{"method": "GET", "url": "http://x/api/me",
                                "status": 500}]},
        "consoleErrors": {"errors": 1, "first": ["TypeError: boom"]}}))
    fn = await _fn(tools, "navigate")
    out = await fn(url="http://x/")
    assert "FAILED requests: GET http://x/api/me -> 500" in out
    assert "1 console error(s): TypeError: boom" in out


@pytest.mark.asyncio
async def test_navigate_says_when_the_page_did_not_hand_over_its_console(tools, server):
    # The extension reads its console buffer only after the page has handed over its
    # pending entries. A page too busy to answer may have errors this result lacks, and an
    # agent reading "no console errors" from it would be misled.
    server.set_response("navigation.navigate", resp({
        "ok": True, "op": "goto", "tabId": 1, "url": "http://x/", "settled": True,
        "waitedMs": 760, "consoleFlushTimedOut": True}))
    tool_outcome.arm()
    fn = await _fn(tools, "navigate")
    out = await fn(url="http://x/")
    assert CONSOLE_UNFLUSHED_NOTE in out
    outcome = tool_outcome.read() or {}
    assert not outcome.get("failed")
    assert [d["kind"] for d in outcome.get("degraded", [])] == ["console_unflushed"]


@pytest.mark.asyncio
async def test_page_act_says_when_the_page_did_not_hand_over_its_console(tools, server):
    server.set_response("content.act", resp({
        "ok": True, "completedSteps": 1, "totalSteps": 1, "results": [
            {"ok": True, "step": 1, "stepAction": "fill", "consoleFlushTimedOut": True},
        ]}))
    tool_outcome.arm()
    fn = await _fn(tools, "page_act")
    out = await fn(steps=[{"action": "fill", "target": "#q", "value": "x"}])
    assert CONSOLE_UNFLUSHED_NOTE in out
    assert any(d.get("kind") == "console_unflushed"
               for d in (tool_outcome.read() or {}).get("degraded", []))


@pytest.mark.asyncio
async def test_navigate_surfaces_failure_diagnostics(tools, server):
    # net error + dialed address + about:blank sentinel.
    server.set_response("navigation.navigate", resp({
        "ok": True, "op": "goto", "tabId": 1, "url": "about:blank",
        "timedOut": True, "waitedMs": 8000, "status": "loading",
        "aboutBlank": True, "pendingUrl": "http://127.0.0.1:9/",
        "netError": "connection refused — nothing is listening on that address/port [NS_ERROR_CONNECTION_REFUSED]",
        "dialedAddress": "127.0.0.1:9"}))
    fn = await _fn(tools, "navigate")
    out = await fn(url="http://127.0.0.1:9/")
    assert "document is still about:blank — nothing rendered (attempted http://127.0.0.1:9/)" in out
    assert "net error: connection refused" in out
    assert "dialed address: 127.0.0.1:9" in out


@pytest.mark.asyncio
async def test_navigate_reports_redirect_chain_and_superseded(tools, server):
    # redirect chain + superseded label.
    server.set_response("navigation.navigate", resp({
        "ok": True, "op": "goto", "tabId": 1, "url": "http://final/",
        "settled": True, "waitedMs": 1000,
        "redirects": [{"to": "http://b/", "status": 302}, {"to": "http://final/", "status": 301}],
        "superseded": True}))
    fn = await _fn(tools, "navigate")
    out = await fn(url="http://a/")
    assert "redirect chain: 302 http://b/ -> 301 http://final/" in out
    assert "superseded" in out.lower()


@pytest.mark.asyncio
async def test_navigate_reports_service_worker_controlling(tools, server):
    # A SW that actively controls the loaded document (e.g. a repeat visit).
    server.set_response("navigation.navigate", resp({
        "ok": True, "op": "goto", "tabId": 1, "url": "https://ex/", "settled": True,
        "waitedMs": 700,
        "serviceWorkers": {
            "controlled": True,
            "controller": {"scriptURL": "https://ex/sw.js", "state": "activated"},
            "registrations": [{
                "scope": "https://ex/", "updateViaCache": "imports",
                "active": {"scriptURL": "https://ex/sw.js", "state": "activated"},
                "waiting": None, "installing": None}]}}))
    fn = await _fn(tools, "navigate")
    out = await fn(url="https://ex/")
    assert "document controlled by https://ex/sw.js (activated)" in out
    assert "scope https://ex/ — active: https://ex/sw.js (activated)" in out


@pytest.mark.asyncio
async def test_navigate_reports_service_worker_registered_not_controlling(tools, server):
    # First visit: SW is registered/activated but does not yet control the page.
    server.set_response("navigation.navigate", resp({
        "ok": True, "op": "goto", "tabId": 1, "url": "https://ex/", "settled": True,
        "waitedMs": 700,
        "serviceWorkers": {
            "controlled": False, "controller": None,
            "registrations": [{
                "scope": "https://ex/app/", "updateViaCache": "imports",
                "active": {"scriptURL": "https://ex/app/sw.js", "state": "activated"},
                "waiting": None, "installing": None}]}}))
    fn = await _fn(tools, "navigate")
    out = await fn(url="https://ex/")
    assert "registered but NOT controlling this document" in out
    assert "scope https://ex/app/ — active: https://ex/app/sw.js (activated)" in out


@pytest.mark.asyncio
async def test_navigate_quiet_when_no_service_worker(tools, server):
    # No SW field (the common case) → no service-worker line in the output.
    server.set_response("navigation.navigate", resp({
        "ok": True, "op": "goto", "tabId": 1, "url": "https://ex/", "settled": True,
        "waitedMs": 300}))
    fn = await _fn(tools, "navigate")
    out = await fn(url="https://ex/")
    assert "service worker" not in out.lower()


# =============================================================================
# ActStep — forgiving `text` alias
# =============================================================================

def test_actstep_accepts_text_as_alias_for_value():
    s = ActStep(action="fill", target="#x", text="hi")
    assert s.value == "hi"


def test_actstep_value_takes_precedence_over_text():
    s = ActStep(action="fill", target="#x", value="real", text="ignored")
    assert s.value == "real"


def test_actstep_plain_value_still_works():
    assert ActStep(action="fill", target="#x", value="v").value == "v"


# =============================================================================
# page_eval
# =============================================================================

@pytest.mark.asyncio
async def test_page_eval_defaults_to_isolated_world(tools, server):
    server.set_response("content.execute_script", resp({
        "result": {"a": 1}, "url": "http://x/"}))
    fn = await _fn(tools, "page_eval")
    out = await fn(code="1+1")
    req = _last(server)
    assert req["action"] == "content.execute_script"
    assert req["data"] == {"script": "1+1", "world": "isolated"}
    assert out.startswith("Script result (http://x/):")
    assert '"a": 1' in out


@pytest.mark.asyncio
async def test_page_eval_main_world_and_frame_id_reach_the_wire(tools, server):
    """world="main" and frame_id are the two targeting knobs that survived the 2026-08-03
    removals (all_frames went — it was passed 0 times in 209 calls). This replaces the
    main-world half of the test that also covered all_frames."""
    server.set_response("content.execute_script", resp({"result": 7, "url": "http://x/"}))
    fn = await _fn(tools, "page_eval")
    await fn(code="window.x", world="main", tab_id=4, frame_id=2)
    assert _last(server)["data"] == {"script": "window.x", "world": "main",
                                     "tabId": 4, "frameId": 2}


@pytest.mark.asyncio
async def test_page_eval_flags_still_loading_document(tools, server):
    # An eval that ran while the tab was mid-navigation (e.g. right after a
    # wait="none" reload) is flagged: either the document itself was still
    # parsing (readyState=loading) or the tab had a load in flight and the
    # eval hit Firefox's empty placeholder document (tabLoading). Without the
    # flag an early half-state read is indistinguishable from the final state.
    fn = await _fn(tools, "page_eval")
    for early in ({"readyState": "loading"}, {"tabLoading": True}):
        server.set_response("content.execute_script", resp(
            {"result": "", "url": "http://x/", **early}))
        out = await fn(code="document.title")
        assert "NOTE: the tab was still loading" in out, early
        assert "re-run after the page settles" in out, early

    # A settled document gets no note.
    server.set_response("content.execute_script", resp({
        "result": "Title", "url": "http://x/", "readyState": "complete"}))
    out = await fn(code="document.title")
    assert "still loading" not in out

    # Older extension (no readyState/tabLoading fields) formats exactly as before.
    server.set_response("content.execute_script", resp({
        "result": "Title", "url": "http://x/"}))
    out = await fn(code="document.title")
    assert out == "Script result (http://x/):\nTitle"


@pytest.mark.asyncio
async def test_page_eval_requires_code_or_script_name(tools, server, monkeypatch):
    fn = await _fn(tools, "page_eval")
    assert await fn() == "Error: provide code (or script_name)"
    monkeypatch.delenv("BROWSERPLUG_EXT_SCRIPTS", raising=False)
    out = await fn(script_name="gen.sh")
    assert out.startswith("Error: BROWSERPLUG_EXT_SCRIPTS is not set in the SERVER's environment")
    assert server.sent == []  # validation failures never hit the wire


# =============================================================================
# page_screenshot
# =============================================================================

@pytest.mark.asyncio
async def test_page_screenshot_returns_viewable_image(tools, server):
    server.set_response("tabs.screenshot", resp({
        "dataUrl": f"data:image/png;base64,{PNG_B64}", "format": "png",
        "method": "captureTab"}))
    fn = await _fn(tools, "page_screenshot")
    result = await fn(tab_id=3)
    req = _last(server)
    assert req["action"] == "tabs.screenshot"
    assert req["data"] == {"format": "png", "quality": 90, "fullPage": False, "tabId": 3}
    assert server.timeouts[-1] == 60.0
    kinds = [b.type for b in result.content]
    assert "text" in kinds and "image" in kinds
    img = next(b for b in result.content if b.type == "image")
    assert img.mime_type == "image/png"
    assert img.data == PNG_B64
    text = next(b.text for b in result.content if b.type == "text")
    assert "captureTab" in text and "tab 3" in text


@pytest.mark.asyncio
async def test_page_screenshot_full_page_jpeg_mapping(tools, server):
    server.set_response("tabs.screenshot", resp({
        "dataUrl": f"data:image/jpeg;base64,{PNG_B64}", "format": "jpeg",
        "method": "captureTab", "pages": 4}))
    fn = await _fn(tools, "page_screenshot")
    result = await fn(full_page=True, format="jpeg", quality=70)
    assert _last(server)["data"] == {"format": "jpeg", "quality": 70, "fullPage": True}
    text = next(b.text for b in result.content if b.type == "text")
    assert "4 stitched pages" in text


@pytest.mark.asyncio
async def test_page_screenshot_surfaces_page_warning(tools, server):
    server.set_response("tabs.screenshot", resp({
        "dataUrl": f"data:image/png;base64,{PNG_B64}", "format": "png",
        "method": "captureTab",
        "pageWarning": "window is minimized — capture may be blank"}))
    fn = await _fn(tools, "page_screenshot")
    result = await fn(tab_id=1)
    text = next(b.text for b in result.content if b.type == "text")
    assert "WARNING: window is minimized — capture may be blank" in text


@pytest.mark.asyncio
async def test_page_screenshot_error_returns_string(tools, server):
    server.set_response("tabs.screenshot", {"error": "boom"})
    fn = await _fn(tools, "page_screenshot")
    out = await fn(tab_id=1)
    assert out == "Error capturing screenshot: boom"

    server.set_response("tabs.screenshot", resp({"method": "captureVisibleTab"}))
    out = await fn(tab_id=1)
    assert out == "No screenshot data received (method=captureVisibleTab)"


@pytest.mark.asyncio
async def test_page_screenshot_selector_and_settle_ms_mapping(tools, server):
    # element capture: selector + settle_ms forwarded; the summary names the element.
    server.set_response("tabs.screenshot", resp({
        "dataUrl": f"data:image/png;base64,{PNG_B64}", "format": "png",
        "method": "element", "selector": "#card", "elementBox": {"w": 300, "h": 200}}))
    fn = await _fn(tools, "page_screenshot")
    result = await fn(tab_id=2, selector="#card", settle_ms=300)
    assert _last(server)["data"] == {"format": "png", "quality": 90, "fullPage": False,
                                     "tabId": 2, "selector": "#card", "settleMs": 300}
    text = next(b.text for b in result.content if b.type == "text")
    assert "element '#card' 300x200px" in text


@pytest.mark.asyncio
async def test_page_screenshot_surfaces_settle_warning(tools, server):
    # settle-by-default: a could-not-settle note is surfaced.
    server.set_response("tabs.screenshot", resp({
        "dataUrl": f"data:image/png;base64,{PNG_B64}", "format": "png",
        "method": "fullPage", "pages": 3,
        "settleWarning": "capture may not have fully settled (lazy media/animation still in progress)"}))
    fn = await _fn(tools, "page_screenshot")
    result = await fn(tab_id=1, full_page=True)
    text = next(b.text for b in result.content if b.type == "text")
    assert "NOTE: capture may not have fully settled" in text


# =============================================================================
# tabs
# =============================================================================

@pytest.mark.asyncio
async def test_tabs_list_markdown_format_and_flags(tools, server):
    server.set_response("tabs.list", resp({
        "tabs": [
            {"id": 1, "url": "http://a/", "title": "Alpha", "active": True,
             "windowId": 10, "pinned": False, "index": 0, "status": "complete",
             "lastAccessed": 1700000000000},
            {"id": 2, "url": "http://b/", "title": "Beta", "active": False,
             "windowId": 10, "pinned": True, "index": 1, "status": "loading",
             "audible": True, "lastAccessed": 1700000000000},
            {"id": 3, "url": "http://c/", "title": "Gamma", "active": True,
             "windowId": 20, "pinned": False, "index": 0, "discarded": True,
             "muted": True, "audible": True, "isInReaderMode": True,
             "lastAccessed": 1700000000000},
        ],
        "windows": [
            {"id": 10, "focused": True, "state": "normal", "type": "normal",
             "incognito": False},
            {"id": 20, "focused": False, "state": "minimized", "type": "popup",
             "incognito": True},
        ]}))
    fn = await _fn(tools, "tabs")
    out = await fn(recent=False)  # show every tab
    assert _last(server)["action"] == "tabs.list"
    # window headers: id, then focused / incognito / non-normal type / state
    assert "# Window 10 focused\n" in out
    assert "# Window 20 incognito popup minimized" in out
    # tab block shape (active tab is flagged)
    assert "## Alpha" in out
    assert "id: 1 active" in out
    assert "url: http://a/" in out
    assert "lastAccess: " in out
    assert out.rstrip().endswith("---")
    # flag rendering (order + muted-over-audible precedence; pinned is visible —
    # it used to be write-only)
    assert "id: 2 loading pinned audible" in out
    assert "id: 3 active muted discarded readerMode" in out
    # markers are bare words, not the old parenthesized style
    assert "(active)" not in out and "(focused)" not in out


@pytest.mark.asyncio
async def test_tabs_list_recent_filters_and_summarizes(tools, server):
    now = datetime.now().timestamp() * 1000
    old = now - 2 * 3600 * 1000  # 2h ago -> outside the 15-min window

    def tab(i, wid, idx, ts):
        return {"id": i, "url": f"http://t{i}/", "title": f"T{i}", "active": False,
                "windowId": wid, "pinned": False, "index": idx, "lastAccessed": ts}

    server.set_response("tabs.list", resp({
        "tabs": [
            tab(1, 10, 0, now), tab(2, 10, 1, now), tab(3, 10, 2, now),   # recent
            tab(4, 10, 3, old), tab(5, 10, 4, old), tab(6, 10, 5, old),   # old
            tab(7, 10, 6, old), tab(8, 10, 7, old),
            tab(9, 20, 0, old), tab(10, 20, 1, old),                      # no recent tab
        ],
        "windows": [
            {"id": 10, "state": "normal", "type": "normal", "incognito": False},
            {"id": 20, "state": "normal", "type": "normal", "incognito": False},
        ]}))
    fn = await _fn(tools, "tabs")
    out = await fn()  # recent defaults to True
    # 3 within 15 min -> keep max(5, 3) = 5 most-recent: T1..T3 + T4,T5
    assert "## T1" in out and "## T5" in out
    assert "## T6" not in out  # trimmed to the 5 kept
    # window 10 keeps 5 of its 8 -> notes 3 hidden (plural)
    assert "3 older tabs hidden" in out
    # window 20 has no recent tab -> omitted, summarized at the end
    assert "# Window 20" not in out
    assert "Windows hidden — no recent tabs: Window 20 (2 tabs)" in out


@pytest.mark.asyncio
async def test_tabs_list_marks_last_focused_when_backgrounded(tools, server):
    # Firefox is behind another app -> NO window is focused; the most-recently-
    # focused window (lastFocusedWindowId) is flagged `lastFocused` instead.
    server.set_response("tabs.list", resp({
        "tabs": [
            {"id": 1, "url": "http://a/", "title": "A", "active": True,
             "windowId": 10, "index": 0, "lastAccessed": 1700000000000},
            {"id": 2, "url": "http://b/", "title": "B", "active": True,
             "windowId": 20, "index": 0, "lastAccessed": 1700000000000},
        ],
        "windows": [
            {"id": 10, "focused": False, "state": "normal", "type": "normal"},
            {"id": 20, "focused": False, "state": "normal", "type": "normal"},
        ],
        "lastFocusedWindowId": 20,
    }))
    fn = await _fn(tools, "tabs")
    out = await fn(recent=False)
    assert "# Window 20 lastFocused" in out
    assert "# Window 10 lastFocused" not in out
    assert "# Window 10\n" in out          # window 10 carries no marker
    assert " focused" not in out           # nothing gets the bare `focused` flag


@pytest.mark.asyncio
async def test_tabs_list_hidden_count_is_singular_for_one(tools, server):
    now = datetime.now().timestamp() * 1000
    old = now - 2 * 3600 * 1000

    def tab(i, ts):
        return {"id": i, "url": f"http://t{i}/", "title": f"T{i}", "active": False,
                "windowId": 10, "index": i, "lastAccessed": ts}

    # 6 tabs, 3 within 15 min -> keep max(5, 3) = 5 -> exactly ONE hidden.
    server.set_response("tabs.list", resp({
        "tabs": [tab(1, now), tab(2, now), tab(3, now), tab(4, old), tab(5, old), tab(6, old)],
        "windows": [{"id": 10, "state": "normal", "type": "normal"}],
    }))
    fn = await _fn(tools, "tabs")
    out = await fn()
    assert "1 older tab hidden" in out     # singular
    assert "older tabs hidden" not in out


@pytest.mark.asyncio
async def test_tabs_open_creates_tab(tools, server):
    server.set_response("tabs.create", resp({
        "tab": {"id": 42, "windowId": 10, "url": "http://x/"}}))
    fn = await _fn(tools, "tabs")
    out = await fn(action="open", url="http://x/", background=True, pinned=True, window_id=10)
    req = _last(server)
    assert req["action"] == "tabs.create"
    assert req["data"] == {"url": "http://x/", "active": False, "pinned": True,
                           "windowId": 10}
    assert out == "Opened tab [42] in window 10: http://x/"
    # url is mandatory, and the refusal is a complete call to copy, in schema order
    refused = await fn(action="open", background=True, proxy="direct")
    assert refused.startswith('Error: url is required for action="open"')
    assert 'tabs(action="open", background=true, url="https://…")' in refused
    assert "never opens an empty tab" in refused


@pytest.mark.asyncio
async def test_tabs_open_reports_atomic_load_outcome(tools, server):
    # Atomic open-and-load: a non-committing open is reported, not dressed up as success.
    server.set_response("tabs.create", resp({
        "tab": {"id": 7, "windowId": 1, "url": "about:blank",
                "navPending": "http://slow/", "navTimedOut": True}}))
    fn = await _fn(tools, "tabs")
    out = await fn(action="open", url="http://slow/")
    assert "did not commit within 8s" in out and "http://slow/" in out

    server.set_response("tabs.create", resp({
        "tab": {"id": 8, "windowId": 1, "url": "http://err/",
                "navPending": "http://err/",
                "navError": "connection refused [NS_ERROR_CONNECTION_REFUSED]"}}))
    out2 = await fn(action="open", url="http://err/")
    assert "WARNING: navigation to http://err/ failed: connection refused" in out2


@pytest.mark.asyncio
async def test_tabs_open_new_window_uses_windows_create(tools, server):
    server.set_response("windows.create", resp({
        "window": {"id": 99, "tabs": [{"id": 7}]}}))
    fn = await _fn(tools, "tabs")
    out = await fn(action="open", url="http://x/", new_window=True, width=800, height=600)
    req = _last(server)
    assert req["action"] == "windows.create"
    assert req["data"] == {"url": "http://x/", "incognito": False, "focused": True,
                           "width": 800, "height": 600}
    assert out == "Opened window 99 with tab [7]: http://x/"

    out = await fn(action="open", url="http://x/", incognito=True)
    assert _last(server)["data"]["incognito"] is True
    assert out.startswith("Opened incognito window")


@pytest.mark.asyncio
async def test_tabs_open_window_reports_commit_state(tools, server):
    # The new-window/incognito open path must be exactly as commit-honest as the
    # new-tab path — never echo the REQUESTED url while the tab is actually parked
    # on about:blank.
    fn = await _fn(tools, "tabs")

    # did not commit (timeout): real url (about:blank) + NOTE, requested url only in the NOTE
    server.set_response("windows.create", resp({
        "window": {"id": 31, "tabs": [{"id": 5, "url": "about:blank"}],
                   "navPending": "http://localhost:5173/", "navTimedOut": True}}))
    out = await fn(action="open", url="http://localhost:5173/", incognito=True)
    assert "Opened incognito window 31 with tab [5]: about:blank" in out
    assert "NOTE: navigation to http://localhost:5173/ did not commit within 8s" in out
    assert "tab is on about:blank" in out

    # failed with a net error: WARNING names the cause
    server.set_response("windows.create", resp({
        "window": {"id": 32, "tabs": [{"id": 6, "url": "about:blank"}],
                   "navPending": "http://err/",
                   "navError": "connection refused [NS_ERROR_CONNECTION_REFUSED]"}}))
    out2 = await fn(action="open", url="http://err/", new_window=True)
    assert "with tab [6]: about:blank" in out2
    assert "WARNING: navigation to http://err/ failed: connection refused" in out2

    # committed: the reported url is the POST-COMMIT one (redirects followed)
    server.set_response("windows.create", resp({
        "window": {"id": 33, "tabs": [{"id": 7, "url": "http://final/landing"}]}}))
    out3 = await fn(action="open", url="http://start/", new_window=True)
    assert out3 == "Opened window 33 with tab [7]: http://final/landing"


@pytest.mark.asyncio
async def test_unknown_action_error_names_version_skew(tools, server):
    # An extension build older than the server rejects an action with
    # "Unknown ... action" — the error must name the version skew
    # and the remedy, so the agent stops retrying the dead tool.
    server.extension_version = "1.2.9"
    server.set_response("navigation.navigate", {
        "type": "error",
        "data": {"message": "Unknown navigation action: navigation.navigate"}})
    fn = await _fn(tools, "navigate")
    out = await fn(url="http://x/")
    assert "Unknown navigation action: navigation.navigate" in out
    assert "version skew" in out
    assert "v1.2.9" in out
    assert "Reload the BrowserPlug extension" in out

    # an ordinary error must NOT get the skew hint
    server.set_response("navigation.navigate", {
        "type": "error", "data": {"message": "No tab with id 99"}})
    out2 = await fn(url="http://x/", tab_id=99)
    assert "version skew" not in out2


@pytest.mark.asyncio
async def test_tabs_close_tab_and_window(tools, server):
    fn = await _fn(tools, "tabs")
    out = await fn(action="close", tab_id=4)
    assert _last(server)["action"] == "tabs.close"
    assert _last(server)["data"] == {"tabId": 4}
    assert out == "Closed tab 4"

    out = await fn(action="close", window_id=10)
    assert _last(server)["action"] == "windows.close"
    assert _last(server)["data"] == {"windowId": 10}
    assert out == "Closed window 10"

    out = await fn(action="close")
    assert out == 'Error: action="close" needs tab_id (or window_id to close a window)'


@pytest.mark.asyncio
async def test_tabs_activate_uses_switch_with_focus(tools, server):
    server.set_response("tabs.switch", resp({"focusedWindow": True}))
    fn = await _fn(tools, "tabs")
    out = await fn(action="activate", tab_id=11)
    req = _last(server)
    assert req["action"] == "tabs.switch"
    assert req["data"] == {"tabId": 11, "focusWindow": True}
    assert out == "Activated tab 11 and focused its window"

    await fn(action="activate", tab_id=11, focus_window=False)
    assert _last(server)["data"]["focusWindow"] is False

    assert (await fn(action="activate")) == 'Error: tab_id is required for action="activate"'


@pytest.mark.asyncio
async def test_tabs_window_update_with_explicit_id(tools, server):
    server.set_response("windows.update", resp({
        "window": {"state": "maximized", "width": 800, "height": 600}}))
    fn = await _fn(tools, "tabs")
    out = await fn(action="window", window_id=10, state="maximized",
                   width=800, height=600)
    req = _last(server)
    assert req["action"] == "windows.update"
    assert req["data"] == {"windowId": 10, "state": "maximized", "width": 800,
                           "height": 600, "focused": True}
    assert out == "Window 10 updated: state=maximized, 800x600"


@pytest.mark.asyncio
async def test_tabs_window_update_resolves_last_focused_first(tools, server):
    server.set_response("windows.get_last_focused", resp({"window": {"id": 77}}))
    server.set_response("windows.update", resp({"window": {"state": "minimized"}}))
    fn = await _fn(tools, "tabs")
    await fn(action="window", state="minimized")
    assert [r["action"] for r in server.sent] == ["windows.get_last_focused", "windows.update"]
    assert server.sent[1]["data"]["windowId"] == 77


@pytest.mark.asyncio
async def test_tabs_unknown_action(tools, server):
    fn = await _fn(tools, "tabs")
    out = await fn(action="bogus")
    assert out == 'Error: unknown action "bogus" (use list | active | find | open | close | activate | window | routing)'
    assert server.sent == []


# =============================================================================
# tabs — action="active" (fable-feedback R3) and action="find" (R5)
# =============================================================================

@pytest.mark.asyncio
async def test_tabs_active_one_shot_accessor(tools, server):
    server.set_response("tabs.active", resp({
        "tab": {"id": 915, "windowId": 42, "title": "Dashboard",
                "url": "http://localhost:8899/dash"}}))
    fn = await _fn(tools, "tabs")
    out = await fn(action="active")
    req = _last(server)
    assert req["action"] == "tabs.active"
    assert req["data"] == {}
    assert out == ("Active tab [915] in window 42: Dashboard\n"
                   "url: http://localhost:8899/dash")


@pytest.mark.asyncio
async def test_tabs_active_handles_no_active_tab(tools, server):
    server.set_response("tabs.active", resp({"tab": None}))
    fn = await _fn(tools, "tabs")
    assert (await fn(action="active")) == "No active tab found"


@pytest.mark.asyncio
async def test_tabs_find_matches_url_or_title_substring_most_recent_first(tools, server):
    server.set_response("tabs.list", resp({
        "tabs": [
            {"id": 1, "url": "http://app.example/dashboard", "title": "Old dash",
             "windowId": 10, "lastAccessed": 1000},
            {"id": 2, "url": "http://other.example/", "title": "My Dashboard",
             "windowId": 10, "active": True, "lastAccessed": 3000},
            {"id": 3, "url": "http://unrelated.example/", "title": "News",
             "windowId": 20, "lastAccessed": 2000},
        ]}))
    fn = await _fn(tools, "tabs")
    out = await fn(action="find", url="DASHBOARD")  # case-insensitive
    assert _last(server)["action"] == "tabs.list"
    lines = out.splitlines()
    assert lines[0] == '2 tabs match "DASHBOARD":'
    # most recently used first; active flag carried; title + url shown
    assert lines[1] == "[2] active window 10: My Dashboard — http://other.example/"
    assert lines[2] == "[1] window 10: Old dash — http://app.example/dashboard"
    assert "News" not in out


@pytest.mark.asyncio
async def test_tabs_find_no_match_and_missing_needle(tools, server):
    server.set_response("tabs.list", resp({
        "tabs": [{"id": 1, "url": "http://a/", "title": "A", "windowId": 1}]}))
    fn = await _fn(tools, "tabs")
    out = await fn(action="find", url="zzz")
    assert out == 'No open tab matches "zzz" (searched url + title of 1 tabs)'

    out = await fn(action="find")
    assert out.startswith('Error: url is required for action="find"')
    assert 'tabs(action="find", url="<word>")' in out
    # the missing-needle error is validated before hitting the wire
    assert server.sent[-1]["action"] == "tabs.list"  # only the zzz call went out


@pytest.mark.asyncio
async def test_tabs_find_caps_output_at_ten(tools, server):
    server.set_response("tabs.list", resp({
        "tabs": [{"id": i, "url": f"http://match.example/{i}", "title": f"T{i}",
                  "windowId": 1, "lastAccessed": i} for i in range(12)]}))
    fn = await _fn(tools, "tabs")
    out = await fn(action="find", url="match.example")
    assert '12 tabs match "match.example" (showing the 10 most recently used):' in out
    assert "[11]" in out and "[2]" in out  # newest kept
    assert "[1]" not in out and "[0]" not in out  # oldest two dropped


# =============================================================================
# history
# =============================================================================

# =============================================================================
# requests
# =============================================================================

@pytest.mark.asyncio
async def test_requests_recent_default_mapping(tools, server):
    server.set_response("requests.recent", resp({
        "tabId": 4, "total": 0, "returned": 0, "requests": []}))
    fn = await _fn(tools, "requests")
    out = await fn()
    req = _last(server)
    assert req["action"] == "requests.recent"
    assert req["data"] == {"limit": 30, "includeStatic": False}
    assert out.startswith("No requests in the log for tab 4")


@pytest.mark.asyncio
async def test_requests_recent_optional_args_and_line_format(tools, server):
    server.set_response("requests.recent", resp({
        "tabId": 4, "total": 12, "returned": 2, "requests": [
            {"method": "GET", "url": "http://a/api/u", "type": "xhr", "status": 200,
             "durMs": 120},
            {"method": "POST", "url": "http://a/api/login", "type": "xhr",
             "error": "NS_ERROR_ABORT", "fromCache": True, "ageMs": 2500},
        ]}))
    fn = await _fn(tools, "requests")
    out = await fn(tab_id=4, filter="/api/", limit=5)
    assert _last(server)["data"] == {"limit": 5, "includeStatic": False, "tabId": 4,
                                     "filter": "/api/"}
    assert "Tab 4: showing 2 of 12 logged requests (newest last):" in out
    assert "- GET http://a/api/u -> 200 (120ms) [xhr]" in out
    # errored entry without a known peer: dialed state is EXPLICITLY unknown
    assert "- POST http://a/api/login -> ERR(NS_ERROR_ABORT) [cache] [dialed: unknown] [xhr] 2.5s ago" in out


@pytest.mark.asyncio
async def test_requests_recent_dialed_peer_address(tools, server):
    # The request log carries the dialed peer IP when Firefox knows it, and
    # marks it EXPLICITLY unknown on pending/errored
    # entries (never silently omitted) — so an IPv4-vs-IPv6 split is visible
    # from inside the browser.
    server.set_response("requests.recent", resp({
        "tabId": 9, "total": 3, "returned": 3, "requests": [
            {"method": "GET", "url": "http://localhost:4000/up", "type": "main_frame",
             "status": 200, "durMs": 90, "ip": "127.0.0.1"},
            {"method": "GET", "url": "http://localhost:5173/", "type": "main_frame",
             "status": "pending", "ageMs": 4100},
            {"method": "GET", "url": "http://a/cached.js", "type": "script",
             "status": 200, "fromCache": True},
        ]}))
    fn = await _fn(tools, "requests")
    out = await fn(tab_id=9)
    assert "- GET http://localhost:4000/up -> 200 (90ms) [dialed 127.0.0.1] [main_frame]" in out
    assert "- GET http://localhost:5173/ -> pending [dialed: unknown] [main_frame] 4.1s ago" in out
    # a healthy completed entry without an ip (e.g. cache hit) gets NO dialed marker
    assert "- GET http://a/cached.js -> 200 [cache] [script]" in out
    assert out.count("dialed") == 2


@pytest.mark.asyncio
async def test_requests_start_requires_and_maps_url_patterns(tools, server):
    fn = await _fn(tools, "requests")
    out = await fn(action="start")
    assert out == 'Error: url_patterns is required for action="start" (e.g. ["*/api/*"])'
    assert server.sent == []

    server.set_response("requests.start_monitoring", resp({"monitor_id": "m1"}))
    out = await fn(action="start", url_patterns=["*/api/*"], tab_id=3,
                   options={"content_types_to_capture": []})
    req = _last(server)
    assert req["action"] == "requests.start_monitoring"
    assert req["data"]["url_patterns"] == ["*/api/*"]
    assert req["data"]["tab_id"] == 3
    opts = req["data"]["options"]
    assert opts["content_types_to_capture"] == []  # caller override wins
    assert opts["capture_response_bodies"] is True  # defaults preserved
    assert '"monitor_id":"m1"' in out


@pytest.mark.asyncio
async def test_requests_stop_list_get_validation(tools, server):
    fn = await _fn(tools, "requests")
    assert (await fn(action="stop")) == 'Error: monitor_id is required for action="stop"'
    assert (await fn(action="list")) == 'Error: monitor_id is required for action="list"'
    assert (await fn(action="get")) == \
        'Error: monitor_id and request_id are required for action="get"'
    assert (await fn(action="get", monitor_id="m1")) == \
        'Error: monitor_id and request_id are required for action="get"'
    assert server.sent == []
    out = await fn(action="bogus")
    assert out == 'Error: unknown action "bogus" (use recent | start | stop | list | get)'


@pytest.mark.asyncio
async def test_requests_stop_list_get_mapping(tools, server):
    fn = await _fn(tools, "requests")

    await fn(action="stop", monitor_id="m1")
    assert _last(server)["action"] == "requests.stop_monitoring"
    # no drain_timeout: the extension has no drain, it retired the monitor at once
    assert _last(server)["data"] == {"monitor_id": "m1"}

    await fn(action="list", monitor_id="m1")
    assert _last(server)["action"] == "requests.list_captured"
    assert _last(server)["data"] == {"monitor_id": "m1"}

    await fn(action="get", monitor_id="m1", request_id="r1", include_binary=True)
    assert _last(server)["action"] == "requests.get_content"
    assert _last(server)["data"] == {"monitor_id": "m1", "request_id": "r1",
                                     "include_binary": True}


# =============================================================================
# server_info
# =============================================================================

@pytest.mark.asyncio
async def test_server_info_connected(tools, server):
    class FakeExt:
        close_code = None

    server.extension_connection = FakeExt()
    server.extension_version = "1.3.0"
    server.get_connections_info = lambda: {"clients": [{"pid": 123}]}
    fn = await _fn(tools, "server_info")
    out = await fn()
    assert "Extension connected (version: 1.3.0)" in out
    assert server.sent == []  # no WebSocket round-trip
    # verbose=True still returns the full connection dump; the DEFAULT is the summary,
    # because that payload had a 10 KB median for a pre-flight health check.
    assert '"pid": 123' in await fn(verbose=True)


@pytest.mark.asyncio
async def test_server_info_not_connected(tools, server):
    def unavailable():
        raise RuntimeError("no connection report")
    server.get_connections_info = unavailable
    fn = await _fn(tools, "server_info")
    out = await fn()
    assert "Extension NOT connected (version: unknown)" in out
    assert "(connection details unavailable" in out  # the report itself failed
    assert server.sent == []


# =============================================================================
# Error propagation through _request (shared path)
# =============================================================================

@pytest.mark.asyncio
async def test_extension_error_response_is_reported(tools, server):
    server.set_response("content.read", {"type": "error",
                                         "data": {"message": "Tab 99 not found"}})
    fn = await _fn(tools, "page_read")
    out = await fn(tab_id=99)
    assert out == "Error reading page: Tab 99 not found"


@pytest.mark.asyncio
async def test_missing_extension_hint_on_malformed_response(tools, server):
    # Subject is the malformed-response hint, not the tool it happens to ride. Retargeted
    # from `history` (removed 2026-08-03) to a tool that still exists.
    server.set_response("tabs.list", {})
    fn = await _fn(tools, "tabs")
    out = await fn(action="list")
    assert "is a browser running with the BrowserPlug extension connected" in out


# =============================================================================
# page_snapshot mode="records" (structured listing extraction)
# =============================================================================

@pytest.mark.asyncio
async def test_page_snapshot_records_mode_argument_mapping(tools, server):
    server.set_response("content.snapshot", resp({
        "ok": True, "mode": "records", "pageState": {"url": "u", "title": "t"},
        "detection": {"method": "item_selector", "itemSelector": "article.card"},
        "count": 0, "totalItems": 0, "records": []}))
    fn = await _fn(tools, "page_snapshot")
    await fn(mode="records", item_selector="article.card", root="#results",
             max_elements=40, tab_id=3)
    data = server.sent[0]["data"]
    assert data["mode"] == "records"
    assert data["item_selector"] == "article.card"
    assert data["root"] == "#results"
    assert data["max"] == 40
    assert data["tabId"] == 3


@pytest.mark.asyncio
async def test_page_snapshot_records_omits_item_selector_when_unset(tools, server):
    server.set_response("content.snapshot", resp({
        "ok": True, "mode": "records", "pageState": {},
        "detection": {"method": "auto"}, "count": 0, "totalItems": 0, "records": []}))
    fn = await _fn(tools, "page_snapshot")
    await fn(mode="records")
    assert "item_selector" not in server.sent[0]["data"]


@pytest.mark.asyncio
async def test_page_snapshot_records_formats_one_row_per_card(tools, server):
    server.set_response("content.snapshot", resp({
        "ok": True, "mode": "records", "pageState": {"url": "https://x/l", "title": "Listing"},
        "detection": {"method": "auto", "itemSelector": "article.card",
                      "containerSelector": "div.grid", "score": 900,
                      "candidatesConsidered": 2, "linkedShare": 1.0, "pricedShare": 1.0},
        "count": 2, "totalItems": 2, "truncated": False, "withPrice": 2, "ambiguousCount": 0,
        "records": [
            {"index": 0, "ref": "e1", "title": "Seagate Expansion 8TB",
             "price": {"text": "799,00 zl", "value": 799.0, "currency": "zl"},
             "link": "https://x/p/1"},
            {"index": 1, "ref": "e2", "title": "WD Elements 12TB",
             "price": {"text": "1199,00 zl", "value": 1199.0},
             "priceWas": {"text": "1499,00 zl", "value": 1499.0, "struck": True},
             "link": "https://x/p/2", "linkUnwrappedFrom": "https://x/events/clicks?redirect=..."},
        ]}))
    fn = await _fn(tools, "page_snapshot")
    out = await fn(mode="records")
    assert "Records (2 records, 2 with a single clear price)" in out
    assert "detected as article.card under div.grid" in out
    assert '0 "Seagate Expansion 8TB" | 799,00 zl (=799.0) | https://x/p/1' in out
    assert "was 1499,00 zl (=1499.0)" in out
    assert "(link unwrapped from a tracking redirector)" in out


@pytest.mark.asyncio
async def test_page_snapshot_records_marks_ambiguous_rows_and_explains(tools, server):
    # The core of records mode: a card holding two live prices gets NO price attributed, its
    # candidates listed, and a note saying why — never a silently-picked first match.
    server.set_response("content.snapshot", resp({
        "ok": True, "mode": "records", "pageState": {},
        "detection": {"method": "auto", "itemSelector": "article.card"},
        "count": 1, "totalItems": 1, "withPrice": 0, "ambiguousCount": 1,
        "records": [
            {"index": 0, "ref": "e1", "title": "WD My Book 14TB", "price": None,
             "ambiguous": True, "variantControls": 2,
             "prices": [{"text": "1829,74 zl", "value": 1829.74},
                        {"text": "1199,00 zl", "value": 1199.0}],
             "link": "https://x/p/mybook"},
        ]}))
    fn = await _fn(tools, "page_snapshot")
    out = await fn(mode="records")
    assert "1 AMBIGUOUS" in out
    assert "AMBIGUOUS price (1829,74 zl (=1829.74), 1199,00 zl (=1199.0))" in out
    assert "more than one live price inside the SAME card" in out
    assert "page_eval finds the same prices" in out


@pytest.mark.asyncio
async def test_page_snapshot_records_labels_each_ambiguous_candidate(tools, server):
    # A card that shows a price with and without delivery is still AMBIGUOUS, but each
    # candidate carries the words beside it, so the agent can choose instead of scraping.
    server.set_response("content.snapshot", resp({
        "ok": True, "mode": "records", "pageState": {},
        "detection": {"method": "auto", "itemSelector": "li.offer"},
        "count": 1, "totalItems": 1, "withPrice": 0, "ambiguousCount": 1,
        "records": [
            {"index": 0, "ref": "e1", "title": "Pendrive 128 GB", "price": None,
             "ambiguous": True,
             "prices": [{"text": "36,99 zł", "value": 36.99},
                        {"text": "47,48 zł", "value": 47.48, "label": "z dostawą"},
                        {"text": "52,00 zł", "value": 52.0, "struck": True}],
             "link": "https://x/oferta/1"},
        ]}))
    fn = await _fn(tools, "page_snapshot")
    out = await fn(mode="records")
    assert 'AMBIGUOUS price (36,99 zł (=36.99), 47,48 zł (=47.48) "z dostawą")' in out
    assert "52,00" not in out.split("AMBIGUOUS price", 1)[1].splitlines()[0].split("|")[0], \
        "a struck price is not a live candidate"
    assert "words printed beside it" in out


@pytest.mark.asyncio
async def test_page_snapshot_records_reports_truncation_and_runner_up(tools, server):
    server.set_response("content.snapshot", resp({
        "ok": True, "mode": "records", "pageState": {},
        "detection": {"method": "auto", "itemSelector": "li.item",
                      "runnerUp": {"count": 6, "selector": "div.tile"}},
        "count": 5, "totalItems": 40, "truncated": True, "withPrice": 5, "ambiguousCount": 0,
        "records": [{"index": i, "ref": f"e{i}", "title": f"Drive {i}",
                     "price": {"text": "1 zl", "value": 1.0}} for i in range(5)]}))
    fn = await _fn(tools, "page_snapshot")
    out = await fn(mode="records", max_elements=5)
    assert "5 of 40 records — TRUNCATED, raise max_elements" in out
    assert "runner-up pattern: 6x div.tile" in out


@pytest.mark.asyncio
async def test_page_snapshot_records_refusal_is_surfaced_verbatim(tools, server):
    server.set_response("content.snapshot", resp({
        "ok": False, "mode": "records",
        "error": "no repeated-card pattern could be identified with confidence on this page. "
                 "Rather than return a guess as data: pass item_selector=..."}))
    fn = await _fn(tools, "page_snapshot")
    out = await fn(mode="records")
    assert out.startswith("Error taking snapshot: no repeated-card pattern")
    assert "item_selector" in out


@pytest.mark.asyncio
async def test_page_snapshot_records_row_without_title_falls_back_to_text(tools, server):
    server.set_response("content.snapshot", resp({
        "ok": True, "mode": "records", "pageState": {},
        "detection": {"method": "auto"}, "count": 1, "totalItems": 1,
        "withPrice": 0, "ambiguousCount": 0,
        "records": [{"index": 0, "ref": "e1", "text": "Untitled block of card text"}]}))
    fn = await _fn(tools, "page_snapshot")
    out = await fn(mode="records")
    assert '0 "Untitled block of card text" | no price found' in out


# =============================================================================
# page_act surfaces the clicked control's real state
# =============================================================================

@pytest.mark.asyncio
async def test_page_act_click_reports_control_checked_state(tools, server):
    server.set_response("content.act", resp({
        "ok": True, "completedSteps": 1, "totalSteps": 1, "results": [
            {"ok": True, "step": 0, "stepAction": "click", "clicked": True,
             "control": {"ref": "e7", "kind": "native", "checkedBefore": False,
                         "checked": True, "checkedChanged": True},
             "domChanged": True}]}))
    fn = await _fn(tools, "page_act")
    out = await fn(steps=[{"action": "click", "target": "label[for=cb]"}])
    assert "control e7 is now checked (was unchecked)" in out


@pytest.mark.asyncio
async def test_page_act_click_that_did_not_toggle_is_a_failure_with_the_state(tools, server):
    server.set_response("content.act", resp({
        "ok": False, "completedSteps": 0, "totalSteps": 1, "results": [
            {"ok": False, "step": 0, "stepAction": "click", "checkedUnchanged": True,
             "error": "clicked, but the control did not change state: .checked is still false",
             "control": {"ref": "e7", "kind": "native", "checkedBefore": False,
                         "checked": False, "checkedChanged": False},
             "reachability": {"reachable": False, "why": "covered by an overlay"},
             "target": {"ref": "e60", "role": "generic", "name": "4000 GB (13)"}}]}))
    fn = await _fn(tools, "page_act")
    out = await fn(steps=[{"action": "click", "target": "e60"}])
    assert out.startswith("FAILED: 0/1 steps completed")
    assert "Step 0 click: FAILED - clicked, but the control did not change state".replace(
        " - ", " — ") in out
    # the control + reachability blocks reach the agent, not just the sentence
    assert '"control"' in out and '"reachability"' in out


@pytest.mark.asyncio
async def test_page_act_click_warns_about_a_dangling_label_for(tools, server):
    server.set_response("content.act", resp({
        "ok": True, "completedSteps": 1, "totalSteps": 1, "results": [
            {"ok": True, "step": 0, "stepAction": "click", "labelForMissing": "cap-4000",
             "domChanged": True, "structuralChange": True}]}))
    fn = await _fn(tools, "page_act")
    out = await fn(steps=[{"action": "click", "target": "label"}])
    assert 'WARNING: label points at for="cap-4000" which is not in the document' in out


@pytest.mark.asyncio
async def test_page_act_no_effect_click_names_the_probable_cause(tools, server):
    server.set_response("content.act", resp({
        "ok": True, "completedSteps": 1, "totalSteps": 1, "results": [
            {"ok": True, "step": 0, "stepAction": "click", "domChanged": False,
             "noEffectHint": "the target is inside a closed <details> section (details#acc)"}]}))
    fn = await _fn(tools, "page_act")
    out = await fn(steps=[{"action": "click", "target": "#x"}])
    assert "NO visible effect (domChanged=false)" in out
    assert "probable cause: the target is inside a closed <details> section" in out


# =============================================================================
# A timed-out act step still reports the page state
# =============================================================================

@pytest.mark.asyncio
async def test_page_act_sends_a_budget_below_its_own_timeout(tools, server):
    server.set_response("content.act", resp({
        "ok": True, "completedSteps": 1, "totalSteps": 1, "results": []}))
    fn = await _fn(tools, "page_act")
    await fn(steps=[{"action": "click", "target": "Go"}])
    budget_ms = _last(server)["data"]["budgetMs"]
    server_timeout_ms = server.timeouts[0] * 1000
    assert budget_ms < server_timeout_ms, (
        "the extension must give up FIRST, or its structured report never gets sent")
    assert server_timeout_ms - budget_ms >= 3000


@pytest.mark.asyncio
async def test_page_act_budget_covers_a_step_that_names_its_own_timeout(tools, server):
    # A click carrying timeout_ms is asking the EXTENSION to wait that long before
    # reporting; if the server's own budget did not grow to match, it would give up
    # first and the structured report would never arrive.
    server.set_response("content.act", resp({
        "ok": True, "completedSteps": 1, "totalSteps": 1, "results": []}))
    fn = await _fn(tools, "page_act")
    await fn(steps=[{"action": "click", "target": "#load-more", "timeout_ms": 30000}])
    assert server.timeouts[0] >= 50.0, server.timeouts
    assert _last(server)["data"]["budgetMs"] >= 30000


@pytest.mark.asyncio
async def test_page_act_budget_has_a_floor(tools, server):
    server.set_response("content.act", resp({
        "ok": True, "completedSteps": 1, "totalSteps": 1, "results": []}))
    fn = await _fn(tools, "page_act")
    # steps=[] used to be the smallest plan; it is now refused before the wire
    # (test_tool_validation.py), so the floor is checked with one cheap step.
    await fn(steps=[{"action": "hover", "target": "#x", "settle_ms": 1}])
    assert _last(server)["data"]["budgetMs"] >= 6000


@pytest.mark.asyncio
async def test_page_act_timed_out_step_prints_the_page_state(tools, server):
    # No navigation: the step's report is missing for a reason nobody knows, so it stays a
    # failure, with the page state the extension saw.
    server.set_response("content.act", resp({
        "ok": False, "completedSteps": 0, "totalSteps": 1, "results": [
            {"ok": False, "step": 0, "stepAction": "click", "timedOut": True,
             "actionTimedOut": True, "elapsedMs": 12000, "requestedTimeoutMs": 12000,
             "urlBefore": "https://x/produkt/wd-12tb",
             "urlAfter": "https://x/produkt/wd-12tb#specs",
             "titleAfter": "WD 12TB", "tabStatus": "complete",
             "urlChanged": True, "navigated": False,
             "error": "the click step did not report back within 12s. The page DID navigate "
                      "while it ran, so the action almost certainly took effect."}]}))
    fn = await _fn(tools, "page_act")
    tool_outcome.arm()
    out = await fn(steps=[{"action": "click", "target": "16TB"}])
    assert (tool_outcome.read() or {}).get("failed")
    assert "Step 0 click: TIMED OUT" in out
    assert "url now https://x/produkt/wd-12tb#specs" in out
    assert "was https://x/produkt/wd-12tb" in out
    assert 'title "WD 12TB"' in out


_NAVIGATED_CLICK = {
    "ok": False, "step": 0, "stepAction": "click", "timedOut": True,
    "actionTimedOut": True, "elapsedMs": 12000, "requestedTimeoutMs": 12000,
    "urlBefore": "http://x/form", "urlAfter": "http://x/form/submit",
    "titleAfter": "Application received", "tabStatus": "complete",
    "urlChanged": True, "navigated": True,
    "error": "the click step did not report back within 12s. The page DID navigate while "
             "it ran (now at http://x/form/submit), so the action almost certainly took effect."}


@pytest.mark.asyncio
async def test_page_act_click_that_navigated_while_its_report_was_lost_is_a_success(tools, server):
    # A submit replaces the document the step ran in, so its report can never come back.
    # The eval of 2026-10-08 had every such submit flagged isError.
    server.set_response("content.act", resp({
        "ok": False, "completedSteps": 0, "totalSteps": 1, "results": [dict(_NAVIGATED_CLICK)]}))
    fn = await _fn(tools, "page_act")
    tool_outcome.arm()
    out = await fn(steps=[{"action": "click", "target": "Submit"}])
    outcome = tool_outcome.read() or {}
    assert not outcome.get("failed"), out
    assert [d["kind"] for d in outcome.get("degraded", [])] == ["step_report_lost"]
    assert out.startswith("OK: 1/1 steps completed")
    assert ('Step 0 click: ok — the page navigated to http://x/form/submit '
            '("Application received") while the step ran; was http://x/form.') in out
    assert "TIMED OUT" not in out and "FAILED" not in out


@pytest.mark.asyncio
async def test_page_act_steps_after_a_navigating_step_are_named_as_never_run(tools, server):
    server.set_response("content.act", resp({
        "ok": False, "completedSteps": 1, "totalSteps": 3, "results": [
            {"ok": True, "step": 0, "stepAction": "fill"},
            dict(_NAVIGATED_CLICK, step=1)]}))
    fn = await _fn(tools, "page_act")
    tool_outcome.arm()
    out = await fn(steps=[{"action": "fill", "target": "#name", "value": "Ada"},
                          {"action": "click", "target": "Submit"},
                          {"action": "wait", "text_contains": "received"}])
    outcome = tool_outcome.read() or {}
    assert not outcome.get("failed"), out
    assert out.startswith("FAILED: 2/3 steps completed")
    assert "Step 2 never ran: the batch stops at a step that replaced the page" in out
    partial = [d for d in outcome.get("degraded", []) if d["kind"] == "partial_steps"]
    assert partial and partial[0]["not_run"] == [2]


@pytest.mark.asyncio
async def test_page_act_a_timeout_that_saw_nothing_or_a_closed_tab_stays_a_failure(tools, server):
    fn = await _fn(tools, "page_act")
    for res in ({"navigated": False, "urlChanged": False},
                {"navigated": True, "tabClosed": True}):
        server.set_response("content.act", resp({
            "ok": False, "completedSteps": 0, "totalSteps": 1,
            "results": [dict(_NAVIGATED_CLICK, **res)]}))
        tool_outcome.arm()
        out = await fn(steps=[{"action": "click", "target": "Submit"}])
        assert (tool_outcome.read() or {}).get("failed"), out
        assert "TIMED OUT" in out


@pytest.mark.asyncio
async def test_page_act_wait_timeout_wording_is_unaffected_by_the_action_timeout(tools, server):
    # A wait-step timeout keeps its own (different) diagnostics path.
    server.set_response("content.act", resp({
        "ok": False, "completedSteps": 0, "totalSteps": 1, "results": [
            {"ok": False, "step": 0, "stepAction": "wait", "timedOut": True,
             "elapsedMs": 5000, "requestedTimeoutMs": 5000}]}))
    fn = await _fn(tools, "page_act")
    out = await fn(steps=[{"action": "wait", "target": "#x", "timeout_ms": 5000}])
    assert "Step 0 wait: FAILED".replace("FAILED", "FAILED") in out
    assert "timed out after 5.0s (requested 5.0s)" in out
    assert "TIMED OUT" not in out


@pytest.mark.asyncio
async def test_page_act_reports_a_wait_clamped_by_the_batch_budget(tools, server):
    server.set_response("content.act", resp({
        "ok": False, "completedSteps": 0, "totalSteps": 1, "results": [
            {"ok": False, "step": 0, "stepAction": "wait", "timedOut": True,
             "elapsedMs": 4000, "requestedTimeoutMs": 4000, "timeoutClampedToMs": 4000}]}))
    fn = await _fn(tools, "page_act")
    out = await fn(steps=[{"action": "wait", "target": "#x", "timeout_ms": 60000}])
    assert "the wait was clamped to 4.0s by this call" in out


@pytest.mark.asyncio
async def test_page_act_reports_steps_that_never_ran(tools, server):
    server.set_response("content.act", resp({
        "ok": False, "completedSteps": 1, "totalSteps": 3, "batchTimedOut": True,
        "notRunSteps": [{"step": 1, "stepAction": "click"},
                        {"step": 2, "stepAction": "fill"}],
        "results": [{"ok": True, "step": 0, "stepAction": "click", "domChanged": True}]}))
    fn = await _fn(tools, "page_act")
    out = await fn(steps=[{"action": "click", "target": "a"},
                          {"action": "click", "target": "b"},
                          {"action": "fill", "target": "c", "value": "d"}])
    assert "BATCH BUDGET EXHAUSTED: steps 1, 2 never ran" in out
    assert "Re-issue the remaining steps as a separate page_act call" in out


@pytest.mark.asyncio
async def test_page_act_transport_timeout_probes_the_tab_state(tools, server):
    # If the whole batch still overruns the SERVER budget, a bare "Request timed out"
    # reads as "nothing happened" — which was wrong every time it was checked.
    server.set_response("content.act", {"error": "Request timed out after 22.1 seconds"})
    server.set_response("tabs.list", resp({"tabs": [
        {"id": 7, "url": "https://x/wroclaw", "title": "Wroclaw results",
         "active": True, "status": "complete"}]}))
    fn = await _fn(tools, "page_act")
    out = await fn(steps=[{"action": "click", "target": "szukaj"}])
    assert [r["action"] for r in server.sent] == ["content.act", "tabs.list"]
    assert "The tab is NOW at https://x/wroclaw" in out
    assert "A timeout does NOT mean nothing happened" in out


@pytest.mark.asyncio
async def test_page_act_transport_timeout_probe_targets_the_named_tab(tools, server):
    server.set_response("content.act", {"error": "Request timed out after 22.1 seconds"})
    server.set_response("tabs.list", resp({"tabs": [
        {"id": 1, "url": "https://other/", "title": "Other", "active": True, "status": "complete"},
        {"id": 9, "url": "https://x/target", "title": "Target", "active": False,
         "status": "loading"}]}))
    fn = await _fn(tools, "page_act")
    out = await fn(steps=[{"action": "click", "target": "Go"}], tab_id=9)
    assert "The tab is NOW at https://x/target" in out
    assert "status=loading" in out


@pytest.mark.asyncio
async def test_page_act_transport_timeout_probe_failure_is_harmless(tools, server):
    server.set_response("content.act", {"error": "Request timed out after 22.1 seconds"})
    server.set_response("tabs.list", {"error": "No extension connection available"})
    fn = await _fn(tools, "page_act")
    out = await fn(steps=[{"action": "click", "target": "Go"}])
    assert out == "Error executing steps: Request timed out after 22.1 seconds"


@pytest.mark.asyncio
async def test_page_act_non_timeout_error_does_not_probe(tools, server):
    server.set_response("content.act",
                        {"type": "error", "data": {"message": "Tab 9 no longer exists."}})
    fn = await _fn(tools, "page_act")
    out = await fn(steps=[{"action": "click", "target": "Go"}])
    assert [r["action"] for r in server.sent] == ["content.act"]
    assert out.startswith("Error executing steps: Tab 9 no longer exists")
    assert 'tabs(action="open", background=true, url="https://…")' in out


@pytest.mark.asyncio
async def test_page_snapshot_records_output_stays_within_a_sane_size(tools, server):
    """A full page of records must not blow the MCP response up.

    Each row is one line by design, and the per-card `text` is only printed when a
    row has no title. Nothing enforced that: adding `prices`, `text` or the digest
    to every row would quietly multiply the response by 5-10x, which an agent pays
    for on every listing page. 150 rows is the tool's own max_elements default.
    """
    records = []
    for i in range(150):
        records.append({
            "index": i,
            "ref": f"e{i}",
            "title": f"Seagate IronWolf Pro {i} 8TB SATA III 3.5 cala 7200 obr/min NAS",
            "price": {"text": "1 199,00 zl", "value": 1199.0, "currency": "zl"},
            "prices": [{"text": "1 499,00 zl", "value": 1499.0, "struck": True},
                       {"text": "1 199,00 zl", "value": 1199.0}],
            "priceWas": {"text": "1 499,00 zl", "value": 1499.0, "struck": True},
            "link": f"https://shop.example/oferta/dysk-twardy-seagate-ironwolf-pro-{i}-8tb",
            "image": {"src": f"https://shop.example/img/{i}.jpg", "alt": "a" * 100},
            # A long per-card text blob: the thing that must NOT reach the output
            # while the row has a title.
            "text": "x" * 200,
        })
    server.set_response("content.snapshot", resp({
        "ok": True, "mode": "records", "pageState": {"url": "u", "title": "t"},
        "detection": {"method": "auto", "itemSelector": "article.card"},
        "count": len(records), "totalItems": len(records), "withPrice": len(records),
        "ambiguousCount": 0, "records": records}))
    fn = await _fn(tools, "page_snapshot")
    out = await fn(mode="records")

    assert out.count("\n") < 200, "one line per record (plus a small header), nothing more"
    assert len(out) < 40_000, (
        f"records output ballooned to {len(out)} chars for 150 rows — something started "
        f"printing a per-row blob (text/prices/image). Keep rows to one line.")
    assert "x" * 50 not in out, "the per-card text blob must not be printed when a title exists"
    assert "https://shop.example/img/" not in out, "image URLs are not worth a line each"


@pytest.mark.asyncio
async def test_page_act_reports_a_control_that_was_re_resolved(tools, server):
    server.set_response("content.act", resp({
        "ok": True, "completedSteps": 1, "totalSteps": 1, "results": [
            {"ok": True, "step": 0, "stepAction": "click", "domChanged": True,
             "control": {"ref": "e9", "kind": "native", "checkedBefore": False,
                         "checked": True, "checkedChanged": True, "reresolved": "id"}}]}))
    fn = await _fn(tools, "page_act")
    out = await fn(steps=[{"action": "click", "target": "#cb"}])
    assert "control e9 is now checked (was unchecked)" in out


@pytest.mark.asyncio
async def test_page_act_click_whose_control_vanished_is_not_called_a_no_op(tools, server):
    # A control removed by the click's own re-render: the removal IS evidence the
    # click worked, so this must not read as a failure.
    server.set_response("content.act", resp({
        "ok": True, "completedSteps": 1, "totalSteps": 1, "results": [
            {"ok": True, "step": 0, "stepAction": "click", "domChanged": True,
             "controlDetached": True,
             "control": {"ref": "e9", "kind": "native", "checkedBefore": False,
                         "checkedChanged": False, "detached": True}}]}))
    fn = await _fn(tools, "page_act")
    out = await fn(steps=[{"action": "click", "target": "#cb"}])
    assert out.startswith("OK: 1/1 steps completed")
    assert "is now checked" not in out, "no claim is made about a vanished control"
    assert "did not change state" not in out


# =============================================================================
# Fused read: continuation hint and url= pass-through (review T3, T4)
# =============================================================================

def _long_page(server, tab_id=42):
    server.set_response("navigation.navigate", resp({
        "ok": True, "op": "goto", "tabId": tab_id, "url": "https://docs.example/long",
        "title": "Long", "settled": True, "waitedMs": 800}))
    server.responses["content.read"] = resp({
        "ok": True, "format": "text", "url": "https://docs.example/long", "title": "Long",
        "totalChars": 50000, "content": "x" * 100, "offset": 0})


@pytest.mark.asyncio
async def test_fused_read_hint_names_tab_format_and_scope(tools, server):
    """A follow-up built from the hint must hit the same tab, in the same format."""
    _long_page(server)
    fn = await _fn(tools, "navigate")
    out = await fn(url="https://docs.example/long", tab_id=42, read="text",
                   read_scope="main", read_max_chars=100)
    hint = [ln for ln in out.splitlines() if "continue with" in ln][0]
    assert "page_read(" in hint
    assert "tab_id=42" in hint
    assert 'format="text"' in hint
    assert 'scope="main"' in hint
    assert "offset=100" in hint


@pytest.mark.asyncio
async def test_page_read_url_passes_offset_and_frame_id(tools, server):
    _long_page(server)
    fn = await _fn(tools, "page_read")
    out = await fn(url="https://docs.example/long", tab_id=42, offset=20000,
                   frame_id=3, max_chars=100)
    reads = [r for r in server.sent if r["action"] == "content.read"]
    assert reads and reads[-1]["data"]["offset"] == 20000
    assert reads[-1]["data"]["frameId"] == 3
    assert "frame_id=3" in [ln for ln in out.splitlines() if "continue with" in ln][0]
