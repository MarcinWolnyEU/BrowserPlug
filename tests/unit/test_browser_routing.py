"""
Unit tests for multi-Firefox ROUTING (no Firefox, no psutil).

The registry (test_browser_registry.py) says WHICH browsers exist. This module covers
what an agent actually experiences on top of it:

  * every existing tool routes to the calling client's browser with ZERO schema change
    (no `browser` parameter anywhere) — the target comes from WHO is calling
  * the selection is PER MCP CLIENT: two Claude Codes share one server, and a global
    switch would silently hijack the other agent's session — acting in the wrong
    *logged-in* Firefox is the worst failure this product has
  * the `browsers` tool exists ONLY while >=2 are connected, so a single-browser install
    keeps today's 9-tool surface byte for byte
  * a selected browser that goes away is an ERROR with a recovery hint, never a silent
    fallback — EXCEPT when <=1 browser is left, where the tool that would fix it has just
    been hidden, so the stale selection is dropped instead
"""

import asyncio
from contextlib import contextmanager
from unittest.mock import patch

import pytest

import test_imports  # noqa: F401
from server.server import BrowserPlugServer
from fake_process_table import fake_process_table  # noqa: F401  (autouse)
from test_browser_registry import (FakeWS, connect, hello, remote_hello, disconnect,
                                   wait_until)

BASE_TOOLS = {
    "tabs", "navigate", "page_read", "page_snapshot", "page_act", "page_eval",
    "page_screenshot", "requests", "server_info",
}


@pytest.fixture
def server():
    return BrowserPlugServer(host="localhost", port=8772, start_mcp=False)


@pytest.fixture
def tools(server):
    return server.mcp_tools


def ident(pid, brand):
    """What the local browser probe would report (patched out in connect())."""
    return {"pid": pid, "brand": brand, "friendly": brand, "is_firefox": True,
            "exe_path": r"C:\ff\firefox.exe", "start_time": None}


async def add_browser(server, instance_id, pid, brand, **hello_fields):
    """Connect a browser and let it say hello. Returns (FakeWS, handler task)."""
    ws = FakeWS(peer=("127.0.0.1", 10000 + pid))
    task = await connect(server, ws, identity=ident(pid, brand))
    await hello(server, ws, instance_id=instance_id,
                browserInfo={"name": "Firefox", "version": "152.0"}, **hello_fields)
    return ws, task


@contextmanager
def http_context(headers):
    """Bind request headers exactly as HttpEndpoints does for an /mcp request (the raw
    ASGI header pairs through request_context.headers_of), so the client key is derived
    the way it is in production."""
    from server.web import request_context

    raw = [(k.encode("latin-1"), v.encode("latin-1")) for k, v in headers.items()]
    token = request_context.bind(request_context.headers_of(raw))
    try:
        yield
    finally:
        request_context.reset(token)


@contextmanager
def as_client(pid):
    """Run tool calls as the MCP client with this pid — through the REAL client-key path
    (the stdio launcher forwards the pid in a header), not by stubbing client_key out.
    A stub would hide the failure mode that matters most: a client key that doesn't
    actually distinguish clients turns every switch into a global one."""
    with http_context({"x-browserplug-client-pid": str(pid)}):
        yield


async def call_tabs(tools, server, wss, timeout=2.0):
    """Run the `tabs` tool end to end and report WHICH browser got the request.

    Returns (the FakeWS that received it, the tool's output). The FakeWS never answers on
    its own, so this plays the extension: it replies to whatever request landed.
    """
    for ws in wss:
        ws.sent.clear()
    fn = (await tools.mcp.get_tool("tabs")).fn
    task = asyncio.create_task(fn(action="list"))
    # The call either reaches a browser or is refused outright (no browser / a stale
    # selection, which must NOT fall back) — both are terminal, so wait for whichever
    # lands rather than guessing how long routing takes.
    await wait_until(lambda: any(ws.sent for ws in wss) or task.done(),
                     "the tabs call either reached a browser or was refused")

    got = [ws for ws in wss if ws.sent]
    for ws in got:
        req = ws.sent[-1]
        await server.rpc.handle_response({
            "id": req["id"], "type": "response", "action": req["action"],
            "data": {"tabs": [{"id": 1, "url": "https://example.org/", "title": "T",
                               "windowId": 1, "active": True}],
                     "windows": [{"id": 1, "focused": True}]},
        })
    await asyncio.sleep(0)
    out = await asyncio.wait_for(task, timeout=timeout)
    assert len(got) <= 1, "a tool call must go to exactly ONE browser"
    return (got[0] if got else None), out


