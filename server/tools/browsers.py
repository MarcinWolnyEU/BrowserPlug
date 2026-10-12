"""The `browsers` tool and the roster text around it.

The tool exists only while two or more browsers are connected, and its description embeds
the live roster, so `mcp_tools.py` re-registers it whenever the roster changes. This module
builds that description, renders one roster line, resolves a `use` target and writes the
one-line roster `server_info` prints.
"""

from typing import Annotated, Any, Callable, Optional, Union

from pydantic import Field

from server.tools.response_hints import NO_BROWSER_GUIDANCE

from server.tools.tool_outcome import EXTENSION_DISCONNECTED, degrade, invalid, fail

from server.tools.browser_calls import BrowserCalls

from server.tools.call_order import calls_ahead_of_switch


def hms(dt) -> str:
    return dt.strftime("%H:%M:%S") if dt else "?"


def browser_line(b, active=None, default=None, with_activity: bool = False) -> str:
    """One roster line. `with_activity` (list only, never the description) adds the
    timestamps; they move constantly, and a description that changed on every page
    load would flip tools_signature and spam clients with tools/list_changed."""
    info = b.browser_info or {}
    engine = getattr(b, "engine", None)
    # For a GECKO browser, `version` in browser_info is the FIREFOX it is built on
    # (Zen: 152.0.5). For a FORK that is not its version; its own is 1.21.6b; so
    # name it, give its version when it publishes one, and label the base for what it
    # is:  Zen Browser 1.21.6b (Firefox 152.0.5).
    # For a BLINK browser `version` IS its own version; there is no "built-on-Firefox"
    # base to name, so it renders as plain "{edition} {ver}".
    _default = "Chromium" if engine == "blink" else "Firefox"
    edition = getattr(b, "edition", None) or b.friendly or _default
    family = getattr(b, "family", None) or _default
    ver = info.get("version")
    fork_ver = getattr(b, "fork_version", None)
    if engine == "blink":
        bits = [f"{edition} {ver}" if ver else edition]
    elif not ver:
        bits = [f"{edition} {fork_ver}" if fork_ver else edition]
    elif family == "Firefox":
        bits = [f"{edition} {ver}"]
    elif fork_ver:
        bits = [f"{edition} {fork_ver} (Firefox {ver})"]
    else:
        bits = [f"{edition} (Firefox {ver})"]
    if b.is_remote and b.remote_addr:
        bits.append(f"host {b.remote_addr}")
    if with_activity:
        bits.append(f"connected {hms(b.connected_at)}")
        bits.append(f"last used {hms(b.last_activity)}" if b.last_action
                    else "not used yet")
    marks = []
    if active is not None and b is active:
        marks.append("YOUR active browser")
    if default is not None and b is default:
        marks.append("server default")
    mark = f"   <- {', '.join(marks)}" if marks else ""
    return f"  [{b.slot}] {b.name}; {' · '.join(bits)}{mark}"


def tool_description(calls: BrowserCalls) -> Optional[str]:
    """The `browsers` tool's agent-facing description, with the live roster baked in
    (so an agent usually never needs a `list` call). None when the tool must not
    exist; i.e. fewer than 2 Firefoxes connected."""
    live = calls.connected_browsers()
    if len(live) < 2:
        return None
    default = calls.preferred_browser(live)
    roster = "\n".join(browser_line(b, default=default) for b in live)
    return (
        f"Pick which of the {len(live)} connected browsers your calls drive. Each is a "
        "separate logged-in profile, and tabs, navigate, page_* and requests act in one "
        "of them: until you switch, the server default marked below (the pinned browser, "
        "else the first to connect). The choice is per-MCP-client and sticky; it moves "
        "only your session. Tab ids and page_snapshot refs do not carry across browsers. "
        "If the browser you chose disconnects, calls fail instead of falling back\n"
        "\n"
        f"{roster}"
    )


def match_browser(target, live):
    """Resolve a `use` target to a browser: (browser, error). Forgiving; a slot
    number, or a case-insensitive substring of either name the roster prints.
    Ambiguity is an error listing the candidates, never a guess."""
    s = str(target).strip().strip("[]")
    roster = "\n".join(browser_line(b) for b in live)
    if s.isascii() and s.isdigit():
        slot = int(s)
        for b in live:
            if b.slot == slot:
                return b, None
        return None, f"No connected browser in slot {slot}. Connected:\n{roster}"
    low = s.lower()
    # Both identity strings the roster shows, because either is a fair thing to
    # type. `name` is what this server calls the browser, which for a REMOTE one is
    # `remote-<addr>` (no process here to name it); `edition` is the browser's own
    # name printed beside it, e.g. "Firefox for Android Nightly". Matching `name`
    # alone advertised a label you could not select by.
    hits = [b for b in live
            if low in (b.name or "").lower() or low in (b.edition or "").lower()]
    if len(hits) == 1:
        return hits[0], None
    if not hits:
        return None, f'No connected browser matches "{s}". Connected:\n{roster}'
    cands = "\n".join(browser_line(b) for b in hits)
    return None, (f'"{s}" is ambiguous; it matches {len(hits)} browsers:\n{cands}\n'
                  "Use the slot number, or a substring that matches only one.")


