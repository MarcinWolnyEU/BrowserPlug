"""Tool-boundary validation (review finding T8): bad numbers and enums are refused with a
structured error BEFORE any wire action, and the server deadline never undercuts the
extension's. Uses a recording double, no browser."""

import pytest

import test_imports  # noqa: F401
from facade_double import FacadeDouble
from server.tools import tool_outcome
from server.tools.mcp_tools import BrowserPlugTools
from server.tools import validation
from server.tools.act import plan_act


class Rec(FacadeDouble):
    def __init__(self):
        super().__init__()
        self.sent = []
        self.timeouts = []
        self.responses = {}

    async def send_request_and_wait(self, request, timeout=30.0, browser=None):
        self.sent.append(request)
        self.timeouts.append(timeout)
        return self.responses.get(request["action"]) or {
            "type": "response", "data": {"ok": True, "completedSteps": 1, "totalSteps": 1,
                                         "results": [], "tabId": 1, "url": "https://example.org/",
                                         "settled": True, "content": "x", "totalChars": 1,
                                         "format": "markdown", "offset": 0, "requests": [],
                                         "total": 0, "returned": 0, "result": 1}}


@pytest.fixture
def rec():
    return Rec()


async def call(rec, name, **kw):
    tools = BrowserPlugTools(rec)
    fn = (await tools.mcp.get_tool(name)).fn
    tool_outcome.arm()
    out = await fn(**kw)
    return out, tool_outcome.read()


def refused(out, oc, rec):
    assert out.startswith("Error:"), out
    assert rec.sent == [], rec.sent
    assert oc and oc.get("code") == "INVALID_ARGUMENT", oc


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [-60000, 0])
async def test_act_wait_timeout_must_be_positive(rec, bad):
    out, oc = await call(rec, "page_act", tab_id=1, steps=[
        {"action": "click", "target": "Buy"}, {"action": "wait", "timeout_ms": bad}])
    refused(out, oc, rec)
    assert "timeout_ms" in out


@pytest.mark.asyncio
async def test_act_negative_settle_refused(rec):
    out, oc = await call(rec, "page_act", tab_id=1, steps=[
        {"action": "click", "target": "Buy", "settle_ms": -5}])
    refused(out, oc, rec)


@pytest.mark.asyncio
async def test_act_empty_steps_refused(rec):
    out, oc = await call(rec, "page_act", tab_id=1, steps=[])
    refused(out, oc, rec)
    assert "steps" in out


def test_act_deadline_never_negative_and_clamped_like_extension():
    _, timeout, data = plan_act(
        [{"action": "wait", "timeout_ms": 10_000_000}], 1, None)
    assert data["steps"][0]["timeout_ms"] == validation.MAX_TIMEOUT_MS
    assert timeout >= validation.MAX_TIMEOUT_MS / 1000


@pytest.mark.asyncio
async def test_navigate_zero_timeout_refused(rec):
    out, oc = await call(rec, "navigate", url="https://example.org/", timeout_ms=0)
    refused(out, oc, rec)


@pytest.mark.asyncio
async def test_navigate_over_max_timeout_clamped_and_server_waits_longer(rec):
    await call(rec, "navigate", url="https://example.org/", timeout_ms=10_000_000)
    assert rec.sent[0]["data"]["timeoutMs"] == validation.MAX_TIMEOUT_MS
    assert rec.timeouts[0] > validation.MAX_TIMEOUT_MS / 1000


@pytest.mark.asyncio
async def test_navigate_enum_case_and_unknown(rec):
    await call(rec, "navigate", url="https://example.org/", action="GoTo", wait="Load")
    assert rec.sent[0]["data"]["op"] == "goto" and rec.sent[0]["data"]["wait"] == "load"
    rec.sent.clear()
    out, oc = await call(rec, "navigate", url="https://example.org/", wait="forever")
    refused(out, oc, rec)
    assert "settled | load | dom | none" in out


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [0, -1])
async def test_navigate_read_max_chars_refused(rec, bad):
    out, oc = await call(rec, "navigate", url="https://example.org/", read="text",
                         read_max_chars=bad)
    refused(out, oc, rec)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [0, -5])
async def test_page_read_max_chars_refused(rec, bad):
    out, oc = await call(rec, "page_read", tab_id=1, max_chars=bad)
    refused(out, oc, rec)
    assert "max_chars" in out


@pytest.mark.asyncio
async def test_page_read_negative_offset_refused(rec):
    out, oc = await call(rec, "page_read", tab_id=1, offset=-1)
    refused(out, oc, rec)


@pytest.mark.asyncio
async def test_page_read_format_case_normalised_and_unknown_rejected(rec):
    await call(rec, "page_read", tab_id=1, format="Markdown")
    assert rec.sent[-1]["data"]["format"] == "markdown"
    rec.sent.clear()
    out, oc = await call(rec, "page_read", tab_id=1, format="pdf")
    refused(out, oc, rec)
    assert "markdown | text | html" in out


@pytest.mark.asyncio
async def test_page_eval_world_case_normalised_and_unknown_rejected(rec):
    await call(rec, "page_eval", code="1", tab_id=1, world="Main")
    assert rec.sent[-1]["data"]["world"] == "main"
    rec.sent.clear()
    out, oc = await call(rec, "page_eval", code="1", tab_id=1, world="page")
    refused(out, oc, rec)
    assert "isolated | main" in out


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [0, -1])
async def test_requests_limit_refused(rec, bad):
    out, oc = await call(rec, "requests", limit=bad)
    refused(out, oc, rec)


@pytest.mark.asyncio
async def test_requests_limit_clamped_and_action_case(rec):
    await call(rec, "requests", action="Recent", limit=5000)
    assert rec.sent[0]["data"]["limit"] == validation.MAX_REQUEST_LIMIT