async def call_browsers(tools, **kwargs):
    fn = (await tools.mcp.get_tool("browsers")).fn
    return await fn(**kwargs)


# =============================================================================
# Resolution matrix (rules 1-5)
# =============================================================================

@pytest.mark.asyncio
async def test_rule5_no_browser_gives_the_legacy_error(tools, server):
    """Nothing connected: the same message the single-slot server always returned, so
    every tool's "Error ...: No extension connection available" is unchanged."""
    assert server.resolve_target_browser("local") == "No extension connection available"
    _, out = await call_tabs(tools, server, [])
    assert "No extension connection available" in out


@pytest.mark.asyncio
async def test_rule3_single_browser_routes_to_it_and_hides_the_browsers_tool(tools, server):
    """The single-Firefox install: today's behavior, today's 9-tool surface."""
    ws, task = await add_browser(server, "ff-a", 101, "Firefox")

    assert server.resolve_target_browser("local") is server.browsers["ff-a"]
    got, out = await call_tabs(tools, server, [ws])
    assert got is ws
    assert "example.org" in out

    names = {t.name for t in await tools.mcp.list_tools()}
    assert names == BASE_TOOLS, "one browser must not add a `browsers` tool"

    await disconnect(ws, task)


@pytest.mark.asyncio
async def test_rule4_two_browsers_no_selection_routes_to_first_connected(tools, server):
    """A second Firefox appearing must not re-route a session already in flight."""
    a, a_task = await add_browser(server, "ff-a", 101, "Firefox")
    b, b_task = await add_browser(server, "ff-b", 102, "Zen Browser")

    assert server.resolve_target_browser("local") is server.browsers["ff-a"]
    got, _ = await call_tabs(tools, server, [a, b])
    assert got is a, "with no selection, calls stay on the first-connected browser"

    await disconnect(a, a_task)
    await disconnect(b, b_task)


@pytest.mark.asyncio
async def test_rule1_explicit_selection_wins(tools, server):
    a, a_task = await add_browser(server, "ff-a", 101, "Firefox")
    b, b_task = await add_browser(server, "ff-b", 102, "Zen Browser")

    with as_client(777):
        await call_browsers(tools, action="use", target=2)
        got, _ = await call_tabs(tools, server, [a, b])
    assert got is b

    await disconnect(a, a_task)
    await disconnect(b, b_task)


@pytest.mark.asyncio
async def test_rule2_selected_browser_gone_errors_and_never_falls_back(tools, server):
    """The selected Firefox dies while others remain: an error naming the live browsers.
    Silently acting in a DIFFERENT logged-in browser would be far worse than failing."""
    a, a_task = await add_browser(server, "ff-a", 101, "Firefox")
    b, b_task = await add_browser(server, "ff-b", 102, "Zen Browser")
    c, c_task = await add_browser(server, "ff-c", 103, "LibreWolf")

    with as_client(777):
        await call_browsers(tools, action="use", target=3)   # LibreWolf
        await disconnect(c, c_task)                          # ...and it goes away

        # Two browsers are still up, so the selection is NOT dropped (the `browsers` tool
        # is there to fix it) — the call must fail, not land in another browser.
        err = server.resolve_target_browser("pid:777")
        assert isinstance(err, str)
        assert "LibreWolf" in err and "no longer connected" in err
        assert "[1] Firefox pid 101" in err and "[2] Zen Browser pid 102" in err, \
            "the error must name the survivors"
        assert 'browsers(action="use"' in err

        got, out = await call_tabs(tools, server, [a, b])
        assert got is None, "no fallback: the request must not be sent to another browser"
        assert "LibreWolf" in out
        assert server.browser_selection["pid:777"] == "ff-c", "the selection stands"

    await disconnect(a, a_task)
    await disconnect(b, b_task)


