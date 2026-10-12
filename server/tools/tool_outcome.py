#!/usr/bin/env python3
"""Structural failure/degradation signalling for the BrowserPlug tool surface.

Of 2,903 corpus calls, 648 (22.3%) returned a failure and the MCP `isError` flag was
set on 19 of them: a 34x under-report. The flag itself works; it is simply never
reached, because BrowserPlug signals its own failures by RETURNING a string rather than by
raising. The operational consequence is the one that matters: a browser that is not
connected at all, and a bridge that answered nothing in 35 seconds, both reach the
client as SUCCESSFUL tool calls, the two conditions a harness would most want to
retry on are precisely the two it cannot see.

The fix deliberately does not change what a tool returns. Every existing test asserts
on that text, and page_act's timeout report is only useful in full, so a tool still
returns its prose, and additionally records, in a ContextVar, WHAT that prose MEANS.
`ToolOutcomePolicy` reads the record after the tool has run:

  * a recorded FAILURE becomes an `isError` result carrying the message VERBATIM: no
    prefix, no `_meta`. The policy RETURNS that result; it never raises it, because the
    SDK turns anything a handler raises into a JSON-RPC error instead of an error result.
  * a recorded DEGRADATION rides `_meta["browserplug/degraded"]` on an otherwise clean
    success, so "failed" and "succeeded partially" stay distinguishable without prose
    parsing.

`fail()` handing its argument straight back is the load-bearing property of the whole
design: it is what lets ~56 return sites be wrapped without moving a single byte of
agent-facing text, and therefore without editing the 42 test functions that pin it.

An MCP error result carries NO `_meta`, so the structured `code` cannot ride the wire on a
failure. It goes to the operator instead, through the optional `record` sink -> /health's
`last_command`. Do NOT smuggle the code into the message text: that would rewrite 56
agent-facing strings for no protocol benefit.

The tools/call dispatch that runs a call through the policy is server/tools/mcp_server.py;
tests/unit/test_tool_outcome.py pins the behaviour, end to end through an MCP client.
"""

import logging
from contextvars import ContextVar
from typing import Any, Awaitable, Callable, Dict, List, Optional

from mcp_types import CallToolResult, TextContent

# A routing-change notice rides the same seam a degradation does, and for the same
# reason: it is something the caller must read that the tool's own text cannot say. It
# goes at the FRONT of the response (and of an error message); appended, it arrives after
# the answer it is supposed to qualify.
from server.browsers import routing_notice

# The failure shape every error result shares: {"content": [text], "isError": true}.
from server.tools.tool_registry import error_result

# The stale-name hints live in a stdlib-only module because the launcher's proxy
# transport answers with the same text; re-exported here, where tests and callers read them.
from server.tools.tool_renames import RENAMED_TOOLS, renamed_tool_message  # noqa: F401

logger = logging.getLogger(__name__)

# One record per in-flight tool call. A ContextVar and not an attribute because the
# server serves many clients concurrently on one event loop: each request runs in its own
# task, which copies the context at creation, so one call's record can never be observed
# by another.
#
# What the ContextVar holds is a MUTABLE dict, and the recorders MUTATE it rather than
# `.set()` a replacement. That distinction is load-bearing: a context copy (a spawned
# task, or the worker THREAD a synchronous tool runs in, see tool_registry.ToolSpec.run)
# copies the reference, so a replacement made inside the copy is invisible to the policy
# waiting outside, and an invisible failure is reported to the client as a clean success,
# which is the exact defect this module exists to remove. Mutating the shared dict
# survives both.
_OUTCOME: ContextVar[Optional[Dict[str, Any]]] = ContextVar(
    "browserplug_tool_outcome", default=None)

# The verdict the wire layer attached to the most recent request's error envelope
# ({"code", "retryable"}), kept apart from _OUTCOME so a request that failed and was then
# recovered from cannot make a clean call look like it recorded something. Mutable for the
# same reason as _OUTCOME.
_WIRE: ContextVar[Optional[Dict[str, Any]]] = ContextVar(
    "browserplug_wire_verdict", default=None)

# Where a degradation rides on a successful result. Namespaced because `_meta` is shared
# with whatever else a result carries there.
META_KEY = "browserplug/degraded"

# Failure codes. These are for the OPERATOR (/health's last_command), not for the wire:
# an MCP error result has no _meta to carry them in.
TIMEOUT                = "TIMEOUT"
EXTENSION_DISCONNECTED = "EXTENSION_DISCONNECTED"
EXTENSION_SEND_FAILED  = "EXTENSION_SEND_FAILED"
EXTENSION_VERSION_SKEW = "EXTENSION_VERSION_SKEW"
SERVER_RESTARTING      = "SERVER_RESTARTING"
INVALID_ARGUMENT       = "INVALID_ARGUMENT"
PAGE_ERROR             = "PAGE_ERROR"

