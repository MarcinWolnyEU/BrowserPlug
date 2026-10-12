"""Unit tests for server/tools/tool_outcome.py — failure signalling.

The evidence: 648 of 2,903 calls failed and 19 carried the MCP `isError` flag, a
34x under-report, because BrowserPlug signals failure by RETURNING a string. This module
fixes that WITHOUT touching what a tool returns, so the tests below are organised
around the two properties that makes possible and the two failure modes that would
silently undo it:

  * `fail()` returns its argument unchanged (pinned by IDENTITY). ~56 return sites are
    wrapped in it, and ~42 existing test functions assert on the resulting text; if this
    regresses, this file says so before those 42 do.
  * classification is driven by the messages the CORPUS actually produced, not by
    strings invented here.
  * a record must never leak between concurrent calls, or one call's failure is
    attributed to another. Driven with asyncio.gather, both at module level and through
    the real server.
  * a degradation must ACCUMULATE — one page_read can be both truncated and a
    markdown->text fallback, and collapsing that loses the detail a client acts on.

The last section drives the real MCP server (server/tools/mcp_server.py) through the SDK's
MCP client, which is the only layer where `isError` and `_meta` actually exist. Every
other unit test in this repo calls `tool.fn` directly and therefore cannot see either.
"""

import asyncio

import pydantic
import pytest
from mcp import Client
from mcp_types import CallToolResult, ImageContent, TextContent

import test_imports  # noqa: F401  (path setup)
from server.tools.mcp_server import BrowserPlugMCPServer
from server.tools.tool_outcome import (EXTENSION_DISCONNECTED, EXTENSION_SEND_FAILED,
                                       EXTENSION_VERSION_SKEW, INVALID_ARGUMENT, META_KEY,
                                       PAGE_ERROR, RENAMED_TOOLS, TIMEOUT, ToolOutcomePolicy,
                                       arm, classify, degrade, fail, health_outcome, invalid,
                                       read, renamed_tool_message)
from server.tools.tool_registry import ToolRegistry, ToolSpec

# 1x1 PNG — the smallest thing that survives an ImageContent round trip.
PNG_B64 = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDw"
           "AChwGA60e6kgAAAABJRU5ErkJggg==")


@pytest.fixture(autouse=True)
def clean_record():
    """Every test starts from the state the policy guarantees (nothing recorded).
    Tests share a context, so without this a leaked outcome would make the next test
    pass for the wrong reason."""
    arm()
    yield
    arm()


# =============================================================================
# fail() returns its argument unchanged — the property the whole design rests on
# =============================================================================

def test_fail_returns_the_very_same_string_object():
    message = "Error navigating: Request timed out after 30.0 seconds"
    # Identity, not equality: a wrapper that rebuilt the string (adding a code prefix,
    # a marker, a newline) would still pass ==, and that is exactly the regression that
    # would break ~42 downstream assertions.
    assert fail(message) is message


def test_invalid_returns_the_very_same_string_object():
    message = 'Error: url is required for action="open"'
    assert invalid(message) is message


def test_fail_preserves_a_multiline_report_byte_for_byte():
    # page_act's step-timeout report is the hardest case: multi-line, and the whole
    # thing becomes the ToolError message, so not one byte may move.
    report = ("FAILED: 0/1 steps completed\n"
              "  step 1 click e7: timed out after 12.0s\n"
              "  the click NAVIGATED - CHECK WHERE YOU LANDED: https://example.org/next")
    assert fail(report, code=PAGE_ERROR, tool="page_act") is report


def test_fail_records_the_failure_with_message_and_derived_code():
    fail("Error listing tabs: Request timed out after 30 seconds")
    outcome = read()
    assert outcome["failed"] is True
    assert outcome["code"] == TIMEOUT
    # Changed on purpose: retryable is no longer sniffed from the prose (see
    # test_retry_truth.py); with no wire verdict attached it is False.
    assert outcome["retryable"] is False
    assert outcome["message"] == "Error listing tabs: Request timed out after 30 seconds"