@pytest.mark.asyncio
async def test_stale_selection_is_dropped_once_one_browser_remains(tools, server):
    """The edge case that keeps "the tool only exists at >=2" coherent: with one browser
    left, `browsers` has just been hidden — so an error the agent cannot act on would
    strand it. The stale selection is dropped and the survivor takes the calls."""
    a, a_task = await add_browser(server, "ff-a", 101, "Firefox")
    b, b_task = await add_browser(server, "ff-b", 102, "Zen Browser")

    with as_client(777):
        await call_browsers(tools, action="use", target="zen")
        assert server.browser_selection["pid:777"] == "ff-b"

        await disconnect(b, b_task)   # the selected one leaves; only the Firefox is left

        assert server.resolve_target_browser("pid:777") is server.browsers["ff-a"]
        assert "pid:777" not in server.browser_selection, "the unusable selection is dropped"
        got, _ = await call_tabs(tools, server, [a])
        assert got is a

    names = {t.name for t in await tools.mcp.list_tools()}
    assert names == BASE_TOOLS, "back to one browser -> the `browsers` tool is gone again"

    await disconnect(a, a_task)


@pytest.mark.asyncio
async def test_a_selection_pointing_at_a_survivor_is_kept(tools, server):
    """Only STALE selections are pruned: a client sitting on the browser that survived
    must stay on it (and not be quietly reset to the default)."""
    a, a_task = await add_browser(server, "ff-a", 101, "Firefox")
    b, b_task = await add_browser(server, "ff-b", 102, "Zen Browser")

    with as_client(777):
        await call_browsers(tools, action="use", target="zen")
    await disconnect(a, a_task)   # the OTHER one leaves

    assert server.browser_selection["pid:777"] == "ff-b"
    assert server.resolve_target_browser("pid:777") is server.browsers["ff-b"]

    await disconnect(b, b_task)


# =============================================================================
# Selection is PER CLIENT (the whole point)
# =============================================================================

@pytest.mark.asyncio
async def test_selection_is_per_client_not_global(tools, server):
    """Two agents on the shared server, each on its own Firefox. Each one's calls land in
    ITS browser: a global switch would hijack the other's authenticated session."""
    a, a_task = await add_browser(server, "ff-a", 101, "Firefox")
    b, b_task = await add_browser(server, "ff-b", 102, "Zen Browser")

    with as_client(111):
        await call_browsers(tools, action="use", target=1)
    with as_client(222):
        await call_browsers(tools, action="use", target=2)

    assert server.browser_selection == {"pid:111": "ff-a", "pid:222": "ff-b"}

    # Interleave them — each call must follow its own client, not the last one to switch.
    with as_client(222):
        got, _ = await call_tabs(tools, server, [a, b])
        assert got is b
    with as_client(111):
        got, _ = await call_tabs(tools, server, [a, b])
        assert got is a
    with as_client(222):
        got, _ = await call_tabs(tools, server, [a, b])
        assert got is b

    # A third client that never chose keeps the default (rule 4) — untouched by the others.
    with as_client(333):
        got, _ = await call_tabs(tools, server, [a, b])
        assert got is a

    await disconnect(a, a_task)
    await disconnect(b, b_task)


# =============================================================================
# The `browsers` tool: appearance, signature, list, use
# =============================================================================

@pytest.mark.asyncio
async def test_browsers_tool_appears_at_two_and_flips_the_tools_signature(tools, server):
    a, a_task = await add_browser(server, "ff-a", 101, "Firefox")
    sig_one = tools.tools_signature
    assert {t.name for t in await tools.mcp.list_tools()} == BASE_TOOLS

    b, b_task = await add_browser(server, "ff-b", 102, "Zen Browser")
    assert {t.name for t in await tools.mcp.list_tools()} == BASE_TOOLS | {"browsers"}
    sig_two = tools.tools_signature
    assert sig_two != sig_one, "the launcher polls this to push tools/list_changed"

    # A third browser doesn't add a tool, but it DOES change the roster in the
    # description — clients must re-fetch, so the signature has to move again.
    c, c_task = await add_browser(server, "ff-c", 103, "LibreWolf")
    assert tools.tools_signature not in (sig_one, sig_two)
    assert "[3] LibreWolf pid 103" in (await tools.mcp.get_tool("browsers")).description

    await disconnect(c, c_task)
    await disconnect(b, b_task)
    assert {t.name for t in await tools.mcp.list_tools()} == BASE_TOOLS
    assert tools.tools_signature == sig_one, "back to one browser -> back to the old surface"

    await disconnect(a, a_task)