# `retryable` is DATA, never a property of the code or of the message. A timeout on a click
# may still be running and a dropped socket may have followed the click, so whether a
# re-send is safe depends on the action, and only extension_rpc knows it (the read-only
# allowlist in request_retry.py). It stamps `retryable` on the error envelope;
# BrowserCalls.request hands it to fail() through note_wire(). Anything that did not come
# with that verdict is not retryable.

# Codes the EXTENSION already spells out in its own error text (the file-access
# refusal, the proxy fail-closed pair, the chromium eval gate, the unpainted-window
# capture). Passing the token straight through means an operator sees the exact word the
# extension emitted rather than a server-side re-interpretation of it.
_EXT_CODES = ("FILE_ACCESS_DISABLED", "PROXY_PERMISSION_REQUIRED", "PROXY_UNSUPPORTED",
              "EVAL_UNAVAILABLE", "WINDOW_NOT_PAINTED")


def arm() -> None:
    """Install a fresh, empty record. Called by the policy BEFORE the tool runs, so a
    tool that records nothing can never inherit the previous call's verdict, and so the
    dict the recorders mutate belongs to THIS call's context."""
    _OUTCOME.set({})
    _WIRE.set({})


def extension_code(response: Any) -> Optional[str]:
    """The code an extension put on its own error frame ({"type": "error", "data":
    {"code", "message"}}) when it is one of _EXT_CODES, else None. The frame's message
    rarely spells the code, so classify() cannot find it in the text."""
    if not isinstance(response, dict) or response.get("type") != "error":
        return None
    data = response.get("data")
    code = data.get("code") if isinstance(data, dict) else None
    return code if code in _EXT_CODES else None


def note_wire(response: Any) -> None:
    """Remember the wire layer's verdict on the request that just finished, replacing the
    previous one: fail() reports the failure of the LAST request, and a success clears it.
    An extension's own refusal code (extension_code) counts as a verdict, never retryable."""
    wire = _WIRE.get()
    if wire is None:
        wire = {}
        _WIRE.set(wire)
    wire.clear()
    if isinstance(response, dict) and "error" in response and "code" in response:
        wire["code"] = response["code"]
        wire["retryable"] = bool(response.get("retryable"))
        return
    code = extension_code(response)
    if code:
        wire["code"] = code
        wire["retryable"] = False


def read() -> Optional[Dict[str, Any]]:
    """The record for the call in progress, or None when nothing was recorded (a clean
    success). Read by the policy after the tool has run, and by the test harness."""
    slot = _OUTCOME.get()
    return slot or None


def _slot() -> Dict[str, Any]:
    """The mutable record to write into, created on demand when nobody armed one: a
    tool driven directly (as the unit harness drives `tool.fn`) has no policy to arm
    it, and must still be able to record."""
    slot = _OUTCOME.get()
    if slot is None:
        slot = {}
        _OUTCOME.set(slot)
    return slot


def classify(message: str) -> str:
    """Map a failure message to a code, using the substrings the real corpus produced.

    Extension-emitted codes win over everything else: an EVAL_UNAVAILABLE that also
    mentions a timeout is an eval-gate problem, not a transport one.
    """
    for code in _EXT_CODES:
        if code in message:
            return code
    if "timed out after" in message:                     # 541 of 648 corpus failures
        return TIMEOUT
    if "The server is restarting, so this request was not sent" in message:
        return SERVER_RESTARTING
    if "No extension connection available" in message:
        return EXTENSION_DISCONNECTED
    if ("disconnected before responding" in message
            or "restarted while this request was in flight" in message):
        return EXTENSION_DISCONNECTED
    if "Failed to send request to extension" in message:
        return EXTENSION_SEND_FAILED
    if "version skew" in message:
        return EXTENSION_VERSION_SKEW
    return PAGE_ERROR                                    # a real page-level failure


def fail(message: str, *, code: Optional[str] = None,
         retryable: Optional[bool] = None, **extra) -> str:
    """Record a HARD failure and hand `message` straight back, unchanged.

    Call-site shape is deliberately `return fail(f"Error navigating: {err}")`, so the
    returned text is byte-identical to what the site returned before. That identity is
    what lets every return site be wrapped without touching a single assertion; if it
    ever regresses, ~42 test functions go red at once and the cause is here, not there.
    """
    wire = _WIRE.get() or {}
    if retryable is None:
        retryable = wire.get("retryable", False)
    code = code or wire.get("code") or classify(message)
    _slot().update({"failed": True, "code": code, "message": message,
                    "retryable": bool(retryable), **extra})
    return message