def test_invalid_records_INVALID_ARGUMENT_without_sniffing_the_prose():
    # A local-validation refusal never reached the browser, so the code is fixed even
    # though this particular message happens to mention a timeout.
    invalid("Error: timeout_ms must be a positive integer")
    assert read()["code"] == INVALID_ARGUMENT
    assert read()["retryable"] is False


def test_an_explicit_code_overrides_classification():
    # The three sites whose text does not start with "Error" carry their own code;
    # classify() would have called this one PAGE_ERROR.
    fail("No screenshot data received (method=captureVisibleTab)",
         code="SCREENSHOT_NO_DATA")
    assert read()["code"] == "SCREENSHOT_NO_DATA"


def test_extra_fields_ride_along_on_the_record():
    fail("FAILED: 0/1 steps completed", code=PAGE_ERROR, tool="page_act", completed=0)
    outcome = read()
    assert outcome["tool"] == "page_act"
    assert outcome["completed"] == 0


def test_nothing_is_recorded_until_something_records():
    assert read() is None


def test_arm_clears_a_previous_verdict():
    fail("Error navigating: No extension connection available")
    arm()
    # Without this the next call in the same context inherits a failure it never had.
    assert read() is None


@pytest.mark.parametrize("code", [TIMEOUT, EXTENSION_SEND_FAILED, EXTENSION_DISCONNECTED,
                                  PAGE_ERROR, INVALID_ARGUMENT])
def test_a_code_alone_never_makes_a_failure_retryable(code):
    # Changed on purpose: it used to be a property of the code. Now the site that knows
    # the action passes retryable=True (or the wire verdict carries it).
    fail("whatever", code=code)
    assert read()["retryable"] is False
    fail("whatever", code=code, retryable=True)
    assert read()["retryable"] is True


# =============================================================================
# classify() — driven by the corpus signatures
# =============================================================================

@pytest.mark.parametrize("message,expected", [
    # -- the two that are 546 of the corpus's 648 failures ---------------------------
    ("Error navigating: Request timed out after 30.0 seconds", TIMEOUT),
    ("Error reading page: Request timed out after 22.1 seconds", TIMEOUT),
    ("Error listing tabs: Request timed out after 30 seconds", TIMEOUT),
    ("Error executing steps: Request timed out after 22.1 seconds", TIMEOUT),
    ("Error taking snapshot: Request timed out after 30.0 seconds", TIMEOUT),
    ("Error capturing screenshot: Request timed out after 30.0 seconds", TIMEOUT),
    ("Error navigating: No extension connection available", EXTENSION_DISCONNECTED),
    ("Failed to send request to extension", EXTENSION_SEND_FAILED),
    # -- genuine page-level failures: nothing to retry, nothing to reconnect ---------
    ("page read failed: Missing host permission for the tab", PAGE_ERROR),
    ("Component returned failure code: 0x8007000e (NS_ERROR_OUT_OF_MEMORY)", PAGE_ERROR),
    ("Tab 5 is a privileged page (about:blank)", PAGE_ERROR),
    ("Navigation API error: Illegal URL: file:///C:/tmp/page.html", PAGE_ERROR),
    ("Tab 12 no longer exists. It was probably closed.", PAGE_ERROR),
    ("FAILED: 0/1 steps completed", PAGE_ERROR),
    ("", PAGE_ERROR),
])
def test_classify_maps_corpus_signatures(message, expected):
    assert classify(message) == expected


@pytest.mark.parametrize("token", [
    "FILE_ACCESS_DISABLED", "PROXY_PERMISSION_REQUIRED", "PROXY_UNSUPPORTED",
    "EVAL_UNAVAILABLE", "WINDOW_NOT_PAINTED",
])
def test_classify_passes_an_extension_code_through_verbatim(token):
    # The operator should see the exact word the extension emitted, not a server-side
    # re-interpretation of it.
    assert classify(f"page read failed [{token}]: about:addons > Permissions") == token