@pytest.mark.asyncio
async def test_browsers_description_embeds_the_live_roster(tools, server):
    """The description carries the roster so an agent needs no `list` call — but NOT the
    timestamps: a description that moved on every page load would flip tools_signature and
    spam every connected client with tools/list_changed."""
    a, a_task = await add_browser(server, "ff-a", 101, "Firefox")
    b, b_task = await add_browser(server, "ff-b", 102, "Zen Browser")

    desc = (await tools.mcp.get_tool("browsers")).description
    assert "[1] Firefox pid 101" in desc and "[2] Zen Browser pid 102" in desc
    assert "Firefox 152.0" in desc
    assert "server default" in desc
    assert "per-MCP-client" in desc
    assert "connected 1" not in desc and "last used" not in desc

    sig = tools.tools_signature
    server.browsers["ff-a"].last_activity = None
    server.browsers["ff-a"].last_action = "tabs.list"
    server._refresh_browsers_tool()
    assert tools.tools_signature == sig, "activity must not churn the tool surface"

    await disconnect(a, a_task)
    await disconnect(b, b_task)


@pytest.mark.asyncio
async def test_browsers_list_marks_your_browser_and_the_default(tools, server):
    a, a_task = await add_browser(server, "ff-a", 101, "Firefox")
    b, b_task = await add_browser(server, "ff-b", 102, "Zen Browser")

    with as_client(777):
        out = await call_browsers(tools, action="use", target=2)
        assert "not valid here" in out.lower() or "NOT valid here" in out

        out = await call_browsers(tools)          # action defaults to list
        assert "2 browser(s) connected" in out
        lines = {line.split("]")[0].strip("  ["): line for line in out.splitlines() if "] " in line}
        assert "server default" in lines["1"] and "YOUR active browser" not in lines["1"]
        assert "YOUR active browser" in lines["2"]
        assert "connected " in lines["2"], "list (unlike the description) shows timestamps"

    await disconnect(a, a_task)
    await disconnect(b, b_task)


@pytest.mark.asyncio
async def test_use_by_slot_and_by_name_substring(tools, server):
    a, a_task = await add_browser(server, "ff-a", 101, "Firefox")
    b, b_task = await add_browser(server, "ff-b", 102, "Zen Browser")

    with as_client(777):
        out = await call_browsers(tools, action="use", target=2)      # int slot
        assert "[2] Zen Browser pid 102" in out and "was [1] Firefox pid 101" in out
        assert server.browser_selection["pid:777"] == "ff-b"
        assert "page_snapshot" in out, "the switch must warn that refs/tab ids don't carry over"

        await call_browsers(tools, action="use", target="firefox")    # name substring
        assert server.browser_selection["pid:777"] == "ff-a"

        await call_browsers(tools, action="use", target="2")          # slot as a string
        assert server.browser_selection["pid:777"] == "ff-b"

        out = await call_browsers(tools, action="use", target="zen")
        assert "Already on" in out, "re-selecting the current browser is a no-op, said plainly"

    await disconnect(a, a_task)
    await disconnect(b, b_task)