def invalid(message: str, **extra) -> str:
    """`fail()` for a local-validation refusal: the request never reached the browser,
    so the code is fixed rather than sniffed out of the prose."""
    return fail(message, code=INVALID_ARGUMENT, **extra)


def degrade(kind: str, **fields) -> None:
    """Mark an otherwise-successful result as degraded (truncated / empty / unsettled).

    Additive on purpose: one page_read can be BOTH truncated and a markdown->text
    fallback, and collapsing that to a single verdict would lose exactly the detail a
    client needs to decide whether to continue with an offset or change format.
    """
    marks: List[Dict[str, Any]] = _slot().setdefault("degraded", [])
    marks.append({"kind": kind, **fields})


def health_outcome(outcome: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The operator-facing projection of a record, for /health's `last_command.outcome`.

    Drops `message` (the client already has it, verbatim, as the error text) and keeps
    what prose cannot answer: did it fail, why, is a retry worth attempting, and what
    was degraded. None in, None out: a clean call reports no outcome rather than an
    empty one.
    """
    if not outcome:
        return None
    return {"failed": bool(outcome.get("failed")),
            "code": outcome.get("code"),
            "retryable": bool(outcome.get("retryable")),
            "degraded": outcome.get("degraded") or []}


def _with_leading_text(result, text: str):
    """Put `text` at the front of the result's first TEXT block.

    `model_copy` for the reason the degradation path uses it: page_screenshot's
    ImageContent block has to come through untouched, and a rebuilt result would
    re-validate every block. A result that carries no text block at all gets one inserted
    rather than losing the notice; a result shaped like nothing we know is handed back
    unchanged.
    """
    if not text or not hasattr(result, "content") or not hasattr(result, "model_copy"):
        return result
    content = list(result.content or [])
    for index, block in enumerate(content):
        if isinstance(getattr(block, "text", None), str):
            content[index] = block.model_copy(
                update={"text": f"{text}\n\n{block.text}"})
            break
    else:
        content.insert(0, TextContent(type="text", text=text))
    try:
        return result.model_copy(update={"content": content})
    except Exception as e:
        logger.warning("Could not attach a routing notice to the result: %s", e)
        return result


class ToolOutcomePolicy:
    """What a finished call's record means on the wire: a recorded failure becomes an
    isError result, a recorded degradation becomes `_meta`, and a pending routing notice
    goes in front of whatever the client reads first.

    `record`, when given, is called as record(tool_name, health_outcome(...), call) after
    every call that ran, clean ones included, so the operator view (/health's
    `last_command`) can never show a verdict left over from the previous command. `call`
    is the (session, request id) identity the dispatcher saw; None leaves it out. A sink
    that raises is logged and swallowed: diagnostics must not become a new way for a tool
    call to fail.
    """

    def __init__(self, record: Optional[Callable[..., Any]] = None):
        self.record = record

    async def apply(self, name: str, run: Callable[[], Awaitable[CallToolResult]],
                    call: Optional[Any] = None) -> CallToolResult:
        """Run one call (`run()` validates, runs the tool and returns its result) and
        apply the record. Whatever `run()` raises (an argument error, a tool bug)
        propagates with nothing reported and no notice attached: the dispatcher answers
        those with texts of its own."""
        arm()
        routing_notice.arm()
        result = await run()

        outcome = read()
        self._report(name, outcome, call)
        notice = routing_notice.pending()
        if outcome and outcome.get("failed"):
            # The tool's own prose becomes the error text verbatim, including page_act's
            # multi-line timeout report, which is only useful in full.
            return error_result(routing_notice.prepend(notice, outcome["message"]))
        if notice:
            result = _with_leading_text(result, notice)
        if not outcome:
            return result
        marks = outcome.get("degraded")
        if marks:
            meta = dict(getattr(result, "meta", None) or {})
            meta[META_KEY] = marks
            # model_copy, not a rebuilt result: page_screenshot's ImageContent block must
            # survive untouched.
            return result.model_copy(update={"meta": meta})
        return result

    def _report(self, name: str, outcome: Optional[Dict[str, Any]], call=None) -> None:
        if self.record is None:
            return
        try:
            if call is None:
                self.record(name, health_outcome(outcome))
            else:
                self.record(name, health_outcome(outcome), call)
        except Exception as e:
            logger.warning("tool-outcome record sink failed for %s: %s", name, e)
