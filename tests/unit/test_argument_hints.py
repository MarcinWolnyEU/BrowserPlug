"""The lines a refused call gets after pydantic's report (server/tools/argument_hints.py).

Two refusals that small models repeat unchanged: an argument the tool does not take (most
often one another tool takes, such as frame_id on page_screenshot or navigate's read_scope
on page_read), and an array sent as a JSON string. Each hint is tested where it must fire
and where it must stay silent, and the dispatcher test drives the real tools through an MCP
client, because the helper tests alone pass with the hook deleted.
"""

import pytest
from mcp import Client

import test_imports  # noqa: F401
from facade_double import FacadeDouble
from server.tools.argument_hints import refusal_hints
from server.tools.mcp_tools import BrowserPlugTools

PARAMS = {
    "page_read": ["tab_id", "url", "format", "scope", "max_chars", "offset", "frame_id"],
    "page_snapshot": ["tab_id", "mode", "root", "frame_id"],
    "page_screenshot": ["tab_id", "full_page", "selector"],
    "navigate": ["url", "tab_id", "read", "read_scope"],
    "page_act": ["steps", "tab_id", "frame_id"],
}


def _unknown(name, value=1):
    return {"type": "unexpected_keyword_argument", "loc": (name,), "input": value}


def test_an_argument_another_tool_takes_names_that_tool_and_this_ones_parameters():
    out = refusal_hints("page_screenshot", [_unknown("frame_id")], PARAMS)
    assert "page_screenshot has no frame_id argument." in out
    assert "frame_id belongs to page_read, page_snapshot and page_act." in out
    assert out.endswith("page_screenshot takes: tab_id, full_page, selector.")


def test_a_prefixed_name_points_at_the_parameter_it_contains():
    out = refusal_hints("page_read", [_unknown("read_scope", "main")], PARAMS)
    assert "Did you mean scope?" in out
    assert "read_scope belongs to navigate." in out


def test_an_unknown_name_no_tool_takes_still_lists_the_parameters():
    out = refusal_hints("page_snapshot", [_unknown("force_include_hidden", "true")], PARAMS)
    assert "page_snapshot has no force_include_hidden argument." in out
    assert "belongs to" not in out
    assert "page_snapshot takes: tab_id, mode, root, frame_id." in out


def test_an_array_sent_as_a_string_gets_the_shape_to_send():
    err = {"type": "list_type", "loc": ("steps",), "input": '[{"action": "click"}]'}
    out = refusal_hints("page_act", [err], PARAMS)
    assert out == ('\nsteps must be a JSON array, not a string that holds one. Send it as the '
                   'value itself: steps=[{"action": "click", "target": "Save"}]')


def test_an_object_sent_as_a_string_gets_the_shape_to_send():
    err = {"type": "dict_type", "loc": ("options",), "input": "{}"}
    assert "options must be a JSON object" in refusal_hints("requests", [err], PARAMS)


@pytest.mark.parametrize("err", [
    {"type": "int_parsing", "loc": ("tab_id",), "input": "abc"},
    {"type": "missing_argument", "loc": ("steps",), "input": {}},
    {"type": "list_type", "loc": ("steps",), "input": 5},
    {"type": "missing", "loc": ("steps", 0, "action"), "input": {"target": "#x"}},
    {"type": "unexpected_keyword_argument", "loc": ("steps", 0, "bogus"), "input": 1},
])
def test_other_refusals_get_nothing_added(err):
    assert refusal_hints("page_act", [err], PARAMS) == ""


class _Server(FacadeDouble):
    async def send_request_and_wait(self, request, timeout=30.0, browser=None):
        raise AssertionError("an argument refusal must not reach the browser")


@pytest.mark.asyncio
async def test_the_dispatcher_appends_the_hint_after_pydantics_report():
    tools = BrowserPlugTools(_Server())
    async with Client(tools.mcp, mode="legacy") as client:
        shot = await client.call_tool("page_screenshot", {"tab_id": 7, "frame_id": 5})
        act = await client.call_tool("page_act", {"tab_id": 7, "steps": '[{"action": "click"}]'})
    text = shot.content[0].text
    assert shot.is_error is True
    assert text.startswith("1 validation error for call[page_screenshot]\nframe_id\n")
    assert "\npage_screenshot has no frame_id argument. frame_id belongs to " in text
    assert "page_read" in text.split("belongs to ", 1)[1].split("\n", 1)[0]
    assert "page_screenshot takes: tab_id, " in text
    assert act.is_error is True
    assert act.content[0].text.startswith("1 validation error for call[page_act]\nsteps\n")
    assert "steps must be a JSON array, not a string that holds one." in act.content[0].text


def test_a_step_field_sent_beside_steps_is_put_back_inside_each_step():
    out = refusal_hints("page_act", [_unknown("watch", ["#a"])], PARAMS,
                        {"page_act": {"steps": ["action", "target", "watch"]}})
    assert out == ('\npage_act has no watch argument. watch is a field of each item in steps: '
                   'steps=[{..., "watch": ...}].\npage_act takes: steps, tab_id, frame_id.')