@pytest.mark.asyncio
async def test_use_with_an_ambiguous_name_errors_and_lists_the_candidates(tools, server):
    a, a_task = await add_browser(server, "ff-a", 101, "Firefox")
    b, b_task = await add_browser(server, "ff-b", 102, "Firefox Developer Edition")
    c, c_task = await add_browser(server, "ff-c", 103, "Zen Browser")

    with as_client(777):
        out = await call_browsers(tools, action="use", target="firefox")
        assert "ambiguous" in out
        assert "[1] Firefox pid 101" in out and "[2] Firefox Developer Edition pid 102" in out
        assert "[3]" not in out, "only the candidates, not the whole roster"
        assert "pid:777" not in server.browser_selection, "an ambiguous target must not guess"

        # Disambiguating by the unique part, or by slot, works.
        await call_browsers(tools, action="use", target="developer")
        assert server.browser_selection["pid:777"] == "ff-b"

        out = await call_browsers(tools, action="use", target=9)
        assert "No connected browser in slot 9" in out
        out = await call_browsers(tools, action="use", target="nope")
        assert 'No connected browser matches "nope"' in out
        assert server.browser_selection["pid:777"] == "ff-b", "a bad target changes nothing"

    await disconnect(a, a_task)
    await disconnect(b, b_task)
    await disconnect(c, c_task)


@pytest.mark.asyncio
async def test_use_matches_the_EDITION_the_roster_prints_not_just_the_name(tools, server):
    """A remote browser is named `remote-<addr>`, because there is no process here to
    name it; the roster prints its own name beside that. Matching only `name` meant
    `use "Android"` failed against a row that reads "Firefox for Android Nightly",
    which is the first thing anyone types."""
    a, a_task = await add_browser(server, "ff-a", 101, "Firefox")

    phone = FakeWS(peer=("192.168.8.104", 51000))
    phone_task = await connect(server, phone)
    await remote_hello(server, phone, instance_id="phone-1",
                       platform={"os": "android", "arch": "aarch64"},
                       browserInfo={"name": "Firefox Nightly", "vendor": "Mozilla",
                                    "version": "157.0a1"})
    await wait_until(lambda: len(tools.calls.connected_browsers()) == 2,
                     "both browsers registered")

    with as_client(777):
        # The name the roster leads with still works…
        out = await call_browsers(tools, action="use", target="remote-192.168.8.104")
        assert server.browser_selection["pid:777"] == "phone-1", out

        await call_browsers(tools, action="use", target="firefox pid")
        assert server.browser_selection["pid:777"] == "ff-a"

        # …and so does the name the browser gives for ITSELF, which is the only part
        # of that row a person reads as the browser's identity.
        out = await call_browsers(tools, action="use", target="Android")
        assert server.browser_selection["pid:777"] == "phone-1", out
        assert "Firefox for Android Nightly" in out or "remote-192.168.8.104" in out

        # Case-insensitive, and a mid-string fragment counts.
        await call_browsers(tools, action="use", target="firefox pid")
        await call_browsers(tools, action="use", target="nightly")
        assert server.browser_selection["pid:777"] == "phone-1"

    await disconnect(phone, phone_task)
    await disconnect(a, a_task)


@pytest.mark.asyncio
async def test_a_target_matching_two_browsers_across_both_fields_is_still_ambiguous(tools, server):
    """Widening the haystack must not start guessing: "firefox" now matches the local
    browser by NAME and the phone by EDITION, and that is two candidates, not a pick."""
    a, a_task = await add_browser(server, "ff-a", 101, "Firefox")

    phone = FakeWS(peer=("192.168.8.104", 51000))
    phone_task = await connect(server, phone)
    await remote_hello(server, phone, instance_id="phone-1",
                       platform={"os": "android", "arch": "aarch64"},
                       browserInfo={"name": "Firefox Nightly", "version": "157.0a1"})
    await wait_until(lambda: len(tools.calls.connected_browsers()) == 2,
                     "both browsers registered")

    with as_client(777):
        out = await call_browsers(tools, action="use", target="firefox")
        assert "ambiguous" in out and "2 browsers" in out
        assert "pid:777" not in server.browser_selection, "ambiguity must not guess"

    await disconnect(phone, phone_task)
    await disconnect(a, a_task)


@pytest.mark.asyncio
async def test_browsers_list_reports_a_stale_selection(tools, server):
    """`list` is how the agent recovers from rule 2, so the error must be visible there."""
    a, a_task = await add_browser(server, "ff-a", 101, "Firefox")
    b, b_task = await add_browser(server, "ff-b", 102, "Zen Browser")
    c, c_task = await add_browser(server, "ff-c", 103, "LibreWolf")

    with as_client(777):
        await call_browsers(tools, action="use", target="libre")
        await disconnect(c, c_task)
        out = await call_browsers(tools, action="list")

    assert "no longer connected" in out and "LibreWolf" in out
    assert "[1] Firefox pid 101" in out and "[2] Zen Browser pid 102" in out

    await disconnect(a, a_task)
    await disconnect(b, b_task)