def test_an_extension_code_outranks_a_timeout_mention():
    # An eval blocked by the Chrome user-scripts toggle is an eval-gate problem even if
    # the surrounding prose also mentions a timeout; classifying it TIMEOUT would send
    # an operator chasing the network.
    message = ("content.execute failed: EVAL_UNAVAILABLE - enable 'Allow user scripts'. "
               "The previous attempt timed out after 30.0 seconds.")
    assert classify(message) == "EVAL_UNAVAILABLE"


def test_classify_recognises_a_version_skew_message():
    assert classify("extension version skew: browser 1.7.4, server 1.7.6") == \
        EXTENSION_VERSION_SKEW


# =============================================================================
# degrade() — additive, and orthogonal to failure
# =============================================================================

def test_degrade_accumulates_in_call_order():
    # The real page_read case: truncated AND downgraded from markdown to text.
    degrade("truncated", tool="page_read", returned=500, total=9000, next_offset=500)
    degrade("format_fallback", tool="page_read", **{"from": "markdown", "to": "text"})
    marks = read()["degraded"]
    assert [m["kind"] for m in marks] == ["truncated", "format_fallback"]
    assert marks[0]["next_offset"] == 500
    assert marks[1]["to"] == "text"


def test_degrade_does_not_mark_the_call_as_failed():
    degrade("empty_extraction", tool="page_snapshot", count=0)
    assert read().get("failed") is None


def test_a_degradation_recorded_before_a_failure_survives_on_the_record():
    # Order is not guaranteed at a call site; fail() merges rather than replaces, so the
    # operator record still shows what was degraded before it fell over.
    degrade("truncated", tool="page_read", returned=500, total=9000)
    fail("Error reading page: Request timed out after 30.0 seconds")
    outcome = read()
    assert outcome["failed"] is True
    assert [m["kind"] for m in outcome["degraded"]] == ["truncated"]


# =============================================================================
# health_outcome() — the operator projection (/health last_command.outcome)
# =============================================================================

def test_health_outcome_is_none_for_a_clean_call():
    assert health_outcome(None) is None
    assert health_outcome({}) is None


def test_health_outcome_drops_the_message_and_keeps_the_verdict():
    fail("Error navigating: Request timed out after 30.0 seconds")
    summary = health_outcome(read())
    # The client already has the message verbatim as the error text; what prose cannot
    # answer is the code and whether a retry is worth attempting.
    assert summary == {"failed": True, "code": TIMEOUT, "retryable": False, "degraded": []}


def test_health_outcome_reports_a_degraded_success_as_not_failed():
    degrade("unsettled", tool="navigate", waited_ms=8000, status="loading")
    assert health_outcome(read()) == {
        "failed": False, "code": None, "retryable": False,
        "degraded": [{"kind": "unsettled", "tool": "navigate",
                      "waited_ms": 8000, "status": "loading"}]}


# =============================================================================
# renamed_tool_message() — the 9 stale names the corpus actually called
# =============================================================================

_TAIL = ("Your tool list is stale - re-read it (tools/list); this server "
         "exposes 9 tools (10 with 2+ browsers connected).")

# Asserted character-for-character (the test_version_skew.py convention): this text is
# the contract with an agent that just discovered its tool list is out of date.
EXPECTED_RENAME_MESSAGES = {
    "page_wait_for": "Unknown tool 'page_wait_for': it was renamed. Use 'page_act' with "
                     'a step {"action": "wait", "target": "<css>"}. ' + _TAIL,
    "tabs_list": "Unknown tool 'tabs_list': it was renamed. Use 'tabs' with "
                 'action="list". ' + _TAIL,
    "tabs_activate": "Unknown tool 'tabs_activate': it was renamed. Use 'tabs' with "
                     'action="activate", tab_id=<id>. ' + _TAIL,
    "page_navigate": "Unknown tool 'page_navigate': it was renamed. Use 'navigate' with "
                     'url="<url>". ' + _TAIL,
    "page_execute_script": "Unknown tool 'page_execute_script': it was renamed. Use "
                           '\'page_eval\' with code="<js>". ' + _TAIL,
    "page_get_text": "Unknown tool 'page_get_text': it was renamed. Use 'page_read' with "
                     'format="text". ' + _TAIL,
    "page_reload": "Unknown tool 'page_reload': it was renamed. Use 'navigate' with "
                   'action="reload". ' + _TAIL,
    "server_status": "Unknown tool 'server_status': it was renamed. Use 'server_info'. "
                     + _TAIL,
}


