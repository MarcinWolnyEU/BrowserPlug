"""The texts an agent reads at the moment of a mistake, from the eval of 2026-10-08.

Each one fixes a failure that cost real runs, and each is a general rule, not a reply to
one page:

  * a call missing `url` is refused with a complete call to copy, in schema order, with
    `url` last (156 of Qwen 3.5 9B's 179 failed calls);
  * a page_eval script's own error is reported as one, and a top-level `return` gets its
    mechanical fix, also when an older Firefox build words it as an injection failure;
  * a failed screenshot of a page with no layout says why, and what works without focus;
  * a screenshot warning keeps activation as a last resort, with its cost;
  * every example call in the server instructions carries its required arguments.
"""

import pytest

import test_imports  # noqa: F401
from server.tools import tool_outcome
from server.tools.eval_hints import script_error_text, top_level_return_hint
from server.tools.formatting import screenshot_warning
from server.tools.instructions import instructions
from server.tools.mcp_tools import BrowserPlugTools
from test_page_tools import MockServer, resp


@pytest.fixture
def server():
    return MockServer()


@pytest.fixture
def tools(server):
    return BrowserPlugTools(server)


async def _fn(tools, name):
    return (await tools.mcp.get_tool(name)).fn


# --- url ------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_navigate_without_url_is_refused_before_the_wire_with_a_call_to_copy(tools, server):
    fn = await _fn(tools, "navigate")
    tool_outcome.arm()
    out = await fn(tab_id=262, wait="settled")
    assert (tool_outcome.read() or {}).get("code") == tool_outcome.INVALID_ARGUMENT
    assert server.sent == [], "refused before anything reaches the browser"
    assert out.startswith("Error navigating: url is required for goto")
    assert 'navigate(tab_id=262, url="https://…")' in out
    # The weak model's other reason for a url-less navigate: waiting for the tab it opened.
    assert "page_read(tab_id=262)" in out


@pytest.mark.asyncio
async def test_navigate_without_url_or_tab_never_suggests_the_users_active_tab(tools, server):
    out = await (await _fn(tools, "navigate"))(proxy="direct")
    assert "tab_id=<your tab's id>" in out
    assert server.sent == []


@pytest.mark.asyncio
async def test_back_forward_and_reload_still_need_no_url(tools, server):
    server.set_response("navigation.navigate", resp({"ok": True, "op": "reload", "tabId": 3,
                                                     "url": "http://x/", "settled": True}))
    out = await (await _fn(tools, "navigate"))(action="reload", tab_id=3)
    assert out.startswith("Reloaded tab 3")


@pytest.mark.asyncio
async def test_url_comes_last_and_says_when_it_is_required(tools):
    # A provider that decodes arguments in schema order can only emit url while nothing
    # after it has been written; models write action, tab_id and background first. With
    # url second in tabs, Qwen 3.5 9B on such a provider lost url in 90% of its opens;
    # with url last, in none (schema A/B loop of 2026-10-08, tool-economy.md).
    tool = await tools.mcp.get_tool("tabs")
    props = list(tool.parameters["properties"])
    assert props[0] == "action" and props[-1] == "url", props
    assert tool.parameters["properties"]["url"]["description"].startswith(
        "required for open and find")
    assert 'tabs(action="open", background=true, url="https://…")' in tool.description
    nav = await tools.mcp.get_tool("navigate")
    nav_props = list(nav.parameters["properties"])
    assert nav_props[0] == "tab_id" and nav_props[-1] == "url", nav_props
    assert nav.parameters["properties"]["url"]["description"].startswith("required for goto")


def test_every_tabs_open_example_in_the_instructions_carries_a_url():
    text = instructions()
    assert 'tabs(action="open", background=true, url="https://…")' in text
    assert 'tabs action="open", background=true' not in text


# --- page_eval ------------------------------------------------------------------------

_OLD_GECKO = ("Script execution failed: content script injection failed after 4 attempt(s): "
              "{inner}. The tab may have been navigating; retry; if it persists the tab may be "
              "a privileged page (page_screenshot / requests work there; page_read / page_eval "
              "do not).")


@pytest.mark.parametrize("inner", [
    "return not in function",
    "document.getElementById(...).options.map is not a function",
    "can't access property \"attributes\", el is null",
    "SyntaxError: missing ) after argument list",
])
def test_an_older_builds_injection_wording_comes_off_a_scripts_own_error(inner):
    assert script_error_text(_OLD_GECKO.format(inner=inner)) == \
        f"Script execution failed: {inner}"


@pytest.mark.parametrize("inner", [
    "Invalid frame IDs: [781684047875].",
    "No window matching {\"matchesHost\":[]}",
    "Missing host permission for the tab",
])
def test_a_real_injection_failure_keeps_its_text(inner):
    old = _OLD_GECKO.format(inner=inner)
    assert script_error_text(old) == old


def test_a_top_level_return_gets_its_fix_in_both_engines_wordings():
    for err in ("SyntaxError: return not in function", "Uncaught SyntaxError: Illegal return statement"):
        hint = top_level_return_hint(err)
        assert hint.startswith(". page_eval returns the value of the code's last expression")
        assert "(() => { ...; return value; })()" in hint
        assert "after the closing parenthesis" in hint
    assert top_level_return_hint("TypeError: x is null") == ""


@pytest.mark.asyncio
async def test_page_eval_reports_a_script_error_as_one(tools, server):
    server.set_response("content.execute_script", {"type": "error", "data": {
        "code": "INJECT_FAILED", "message": _OLD_GECKO.format(inner="return not in function")}})
    out = await (await _fn(tools, "page_eval"))(code="return document.title", tab_id=4)
    assert out.startswith("Error executing script: Script execution failed: return not in "
                          "function. page_eval returns the value")
    assert "navigating" not in out and "injection failed" not in out