# =============================================================================
# Aggregate proxy visibility
# =============================================================================

@pytest.mark.asyncio
async def test_proxy_arg_is_exposed_when_ANY_browser_holds_the_grant(tools, server):
    """One schema serves every browser, so the `proxy` arg can't be per-browser. It is
    shown when some connected Firefox can use it; the extension stays fail-closed."""
    a, a_task = await add_browser(server, "ff-a", 101, "Firefox",
                                  proxyAccess={"has_proxy_permission": False})
    assert tools.proxy_tools_enabled() is False
    assert "proxy" not in (await tools.mcp.get_tool("tabs")).parameters["properties"]

    b, b_task = await add_browser(server, "ff-b", 102, "Zen Browser",
                                  proxyAccess={"has_proxy_permission": True})
    assert tools.calls.proxy_permission_granted() is True
    assert tools.proxy_tools_enabled() is True
    assert "proxy" in (await tools.mcp.get_tool("tabs")).parameters["properties"]

    # The grant-holder leaves -> the arg goes away again.
    await disconnect(b, b_task)
    assert tools.refresh_proxy_tool_visibility() is True
    assert "proxy" not in (await tools.mcp.get_tool("tabs")).parameters["properties"]

    await disconnect(a, a_task)


@pytest.mark.asyncio
async def test_proxy_permission_error_names_the_browser_that_has_the_grant(tools, server):
    a, a_task = await add_browser(server, "ff-a", 101, "Firefox",
                                  proxyAccess={"has_proxy_permission": False})
    b, b_task = await add_browser(server, "ff-b", 102, "Zen Browser",
                                  proxyAccess={"has_proxy_permission": True})

    with as_client(777):       # no selection -> routed to [1] work, no grant
        err = tools.calls.error_of({"error": "PROXY_PERMISSION_REQUIRED: grant it in about:addons"})
    assert "[2] Zen Browser pid 102" in err
    assert 'browsers(action="use", target=2)' in err
    assert "PROXY_PERMISSION_REQUIRED" in err, "the extension's own error survives"

    await disconnect(a, a_task)
    await disconnect(b, b_task)


# =============================================================================
# server_info — the only place multi-browser is discoverable at 1 browser
# =============================================================================

@pytest.mark.asyncio
async def test_server_info_names_the_active_browser(tools, server):
    a, a_task = await add_browser(server, "ff-a", 101, "Firefox")
    fn = (await tools.mcp.get_tool("server_info")).fn

    out = await fn()
    assert "Browsers: 1 connected — [1] Firefox pid 101" in out
    assert "`browsers` tool appears" in out, \
        "with the tool hidden, server_info is where multi-browser stays discoverable"

    b, b_task = await add_browser(server, "ff-b", 102, "Zen Browser")
    with as_client(777):
        await call_browsers(tools, action="use", target=2)
        out = await fn()
    assert "Browsers: 2 connected — [1] Firefox pid 101, [2] Zen Browser pid 102" in out
    assert "Your calls go to: [2] Zen Browser pid 102" in out

    await disconnect(a, a_task)
    await disconnect(b, b_task)


# =============================================================================
# Client key resolution
# =============================================================================

def test_client_key_is_local_without_an_http_context(tools):
    """DirectMCPTestClient, unit tests, an in-process embed: one implicit client, so the
    selection map has a single key and behavior is exactly today's."""
    assert tools.calls.client_key() == "local"


def test_client_key_prefers_the_pid_forwarded_by_the_launcher(tools):
    """The stdio launcher forwards the REAL client's pid; the TCP peer would be the
    launcher itself. It also survives the client re-opening its MCP session."""
    with http_context({"x-browserplug-client-pid": "4242", "mcp-session-id": "abc"}):
        assert tools.calls.client_key() == "pid:4242"