def browsers_summary(calls: BrowserCalls) -> str:
    """One line naming the browser this session drives (+ the others, if any)."""
    live = calls.connected_browsers()
    if not live:
        return "Browsers: none connected." + NO_BROWSER_GUIDANCE
    active, routing_error = calls.target_browser()
    roster = ", ".join(f"[{b.slot}] {b.name}" for b in live)
    if len(live) == 1:
        return (f"Browsers: 1 connected — {roster} (your calls go here). Connect a "
                "second browser and a `browsers` tool appears for switching between them.")
    mine = active.describe() if active is not None else f"NOTHING — {routing_error}"
    return (f"Browsers: {len(live)} connected — {roster}. Your calls go to: {mine}. "
            'Switch with the `browsers` tool (action="use").')


def switch_aftermath(previous, ahead) -> str:
    """What a switch did NOT move: the calls that reached the server before it ran in
    the previous browser, and the ids they returned belong there. `ahead` names the ones
    still running when the switch arrived (call_order.calls_ahead_of_switch)."""
    where = previous.describe() if previous is not None else "the previous browser"
    if ahead:
        return (f"Calls that reached the server before this one ran in {where}, among them "
                f"{', '.join(ahead)}: repeat them now if they were meant for this browser. Tab "
                "ids and page_snapshot refs from there are NOT valid here; re-run "
                "page_snapshot before acting.")
    return (f"Calls that reached the server before this one ran in {where}. Tab ids and "
            "page_snapshot refs from there are NOT valid here; re-run tabs (and "
            "page_snapshot) before acting.")


def make_browsers_tool(calls: BrowserCalls) -> Callable[..., Any]:
    """Build the `browsers` tool function. Its docstring is never shown: the description
    is built from the live roster by `tool_description`."""

    async def browsers(
        action: Annotated[str, Field(description=(
            "list: the live roster, with each browser's version, connect time and last "
            "use. use: switch this session to target"
        ))] = "list",
        target: Annotated[Optional[Union[str, int]], Field(description=(
            "use: a slot number from the roster such as 2, or a case-insensitive "
            "substring of a browser's name such as zen"
        ))] = None
    ) -> str:
        """List the connected browsers, or switch which one this session drives.

        (This docstring is never shown: refresh_browsers_tool registers the tool with
        a description built from the LIVE roster; see _browsers_tool_description.)
        """
        live = calls.connected_browsers()
        if not live:
            return fail("No browser is connected to the server." + NO_BROWSER_GUIDANCE,
                        code=EXTENSION_DISCONNECTED)

        active, routing_error = calls.target_browser()
        default = calls.preferred_browser(live)

        if action == "list":
            lines = [f"{len(live)} browser(s) connected:"]
            lines += [browser_line(b, active=active, default=default,
                                         with_activity=True) for b in live]
            if routing_error:
                # Selected a browser that has since gone: say so HERE, where the agent
                # can see the survivors and fix it in one `use` call.
                lines += ["", routing_error]
                degrade("selection_lost", tool="browsers")
            return "\n".join(lines)

        if action == "use":
            if target is None or not str(target).strip():
                roster = "\n".join(browser_line(b, active=active, default=default)
                                   for b in live)
                return invalid('browsers(action="use") needs a `target`: a slot number or a '
                               f"name substring. Connected:\n{roster}")
            chosen, err = match_browser(target, live)
            if err:
                return invalid(err)
            previous = active
            client_key = calls.client_key()
            calls.server.browser_selection[client_key] = chosen.instance_id
            # This move is the client's own decision, so its next call must not be
            # told about it as though something had happened behind its back.
            calls.server.routing_memory.chose(client_key, chosen.instance_id)
            if previous is chosen:
                return (f"Already on {chosen.describe()}; nothing changed. "
                        "(Selection re-confirmed for this session.)")
            was = f" (was {previous.describe()})" if previous is not None else ""
            return (f"This session now drives {chosen.describe()}{was}.\n"
                    + switch_aftermath(previous, calls_ahead_of_switch()) + "\n"
                    "Only this MCP session moved; other clients on this server keep "
                    "the browser they were on.")

        return invalid(f'Unknown action "{action}". Use action="list" (the roster) or '
                       'action="use" with target=<slot number or name substring>.')

    return browsers