@pytest.mark.asyncio
async def test_page_eval_shows_the_arrow_call_shape_for_a_parenthesised_arrow(tools, server):
    server.set_response("content.execute_script", {"type": "error", "data": {
        "code": "SCRIPT_ERROR", "message": "SyntaxError: missing ) in parenthetical"}})
    out = await (await _fn(tools, "page_eval"))(
        code="(() => { return document.title }())", tab_id=4)
    assert out.startswith("Error executing script: SyntaxError: missing ) in parenthetical. ")
    assert "write (() => { ... })() with the call after the closing parenthesis" in out


# --- page_screenshot ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_failed_capture_of_an_unlaid_out_page_names_the_cause(tools, server):
    server.set_response("tabs.screenshot", {"type": "error", "data": {
        "message": "Failed to capture screenshot: An unexpected error occurred"}})
    server.set_response("content.page_state", resp({"ok": True, "pageState": {
        "painted": False, "hiddenByCss": False}}))
    tool_outcome.arm()
    out = await (await _fn(tools, "page_screenshot"))(tab_id=229)
    assert (tool_outcome.read() or {}).get("failed")
    assert "An unexpected error occurred. Cause: the page is not laid out" in out
    assert "page_snapshot and page_read read this page without focus" in out
    assert 'Last resort for a picture: tabs(action="activate", tab_id=229)' in out
    assert [r["action"] for r in server.sent] == ["tabs.screenshot", "content.page_state"]


@pytest.mark.asyncio
async def test_a_failed_capture_of_a_painted_page_adds_nothing(tools, server):
    server.set_response("tabs.screenshot", {"type": "error", "data": {
        "message": "Failed to capture screenshot: An unexpected error occurred"}})
    server.set_response("content.page_state", resp({"ok": True, "pageState": {"painted": True}}))
    out = await (await _fn(tools, "page_screenshot"))(tab_id=229)
    assert out == "Error capturing screenshot: Failed to capture screenshot: An unexpected error occurred"


@pytest.mark.asyncio
async def test_a_capture_error_that_names_its_cause_is_not_probed(tools, server):
    server.set_response("tabs.screenshot", {"type": "error", "data": {
        "message": "Failed to capture screenshot: element screenshot: matched 2 elements"}})
    await (await _fn(tools, "page_screenshot"))(tab_id=1, selector="iframe")
    assert [r["action"] for r in server.sent] == ["tabs.screenshot"]


def test_screenshot_warnings_keep_activation_as_the_last_resort():
    old_unpainted = ('Page is not painted (its window is unfocused/minimized); the screenshot is '
                     'likely blank. Remedy: tabs action="activate" to focus the window, then retry.')
    for text in (old_unpainted, "Page is not laid out (its window is in the background), so ..."):
        out = screenshot_warning(text, 7)
        assert "Remedy" not in out
        assert out.index("page_snapshot and page_read") < out.index('tabs(action="activate", tab_id=7)')
    uniform = ("The captured image is a single uniform color (#ffffff); likely a blank frame, not "
               "real page content. If the tab is in an unfocused or minimized window, tabs "
               'action="activate" it and retry.')
    out = screenshot_warning(uniform, 7)
    assert out.startswith("The captured image is a single uniform color (#ffffff)")
    assert "front of the user's window" in out and "it and retry" not in out
    assert screenshot_warning("capture may not have fully settled") == \
        "capture may not have fully settled"


# --- frame ids ------------------------------------------------------------------------

def _invalid_frame(frame_id):
    return {"type": "error", "data": {"message": f"page read failed: Invalid frame IDs: [{frame_id}]."}}


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,action,kwargs", [
    ("page_read", "content.read", {}),
    ("page_snapshot", "content.snapshot", {}),
    ("page_eval", "content.execute_script", {"code": "1"}),
    ("page_act", "content.act", {"steps": [{"action": "click", "target": "#x"}]}),
])
async def test_a_frame_id_without_its_tab_says_the_call_ran_on_the_active_tab(tools, server, tool, action, kwargs):
    server.set_response(action, _invalid_frame(4294967297))
    out = await (await _fn(tools, tool))(frame_id=4294967297, **kwargs)
    assert "Invalid frame IDs" in out
    assert "this call named none, so it ran on the user's active tab: pass tab_id with frame_id" in out
    assert "page_snapshot(tab_id=<your tab's id>)" in out


@pytest.mark.asyncio
async def test_the_tab_id_passed_as_frame_id_is_named(tools, server):
    server.set_response("content.read", _invalid_frame(347))
    out = await (await _fn(tools, "page_read"))(tab_id=347, frame_id=347)
    assert "frame_id 347 is the tab id" in out
    assert "page_snapshot(tab_id=347) lists under Frames" in out


@pytest.mark.asyncio
async def test_a_stale_frame_id_points_at_a_fresh_snapshot(tools, server):
    server.set_response("content.snapshot", _invalid_frame(9))
    out = await (await _fn(tools, "page_snapshot"))(tab_id=7, frame_id=9)
    assert "take a current one from page_snapshot(tab_id=7)" in out


@pytest.mark.asyncio
async def test_other_errors_and_calls_without_a_frame_get_no_frame_hint(tools, server):
    server.set_response("content.read", {"type": "error", "data": {"message": "page read failed: boom"}})
    out = await (await _fn(tools, "page_read"))(tab_id=7, frame_id=9)
    assert "frame_id" not in out
    server.set_response("content.read", _invalid_frame(9))
    out = await (await _fn(tools, "page_read"))(tab_id=7)
    assert "Frames" not in out