def test_all_corpus_stale_names_are_mapped():
    assert set(RENAMED_TOOLS) == set(EXPECTED_RENAME_MESSAGES)


@pytest.mark.parametrize("stale", sorted(EXPECTED_RENAME_MESSAGES))
def test_renamed_tool_message_is_exact(stale):
    assert renamed_tool_message(stale) == EXPECTED_RENAME_MESSAGES[stale]


@pytest.mark.parametrize("stale", sorted(EXPECTED_RENAME_MESSAGES))
def test_renamed_tool_message_is_pure_ascii(stale):
    # Produced by the launcher too, which prints to whatever console spawned it (cp1252
    # here). A smart quote or an arrow would raise UnicodeEncodeError at the worst
    # possible moment — while reporting that something else already went wrong.
    renamed_tool_message(stale).encode("ascii")


@pytest.mark.parametrize("stale", sorted(EXPECTED_RENAME_MESSAGES))
def test_renamed_tool_message_names_the_replacement_and_the_next_action(stale):
    message = renamed_tool_message(stale)
    replacement = RENAMED_TOOLS[stale][0]
    assert f"Use '{replacement}'" in message
    assert "tools/list" in message, "must say how to get un-stale, not just that it is"


@pytest.mark.parametrize("unknown", ["totally_made_up", "", "page_wait", "PAGE_WAIT_FOR"])
def test_an_unmapped_name_is_not_claimed_as_renamed(unknown):
    # None means "fall through to the plain Unknown tool text", which is already flagged.
    # Claiming a genuinely unknown tool was renamed would be a confident lie.
    assert renamed_tool_message(unknown) is None


# =============================================================================
# Concurrency — one call's verdict must never reach another
# =============================================================================

async def _record_then_read(tag, mode):
    arm()
    await asyncio.sleep(0)               # force a real interleave at the scheduler
    if mode == "fail":
        fail(f"Error navigating: failure {tag}")
    elif mode == "degrade":
        degrade("truncated", tool="page_read", returned=tag)
    await asyncio.sleep(0)
    return read()


async def test_interleaved_tasks_never_see_each_others_outcome():
    modes = ["fail", "degrade", "clean"] * 5
    outcomes = await asyncio.gather(
        *(_record_then_read(i, modes[i]) for i in range(len(modes))))
    for i, (mode, outcome) in enumerate(zip(modes, outcomes)):
        if mode == "fail":
            assert outcome["message"] == f"Error navigating: failure {i}"
            assert not outcome.get("degraded")
        elif mode == "degrade":
            assert outcome["degraded"] == [{"kind": "truncated", "tool": "page_read",
                                            "returned": i}]
            assert not outcome.get("failed")
        else:
            assert outcome is None, "a clean call must record nothing at all"


async def test_a_record_made_inside_a_spawned_task_is_visible_to_the_caller():
    # A ContextVar REPLACEMENT propagates into a child context but never back out, so a
    # tool that recorded from a spawned task (or, for a synchronous tool, from the worker
    # THREAD it runs in) would be seen as a clean success — an invisible failure,
    # the exact defect this module removes. The record is mutated in place precisely so this
    # cannot happen. Observed for real: sync tools silently recorded nothing before.
    arm()

    async def child():
        fail("Error navigating: Request timed out after 30.0 seconds")

    await asyncio.gather(child())
    assert read()["failed"] is True