def test_client_key_falls_back_to_the_session_pid_then_the_session_id(tools, server):
    """No forwarded header (a direct HTTP client): the ASGI wrapper has already mapped the
    peer port to a pid for this session — use it, so the key survives reconnects too."""
    server.http_clients["sess-1"] = {"pid": 999}
    with http_context({"mcp-session-id": "sess-1"}):
        assert tools.calls.client_key() == "pid:999"

    # Session known, pid not resolvable: the session id still distinguishes clients.
    server.http_clients["sess-2"] = {"pid": None}
    with http_context({"mcp-session-id": "sess-2"}):
        assert tools.calls.client_key() == "session:sess-2"


def test_client_key_degrades_to_local_when_the_lookup_blows_up(tools):
    """Any failure in here must degrade to today's behavior, never misroute."""
    with patch("server.web.request_context.current_headers",
               side_effect=RuntimeError("No active HTTP request found.")):
        assert tools.calls.client_key() == "local"


# --- "server default" means the PINNED browser, not the first-connected one ----------
# Live 2026-08-18: config.json pinned Firefox Developer Edition, routing sent unrouted
# clients there, and every roster still marked Chrome Canary "<- server default" — the
# three display sites read `live[0]`, or the legacy `default_browser` compat property,
# while only routing consulted the pin. A roster that names the wrong logged-in profile
# is worse than no marker: it invites an agent to "switch" to the browser it is already
# in, and it made a user doubt a setting that was working.

@contextmanager
def pinned_to(instance_id):
    """Pin a browser for the duration of the block, then put config.json back."""
    from server.browsers import default_browser as db
    from browserplug_shared.config_store import reset
    db.write(db.normalize({"mode": "pinned", "instance_id": instance_id, "name": "pinned"}))
    try:
        yield
    finally:
        reset()


@pytest.mark.asyncio
async def test_the_roster_marks_the_pinned_browser_as_the_default(tools, server):
    a, a_task = await add_browser(server, "ff-a", 101, "Firefox")
    b, b_task = await add_browser(server, "ff-b", 102, "Zen Browser")

    with pinned_to("ff-b"):
        server._refresh_browsers_tool()
        desc = (await tools.mcp.get_tool("browsers")).description
        assert "server default" in desc.split("[2]")[1].split("\n")[0], desc
        assert "server default" not in desc.split("[1]")[1].split("[2]")[0], desc

        with as_client(778):
            out = await call_browsers(tools)
            lines = {line.split("]")[0].strip("  ["): line for line in out.splitlines() if "] " in line}
            assert "server default" in lines["2"], out
            assert "server default" not in lines["1"], out
            # The pinned browser is also where this client's calls actually go, which is
            # the whole point of the marker agreeing with routing.
            assert "YOUR active browser" in lines["2"], out

        # /health tells the same story to the options page and the popup. Asserted
        # unconditionally: a guard here would turn a renamed method into a silent pass,
        # which is the shape of the bug this test exists for.
        health = server.get_health_info()
        flags = {b_["slot"]: b_["is_default"] for b_ in health["browsers"]}
        assert flags == {1: False, 2: True}, health["browsers"]

    await disconnect(a, a_task)
    await disconnect(b, b_task)


@pytest.mark.asyncio
async def test_a_pin_on_a_browser_that_is_not_here_falls_back_to_first_connected(tools, server):
    """A pin is a preference: pinning a closed browser must not leave the roster with no
    default at all, or mark one nobody can reach."""
    a, a_task = await add_browser(server, "ff-a", 101, "Firefox")
    b, b_task = await add_browser(server, "ff-b", 102, "Zen Browser")

    with pinned_to("a-browser-that-is-not-running"):
        server._refresh_browsers_tool()
        desc = (await tools.mcp.get_tool("browsers")).description
        assert "server default" in desc.split("[1]")[1].split("[2]")[0], desc

    await disconnect(a, a_task)
    await disconnect(b, b_task)


def test_match_browser_treats_a_superscript_slot_as_a_name_not_a_number():
    # isdigit() is True for "²" and int() raises on it; the slot branch is ASCII-only.
    from server.tools.browsers import match_browser
    b, err = match_browser("²", [])
    assert b is None and "slot" not in err
