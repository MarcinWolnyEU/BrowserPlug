"""When nothing is attached, the response has to send the agent to the USER.

This is the one failure in the tool surface that an agent cannot resolve by trying
something else, and the original text ("No extension connection available") read as
"this call failed" rather than "stop and ask the person sitting in front of you".
Observed consequence: agents going off to install a browser, launch a headless one, or
start a fresh profile — none of which can attach to this server — instead of spending a
sentence on the human.

Two constraints shape where the guidance is added, and both are pinned below:

  * `server.resolve_target_browser` must keep returning the bare marker. Its string is
    asserted by EQUALITY in tests/unit/test_browser_routing.py, and tool_outcome.classify
    keys EXTENSION_DISCONNECTED off the same substring.
  * `tool_outcome.fail()` must hand its argument back byte-identical (the property that
    let ~56 return sites be wrapped without moving any agent-facing text).

So the guidance is appended at the response seam, `_error_of`, and the marker travels
untouched underneath it.
"""

import pytest

from facade_double import FacadeDouble
from server.tools import response_hints
from server.tools.response_hints import (NO_BROWSER_GUIDANCE, NO_BROWSER_MARKER,
                                         no_browser_hint)
from server.tools.tool_outcome import EXTENSION_DISCONNECTED, classify, fail


# --- the hint itself ----------------------------------------------------------

def test_hint_fires_on_the_disconnect_marker():
    out = no_browser_hint(f"Error navigating: {NO_BROWSER_MARKER}")
    assert NO_BROWSER_GUIDANCE in out


@pytest.mark.parametrize("message, expect_added_period", [
    (f"Error navigating: {NO_BROWSER_MARKER}", True),        # marker ends bare
    (f"Browsers: none connected. {NO_BROWSER_MARKER}.", False),   # caller supplied one
])
def test_the_join_reads_as_one_sentence(message, expect_added_period):
    """Caught live, not by review: the first version emitted "available This needs".

    The marker has no full stop of its own and the roster callers append to a sentence
    that does, so a fixed prefix is wrong in one direction or the other.
    """
    joined = message + no_browser_hint(message)
    assert ".." not in joined, f"doubled full stop: {joined!r}"
    assert " available This" not in joined and " connected This" not in joined, \
        f"two sentences run together: {joined!r}"
    assert no_browser_hint(message).startswith(".") is expect_added_period


@pytest.mark.parametrize("message", [
    None,
    "",
    "Request timed out after 22.1 seconds",
    "Tab 9 no longer exists.",
    "EVAL_UNAVAILABLE: enable the Allow user scripts toggle",
])
def test_hint_stays_silent_on_every_other_failure(message):
    """A hint that fires on everything is wallpaper — the response_hints thesis."""
    assert no_browser_hint(message) == ""


def test_hint_is_not_applied_twice_to_one_message():
    """A message can cross more than one seam; the paragraph must not stack."""
    once = f"Error navigating: {NO_BROWSER_MARKER}" + NO_BROWSER_GUIDANCE
    assert no_browser_hint(once) == ""


def test_guidance_names_the_human_step_and_forbids_the_workarounds():
    """The wording is the product here, so pin the load-bearing parts of it.

    Each assertion below is a thing an agent has actually done instead of asking:
    substituting its own browser, or treating a disconnected extension as a bug to
    engineer around.
    """
    text = NO_BROWSER_GUIDANCE
    assert "USER" in text, "the agent must be told whose problem this is"
    assert "Reconnect" in text, "name the control the user actually clicks"
    assert "Do not install, launch, or automate another browser" in text
    assert "server_info" in text, "name the tool that reports what the server sees"


# --- the constraints the placement exists to respect --------------------------

def test_the_marker_still_classifies_as_a_disconnect():
    """classify() keys on the substring; appending must not move it out of reach."""
    message = f"Error navigating: {NO_BROWSER_MARKER}" + NO_BROWSER_GUIDANCE
    assert classify(message) == EXTENSION_DISCONNECTED


def test_fail_still_returns_its_argument_byte_identical():
    """The load-bearing property: fail() records, it does not rewrite."""
    message = f"Error navigating: {NO_BROWSER_MARKER}" + NO_BROWSER_GUIDANCE
    assert fail(message) == message


def test_the_routing_layer_string_is_left_alone():
    """The guidance must NOT have been pushed down into the routing layer.

    If someone later 'simplifies' this by appending at the source, this fails next to
    test_browser_routing's equality assertion, and the reason is written down here.
    """
    import ast
    import inspect
    import textwrap
    from server.browsers import routing

    tree = ast.parse(textwrap.dedent(inspect.getsource(routing.resolve_target)))
    # Structure, not substrings: a marker that survives only in a comment or docstring
    # must not satisfy this, and a guidance name in a comment must not fail it.
    returned = [n.value.value for n in ast.walk(tree)
                if isinstance(n, ast.Return) and isinstance(n.value, ast.Constant)]
    assert NO_BROWSER_MARKER in returned, (
        "routing.resolve_target no longer returns the bare marker — tool_outcome.classify "
        "and test_browser_routing both key on that exact string")
    referenced = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    referenced |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert "NO_BROWSER_GUIDANCE" not in referenced
    assert "no_browser_hint" not in referenced
    assert not any(isinstance(n, ast.Constant) and isinstance(n.value, str)
                   and NO_BROWSER_GUIDANCE in n.value for n in ast.walk(tree))


# --- the ARMING, not just the helper ------------------------------------------
# The helper tests above would all still pass with the _error_of hook deleted. This
# repo's recurring failure is exactly that: the StreamFilter arming code stayed dead
# through 343 green JS tests while every helper it called was fine. So drive a real
# tool and read what an agent would actually receive.

class _DisconnectedServer(FacadeDouble):
    """A websocket_server that answers every action the way a dead socket does."""

    def __init__(self):
        super().__init__()
        self.sent = []

    async def send_request_and_wait(self, request, timeout=30.0, browser=None):
        self.sent.append(request)
        return {"error": NO_BROWSER_MARKER}


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name, kwargs", [
    ("navigate", {"url": "https://example.org"}),
    ("page_read", {}),
    ("tabs", {}),
    ("page_snapshot", {}),
])
async def test_a_real_tool_call_tells_the_agent_to_ask_the_user(tool_name, kwargs):
    from server.tools.mcp_tools import BrowserPlugTools

    tools = BrowserPlugTools(_DisconnectedServer())
    fn = (await tools.mcp.get_tool(tool_name)).fn
    out = await fn(**kwargs)

    assert isinstance(out, str), f"{tool_name} returned {type(out).__name__}"
    assert NO_BROWSER_MARKER in out, f"{tool_name} lost the marker: {out!r}"
    assert "needs the USER to fix" in out, (
        f"{tool_name} reported the disconnect without telling the agent to stop and ask "
        f"the user — the _error_of hook is not reached on this path: {out!r}")
    assert "Do not install, launch, or automate another browser" in out


def test_guidance_is_defined_once():
    """One wording, so the error seam and the roster cannot drift apart."""
    assert response_hints.NO_BROWSER_GUIDANCE is NO_BROWSER_GUIDANCE
    assert NO_BROWSER_MARKER not in NO_BROWSER_GUIDANCE, (
        "the guidance must not repeat the marker — classify() would still match, but the "
        "sentence would read as two failures")