def test_a_record_made_on_a_worker_thread_is_visible_to_the_caller():
    import contextvars
    import threading

    arm()
    context = contextvars.copy_context()      # exactly what to_thread.run_sync does
    thread = threading.Thread(
        target=context.run, args=(fail, "Error reading page: Missing host permission"))
    thread.start()
    thread.join()
    assert read()["failed"] is True


# =============================================================================
# The outcome policy, driven directly with a fake `run`
# =============================================================================

def _text_result(text):
    return CallToolResult(content=[TextContent(type="text", text=text)])


async def test_outcome_policy_returns_the_recorded_message_verbatim_as_an_error():
    policy = ToolOutcomePolicy()

    async def run():
        return _text_result(fail("Error navigating: Request timed out after 30.0 seconds"))

    out = await policy.apply("navigate", run)
    assert out.is_error is True
    assert [c.text for c in out.content] == [
        "Error navigating: Request timed out after 30.0 seconds"]
    assert "Error calling tool" not in out.content[0].text, "no prefix may ever engage"
    assert out.meta is None, "an error result carries no _meta"


async def test_outcome_policy_arms_before_the_call_so_a_stale_verdict_cannot_leak():
    fail("Error from the PREVIOUS call")            # same context, as in a test harness
    policy = ToolOutcomePolicy()
    result = _text_result("2 tabs")

    async def run():
        return result

    assert await policy.apply("tabs", run) is result


async def test_outcome_policy_attaches_degradations_to_meta():
    policy = ToolOutcomePolicy()

    async def run():
        degrade("truncated", tool="page_read", returned=500, total=9000, next_offset=500)
        degrade("format_fallback", tool="page_read", **{"from": "markdown", "to": "text"})
        return CallToolResult(content=[TextContent(type="text", text="chars 0-500 of 9000")],
                              _meta={"other/key": {"kept": True}})

    out = await policy.apply("page_read", run)
    assert out.meta[META_KEY] == [
        {"kind": "truncated", "tool": "page_read", "returned": 500, "total": 9000,
         "next_offset": 500},
        {"kind": "format_fallback", "tool": "page_read", "from": "markdown", "to": "text"},
    ]
    assert out.meta["other/key"] == {"kept": True}, "meta already on the result must survive"
    assert out.is_error is False


async def test_degradation_meta_preserves_a_screenshot_image_block():
    # page_screenshot returns TextContent + ImageContent; a rebuild that re-ran content
    # conversion could drop or re-encode the image, which is the payload the agent came
    # for. Also covers the meta=None case (a bare result carries none).
    policy = ToolOutcomePolicy()
    image = ImageContent(type="image", data=PNG_B64, mime_type="image/png")

    async def run():
        degrade("page_not_painted", tool="page_screenshot",
                warning="window not focused; page measured 0x0")
        return CallToolResult(content=[TextContent(type="text", text="Screenshot"), image])

    out = await policy.apply("page_screenshot", run)
    assert out.meta[META_KEY][0]["kind"] == "page_not_painted"
    assert out.content[1].data == PNG_B64


async def test_outcome_policy_lets_what_run_raises_propagate_unrecorded():
    # Argument errors and tool bugs are answered by the dispatcher with texts of its own;
    # the policy must neither swallow them nor report them as the call's verdict.
    seen = []
    policy = ToolOutcomePolicy(record=lambda *a: seen.append(a))

    async def run():
        raise LookupError("tool bug")

    with pytest.raises(LookupError):
        await policy.apply("tabs", run)
    assert seen == []


async def test_the_record_sink_sees_every_call_including_the_clean_ones():
    seen = []
    policy = ToolOutcomePolicy(record=lambda name, outcome: seen.append((name, outcome)))

    async def clean():
        return _text_result("ok")

    async def failing():
        return _text_result(fail("Error navigating: No extension connection available"))

    await policy.apply("tabs", clean)
    assert (await policy.apply("navigate", failing)).is_error is True

    # A clean call must report None, or /health would keep showing the previous verdict.
    assert seen[0] == ("tabs", None)
    assert seen[1] == ("navigate", {"failed": True, "code": EXTENSION_DISCONNECTED,
                                    "retryable": False, "degraded": []})


async def test_the_record_sink_gets_the_call_identity_when_there_is_one():
    seen = []
    policy = ToolOutcomePolicy(record=lambda *a: seen.append(a))

    async def clean():
        return _text_result("ok")

    await policy.apply("tabs", clean, call=("sess-1", 9))
    await policy.apply("tabs", clean)
    assert seen == [("tabs", None, ("sess-1", 9)), ("tabs", None)]


async def test_a_broken_record_sink_never_breaks_the_tool_call():
    def explode(name, outcome):
        raise RuntimeError("last_command is not a dict today")

    policy = ToolOutcomePolicy(record=explode)
    result = _text_result("ok")

    async def run():
        return result

    assert await policy.apply("tabs", run) is result


# =============================================================================
# End to end through the real MCP server and an MCP client: where isError exists
# =============================================================================

def _app(record=None):
    """The real MCP server (BrowserPlugMCPServer) in the shape of the real surface: a tool
    that fails on the wire, one that succeeds but degrades, and a clean one. All
    `async def`, like every real tool.

    `sync_failing_tool` is the odd one out on purpose: a synchronous tool runs on a worker
    thread, which is a CONTEXT COPY, and that is where a `.set()`-based recorder silently
    loses the failure. `raising_tool` has a bug the dispatcher must still answer as a
    result.
    """
    async def failing_tool(tag: str = "x") -> str:
        return fail(f"Error navigating: Request timed out after 30.0 seconds [{tag}]")

    async def degrading_tool(tag: str = "x") -> str:
        degrade("truncated", tool="page_read", returned=500, total=9000, next_offset=500)
        return f"chars 0-500 of 9000 [{tag}]"

    async def clean_tool(tag: str = "x") -> str:
        return f"ok [{tag}]"

    def sync_failing_tool(tag: str = "x") -> str:
        return fail(f"Error listing tabs: No extension connection available [{tag}]")

    async def raising_tool(kind: str = "runtime") -> str:
        if kind == "pydantic":
            pydantic.TypeAdapter(int).validate_python("not-an-int")
        raise RuntimeError("kaboom <x>")

    registry = ToolRegistry()
    for fn in (failing_tool, degrading_tool, clean_tool, sync_failing_tool, raising_tool):
        registry.put(ToolSpec.from_function(fn))
    return BrowserPlugMCPServer(registry, instructions="FX17Test", record=record)


async def test_e2e_a_clean_call_is_a_plain_success():
    async with Client(_app(), mode="legacy") as client:
        result = await client.call_tool("clean_tool", {})
    assert result.is_error is False
    assert result.content[0].text == "ok [x]"
    assert META_KEY not in (result.meta or {})


async def test_e2e_a_recorded_failure_sets_isError_with_the_message_verbatim():
    async with Client(_app(), mode="legacy") as client:
        result = await client.call_tool("failing_tool", {})
    assert result.is_error is True, "this is the whole of the first criterion"
    assert result.content[0].text == \
        "Error navigating: Request timed out after 30.0 seconds [x]"


async def test_e2e_a_synchronous_tool_is_flagged_too():
    # A sync tool runs on a worker thread. Pinned end to end because the module-level
    # thread test cannot prove the dispatcher still hands the tool a COPY of the policy's
    # context rather than a fresh one.
    async with Client(_app(), mode="legacy") as client:
        result = await client.call_tool("sync_failing_tool", {})
    assert result.is_error is True
    assert result.content[0].text == \
        "Error listing tabs: No extension connection available [x]"


async def test_e2e_a_degraded_call_stays_a_success_and_carries_the_marker():
    async with Client(_app(), mode="legacy") as client:
        result = await client.call_tool("degrading_tool", {})
    assert result.is_error is False, "truncation is a partial success, not a failure"
    assert result.meta[META_KEY] == [{"kind": "truncated", "tool": "page_read",
                                      "returned": 500, "total": 9000, "next_offset": 500}]
    assert result.content[0].text == "chars 0-500 of 9000 [x]"


async def test_e2e_a_renamed_tool_name_fails_as_an_error_naming_its_replacement():
    async with Client(_app(), mode="legacy") as client:
        result = await client.call_tool("page_wait_for", {})
    assert result.is_error is True
    assert result.content[0].text == EXPECTED_RENAME_MESSAGES["page_wait_for"]


async def test_e2e_an_unknown_tool_fails_with_the_plain_unknown_tool_message():
    async with Client(_app(), mode="legacy") as client:
        result = await client.call_tool("totally_made_up", {})
    assert result.is_error is True
    assert result.content[0].text == "Unknown tool: 'totally_made_up'"
    assert "renamed" not in result.content[0].text


async def test_e2e_a_schema_violation_is_still_flagged():
    # Rejected before the tool runs: the 15 corpus validation errors were already 100%
    # flagged, and the dispatcher must not swallow that.
    async with Client(_app(), mode="legacy") as client:
        result = await client.call_tool("clean_tool", {"tag": {"not": "a string"}})
    assert result.is_error is True
    assert result.content[0].text.startswith("1 validation error for call[clean_tool]")


@pytest.mark.parametrize("kind,text", [
    ("runtime", "Error calling tool 'raising_tool': kaboom <x>"),
    # A pydantic error raised by the tool BODY is a bug in the tool, not an argument
    # error: the same result as any other exception, never a JSON-RPC error.
    ("pydantic", "Error calling tool 'raising_tool': 1 validation error for int"),
], ids=["runtime", "pydantic"])
async def test_e2e_a_tool_that_raises_is_an_error_result_not_a_protocol_error(kind, text):
    seen = []
    async with Client(_app(record=lambda *a: seen.append(a)), mode="legacy") as client:
        result = await client.call_tool("raising_tool", {"kind": kind})
    assert result.is_error is True
    assert result.content[0].text.startswith(text), result.content[0].text
    assert seen == [], "a call that raised files no verdict"


async def test_e2e_every_call_that_ran_reports_with_a_connection_scoped_identity():
    # No HTTP session here, so the identity is this connection's token: it must be there
    # (else CallTelemetry would file the verdict on the newest record for the tool name)
    # and identical for both calls of one connection.
    seen = []
    async with Client(_app(record=lambda *a: seen.append(a)), mode="legacy") as client:
        await client.call_tool("clean_tool", {})
        await client.call_tool("failing_tool", {})
    assert [(name, bool(outcome)) for name, outcome, _ in seen] == [
        ("clean_tool", False), ("failing_tool", True)]
    (session_a, id_a), (session_b, id_b) = seen[0][2], seen[1][2]
    assert session_a == session_b and session_a.startswith("conn-")
    assert id_a != id_b


async def test_e2e_concurrent_calls_do_not_cross_contaminate():
    # Probe 4's property (30 concurrent HTTP calls, 0 cross-contamination) brought
    # in-repo as a regression guard: each call must be judged on its OWN record.
    names = ["failing_tool", "degrading_tool", "clean_tool"] * 4
    async with Client(_app(), mode="legacy") as client:
        results = await asyncio.gather(
            *(client.call_tool(n, {"tag": str(i)}) for i, n in enumerate(names)))

    for i, (name, result) in enumerate(zip(names, results)):
        if name == "failing_tool":
            assert result.is_error is True
            assert result.content[0].text.endswith(f"[{i}]")
        elif name == "degrading_tool":
            assert result.is_error is False
            assert result.meta[META_KEY][0]["kind"] == "truncated"
            assert result.content[0].text == f"chars 0-500 of 9000 [{i}]"
        else:
            assert result.is_error is False
            assert result.content[0].text == f"ok [{i}]"
            assert META_KEY not in (result.meta or {})
