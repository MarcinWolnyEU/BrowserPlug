"""The `tabs` tool: list, find, open, close, activate, window, routing.

`make_tabs_tool(calls)` returns the tool function. The tool registry reads the name, the
schema and the description from it, so its signature and docstring are the agent-facing
contract and are pinned by `tests/unit/golden/tools_list.json`. The `proxy` parameter
is pruned from the schema by `mcp_tools.py` unless a connected extension holds the
optional proxy permission; everything about proxies lives in that parameter's own
description, so the tool description stays about the calls nearly everyone makes.

Parameter order is part of the contract: `url` comes straight after `action`. A model
that writes arguments in schema order then writes `url` before the optional ones; the
eval that found a weak model losing `url` saw it go missing after a run of those.
"""

from datetime import datetime
from typing import Annotated, Any, Callable, Dict, List, Optional, TypedDict

from pydantic import Field

from server.tools.tool_outcome import degrade, fail, invalid

from server.tools.browser_calls import BrowserCalls, proxy_error
from server.tools import browser_proxy, tor_proxy
from server.tools.formatting import fmt_last_access
from server.tools.response_hints import incognito_hint, missing_url_text


class TabInfo(TypedDict, total=False):
    """Type definition for tab information from browser extension"""
    url: str
    id: int
    title: str
    active: bool
    windowId: int
    pinned: bool
    index: int
    status: str
    discarded: bool
    hidden: bool
    audible: bool
    muted: bool
    isInReaderMode: bool
    lastAccessed: int
    proxy: str


# Hard ceiling on how many tabs one `tabs(recent=false)` may render. Sized from the
# observed overflow: ~200 bytes a tab, so 120 keeps the worst case near 25 KB; comfortably
# inside a client's per-result limit, where 474 tabs at 97,172 characters was not.
TABS_FULL_PAGE = 120

def format_tab_list(rdata: Dict[str, Any], recent: bool) -> str:
    """Render a `tabs.list` response: windows, then tabs, recent ones first."""
    tab_list: List[TabInfo] = rdata.get("tabs", [])
    if not tab_list:
        return "No tabs found"
    windows = {w["id"]: w for w in rdata.get("windows", [])}
    # Which window to flag: the currently-focused one if Firefox is the
    # frontmost app, else the most-recently-focused Firefox window
    # (tracked by the extension); so the marker still points somewhere
    # useful when you're driving from the terminal and Firefox is behind.
    any_focused = any(w.get("focused") for w in windows.values())
    last_focused_wid = rdata.get("lastFocusedWindowId")
    now_ms = datetime.now().timestamp() * 1000

    # recent=True (default): show the more inclusive of the 5
    # most-recently-accessed tabs OR every tab touched in the last
    # 15 min (chosen globally, then grouped by window). shown_ids
    # None => show everything. Degrade to "show all" if the browser
    # reported no lastAccessed at all (e.g. an older extension build).
    shown_ids: Optional[set] = None
    if recent and any(t.get("lastAccessed") for t in tab_list):
        RECENT_MIN, RECENT_MS = 5, 15 * 60 * 1000
        ordered = sorted(tab_list, key=lambda t: t.get("lastAccessed") or 0,
                         reverse=True)
        within = sum(1 for t in ordered
                     if (t.get("lastAccessed") or 0) >= now_ms - RECENT_MS)
        keep = max(RECENT_MIN, within)
        shown_ids = {t.get("id") for t in ordered[:keep]}

    # recent=false is the documented escape hatch, and on a real profile
    # (474 background tabs) it produced 97,172 characters and EXCEEDED the
    # client's token limit outright; leaving exactly two states, "almost
    # nothing" and "too much to return", with nothing in between. Page it. The
    # full list stays reachable, one window of it at a time.
    page_note = ""
    if shown_ids is None and len(tab_list) > TABS_FULL_PAGE:
        ordered = sorted(tab_list, key=lambda t: t.get("lastAccessed") or 0,
                         reverse=True)
        window_slice = ordered[:TABS_FULL_PAGE]
        shown_ids = {t.get("id") for t in window_slice}
        rest = len(tab_list) - len(window_slice)
        page_note = (
            f"Showing the {len(window_slice)} most recently used of "
            f"{len(tab_list)} open tabs; {rest} not shown. The full list does "
            f'not fit in one response. To find a specific tab use '
            f'tabs(action="find", url="<url or title substring>"); that is the '
            f"primitive for this and it costs a couple of hundred bytes.")

    by_window: Dict[int, List[TabInfo]] = {}
    for t in tab_list:
        by_window.setdefault(t.get("windowId"), []).append(t)

    lines: List[str] = []
    hidden_windows: List[tuple] = []
    for wid, wtabs in by_window.items():
        wtabs = sorted(wtabs, key=lambda t: t.get("index", 0))
        if shown_ids is None:
            shown, hidden = wtabs, []
        else:
            shown = [t for t in wtabs if t.get("id") in shown_ids]
            hidden = [t for t in wtabs if t.get("id") not in shown_ids]
        if not shown:
            hidden_windows.append((wid, len(wtabs)))
            continue
        w = windows.get(wid, {})
        hbits = ""
        if w.get("focused"):
            hbits += " focused"
        elif not any_focused and wid == last_focused_wid:
            hbits += " lastFocused"
        if w.get("incognito"):
            hbits += " incognito"
        if w.get("type") and w.get("type") != "normal":
            hbits += f" {w['type']}"
        if w.get("state") and w.get("state") != "normal":
            hbits += f" {w['state']}"
        lines.append(f"# Window {wid}{hbits}")
        if hidden:
            n = len(hidden)
            noun = "tab" if n == 1 else "tabs"
            lines.append(f"_{n} older {noun} hidden; recent view "
                         f"(pass recent=false to show all)_")
        for t in shown:
            flags = ""
            if t.get("active"):
                flags += " active"
            if t.get("status") == "loading":
                flags += " loading"
            if t.get("pinned"):
                flags += " pinned"
            if t.get("muted"):
                flags += " muted"
            elif t.get("audible"):
                flags += " audible"
            if t.get("discarded"):
                flags += " discarded"
            if t.get("hidden"):
                flags += " hidden"
            if t.get("isInReaderMode"):
                flags += " readerMode"
            if t.get("proxy"):
                flags += f" proxied:{t['proxy']}"
            lines.append(f"## {t.get('title') or 'No title'}")
            lines.append(f"id: {t.get('id')}{flags}")
            lines.append(f"url: {t.get('url', 'No URL')}")
            lines.append(
                f"lastAccess: {fmt_last_access(t.get('lastAccessed'), now_ms)}")
            lines.append("---")

    if hidden_windows:
        parts = ", ".join(f"Window {wid} ({n} tab{'s' if n != 1 else ''})"
                          for wid, n in hidden_windows)
        lines.append(f"Windows hidden — no recent tabs: {parts}")
    if page_note:
        lines.append(page_note)
        degrade("truncated", tool="tabs", returned=len(shown_ids or ()),
                total=len(tab_list))

    return "\n".join(lines) if lines else "No tabs found"


def make_tabs_tool(calls: BrowserCalls) -> Callable[..., Any]:
    """Build the `tabs` tool function over `calls`."""

    async def tabs(
        action: Annotated[str, Field(description=(
            "list: open tabs grouped by window. "
            "active: the tab page_* tools use when tab_id is omitted. "
            "find: tabs whose URL or title contains url. "
            "open: load url in a new tab or window. "
            "close: a tab, or a whole window via window_id. "
            "activate: bring tab_id to the front. "
            "window: move, resize, minimize or maximize a window. "
            "routing: report a tab's proxy"
        ))] = "list",
        background: Annotated[bool, Field(description=(
            "open only. Leave the user on their current tab; page_* tools drive a "
            "background tab by its id"
        ))] = False,
        recent: Annotated[bool, Field(description=(
            "list only. true: the recently used tabs plus a count of the rest; false: "
            "every open tab"
        ))] = True,
        tab_id: Annotated[Optional[int], Field(description=(
            "tab id from list, find or active, for activate, close and routing; routing "
            "defaults to the active tab"
        ))] = None,
        window_id: Annotated[Optional[int], Field(description=(
            "window id from list. close: the whole window instead of one tab. window: the "
            "window to change, default the focused one. open: the window for the new tab"
        ))] = None,
        new_window: Annotated[bool, Field(description=(
            "open only. A new window instead of a tab, sharing the profile's cookies and "
            "logins"
        ))] = False,
        incognito: Annotated[bool, Field(description=(
            "open only. A private window, without the profile's cookies or logins"
        ))] = False,
        pinned: Annotated[bool, Field(description=(
            "open only. Pin the new tab"
        ))] = False,
        focus_window: Annotated[bool, Field(description=(
            "activate and window. true raises the window; false acts without taking "
            "focus, but an unfocused or minimized window is not painted and screenshots "
            "blank"
        ))] = True,
        state: Annotated[Optional[str], Field(description=(
            "window only: normal, minimized, maximized or fullscreen"
        ))] = None,
        width: Annotated[Optional[int], Field(description=(
            "pixels: the new width for window, or the initial width of a new_window or "
            "incognito window; ignored when maximized or fullscreen"
        ))] = None,
        height: Annotated[Optional[int], Field(description=(
            "pixels, as width"
        ))] = None,
        proxy: Annotated[Optional[str], Field(description=(
            "only when the user asks for a proxy; omit it otherwise. open (normal tabs): "
            "route the new tab through tor (the built-in Tor client; first use downloads "
            "it, allow a minute) or a socks5, socks5h, socks4 or socks4a URL with "
            "host:port such as socks5://127.0.0.1:9050. NOT supported: HTTP, HTTPS, PAC, "
            "authenticated or system proxies. DNS resolves at the proxy, so .onion hosts "
            "load; fail-closed if unreachable. Chromium has no per-tab routing: it "
            "switches the WHOLE browser, the user's tabs included, until cleared. "
            'routing: only "", which clears it. list marks proxied tabs'
        ))] = None,
        url: Annotated[Optional[str], Field(description=(
            "required for open and find. open: the address to load, with scheme. find: a "
            "case-insensitive literal substring of a tab's URL or title; one distinctive "
            "word beats a phrase"
        ))] = None
    ) -> str:
        """Open, find, close and switch the user's browser tabs and windows. Open your own tab with tabs(action="open", background=true, url="https://…")"""
        if action == "list":
            response = await calls.request("tabs.list", {})
            err = calls.error_of(response)
            if err:
                return fail(f"Error listing tabs: {err}")
            wide = response["data"].get("browserProxy")
            if wide:
                # Chromium has one proxy for the whole browser, so every tab is proxied.
                for tab in response["data"].get("tabs", []):
                    tab.setdefault("proxy", wide.get("endpoint"))
            listing = format_tab_list(response["data"], recent)
            state = browser_proxy.state_line(calls.target_browser_name(),
                                             response["data"].get("browserProxy"))
            return (state + "\n\n" + listing) if state else listing

        elif action == "active":
            response = await calls.request("tabs.active", {})
            err = calls.error_of(response)
            if err:
                return fail(f"Error getting active tab: {err}")
            t = response["data"].get("tab") or {}
            if t.get("id") is None:
                return "No active tab found"
            return (f"Active tab [{t.get('id')}] in window {t.get('windowId')}: "
                    f"{t.get('title') or 'No title'}\nurl: {t.get('url', 'No URL')}")

        elif action == "find":
            if not url:
                return invalid(missing_url_text("tabs", "find"))
            response = await calls.request("tabs.list", {})
            err = calls.error_of(response)
            if err:
                return fail(f"Error listing tabs: {err}")
            tab_list = response["data"].get("tabs", [])
            needle = url.lower()
            matches = [t for t in tab_list
                       if needle in (t.get("url") or "").lower()
                       or needle in (t.get("title") or "").lower()]
            if not matches:
                return (f'No open tab matches "{url}" (searched url + title of '
                        f'{len(tab_list)} tabs)')
            matches.sort(key=lambda t: t.get("lastAccessed") or 0, reverse=True)
            n = len(matches)
            lines = [f'{n} tab{"s" if n != 1 else ""} match "{url}"'
                     + (" (showing the 10 most recently used)" if n > 10 else "")
                     + ":"]
            for t in matches[:10]:
                flags = " active" if t.get("active") else ""
                lines.append(f"[{t.get('id')}]{flags} window {t.get('windowId')}: "
                             f"{t.get('title') or 'No title'} — {t.get('url', 'No URL')}")
            return "\n".join(lines)

        elif action == "open":
            if not url:
                return invalid(missing_url_text("tabs", "open"))
            perr = proxy_error(proxy)
            if perr:
                return fail(f"Error: {perr}")
            cerr = calls.proxy_capability_error(proxy)
            if cerr:
                return fail(f"Error: {cerr}")
            if new_window or incognito:
                if proxy:
                    return (invalid('Error: proxy routing is supported on a normal tab open '
                            "(omit new_window/incognito); per-tab SOCKS routing works on any "
                            "tab, including one that inherits the profile's session."))
                data = {"url": url, "incognito": incognito, "focused": not background}
                if width: data["width"] = width
                if height: data["height"] = height
                response = await calls.request("windows.create", data)
                err = calls.error_of(response)
                if err:
                    return fail(f"Error opening window: {err}{incognito_hint(err)}")
                w = response["data"].get("window", {})
                wtabs = w.get("tabs") or []
                wtab = wtabs[0] if wtabs else {}
                tid = wtab.get("id", "?")
                # Report the tab's REAL url (post-commit, redirects followed), not
                # the requested one; identical honesty to the new-tab path below.
                owner = calls.owner_label()
                line = (f"Opened {'incognito ' if incognito else ''}window {w.get('id')} "
                        + (f"in {owner} " if owner else "")
                        + f"with tab [{tid}]: {wtab.get('url') or url}")
                if w.get("navPending"):
                    if w.get("navError"):
                        line += (f"\n  WARNING: navigation to {w['navPending']} failed: {w['navError']} "
                                 f"(tab is on {wtab.get('url')})")
                        return fail(line)
                    elif w.get("navTimedOut"):
                        line += (f"\n  NOTE: navigation to {w['navPending']} did not commit within 8s "
                                 f"(still loading or unreachable); tab is on {wtab.get('url')}")
                        degrade("nav_uncommitted", tool="tabs", pending=w["navPending"])
                return line
            via_tor = tor_proxy.is_tor(proxy)
            refused = await tor_proxy.refuse_before_start(calls, proxy)
            if refused:
                return refused
            proxy, terr = await tor_proxy.resolve(proxy)
            if terr:
                return fail(f"Error: {terr}")
            data = {"url": url, "active": not background, "pinned": pinned}
            if window_id:
                data["windowId"] = window_id
            if proxy is not None:
                data["proxy"] = proxy
            response = await calls.request("tabs.create", data)
            err = calls.error_of(response)
            if err:
                return fail(f"Error opening tab: {err}")
            t = response["data"].get("tab", {})
            # With several browsers connected the result names the one that owns the id:
            # a tab opened in the browser the agent did not mean is visible at once.
            owner = calls.owner_label()
            line = (f"Opened tab [{t.get('id')}] in " + (f"{owner}, " if owner else "")
                    + f"window {t.get('windowId')}: {t.get('url', url)}")
            if t.get("proxy"):
                line += f"  (routed via {t['proxy']}; DNS at proxy, fail-closed)"
            # Said last, whatever the navigation did: the browser WAS switched (Chromium).
            notice = browser_proxy.change_notice(
                calls.target_browser_name(), response["data"].get("browserProxyChange"),
                via_tor=via_tor)
            if t.get("navPending"):
                if t.get("navError"):
                    line += (f"\n  WARNING: navigation to {t['navPending']} failed: {t['navError']} "
                             f"(tab is on {t.get('url')})")
                    line += await tor_proxy.onion_hint(
                        calls, url=t["navPending"], error_text=t["navError"], proxy=proxy,
                        tab_id=None,
                        retry_call=f'tabs(action="open", url="{url}", proxy="tor")')
                    return fail(line + notice)
                elif t.get("navTimedOut"):
                    line += (f"\n  NOTE: navigation to {t['navPending']} did not commit within 8s "
                             f"(still loading or unreachable); tab is on {t.get('url')}")
                    degrade("nav_uncommitted", tool="tabs", pending=t["navPending"])
            return line + notice

        elif action == "routing":
            data = {}
            if tab_id is not None:
                data["tabId"] = tab_id
            if proxy is not None:
                # The cheap off-switch for a browser-wide proxy: nothing opens or loads.
                if not calls.target_proxy_is_browser_wide():
                    return invalid('Error: proxy on action="routing" only switches off a '
                                   "browser-wide proxy, which Chromium has. On Firefox a proxy "
                                   'belongs to one tab: set it with tabs(action="open") or '
                                   "navigate.")
                if proxy not in ("", "direct"):
                    return invalid('Error: action="routing" only accepts proxy="" or '
                                   '"direct" (switch the browser-wide proxy off). Switch it '
                                   "on with tabs(action=\"open\") or navigate.")
                data["proxy"] = proxy
            response = await calls.request("tabs.routing", data)
            err = calls.error_of(response)
            if err:
                return fail(f"Error reading routing: {err}")
            r = response["data"]
            name = calls.target_browser_name()
            if r.get("routed"):
                text = (f"Tab {r.get('tabId')} is routed via {r.get('endpoint')} "
                        f"(type={r.get('type')}, proxyDNS={r.get('proxyDNS')})")
            elif r.get("browserProxy"):
                text = f"Tab {r.get('tabId')} has no per-tab proxy"
            else:
                text = f"Tab {r.get('tabId')} is direct (no proxy)"
            text += f"\n  isProxy: {'true' if browser_proxy.is_proxied(r) else 'false'}"
            text += browser_proxy.change_notice(name, r.get("browserProxyChange"))
            state = browser_proxy.state_line(name, r.get("browserProxy"))
            return text + ("\n  " + state if state else "")

        elif action == "close":
            if window_id is not None and tab_id is None:
                response = await calls.request("windows.close", {"windowId": window_id})
                err = calls.error_of(response)
                return fail(f"Error closing window {window_id}: {err}") if err else f"Closed window {window_id}"
            if tab_id is None:
                return invalid('Error: action="close" needs tab_id (or window_id to close a window)')
            response = await calls.request("tabs.close", {"tabId": tab_id})
            err = calls.error_of(response)
            return fail(f"Error closing tab {tab_id}: {err}") if err else f"Closed tab {tab_id}"

        elif action == "activate":
            if tab_id is None:
                return invalid('Error: tab_id is required for action="activate"')
            response = await calls.request("tabs.switch", {"tabId": tab_id, "focusWindow": focus_window})
            err = calls.error_of(response)
            if err:
                return fail(f"Error activating tab {tab_id}: {err}")
            focused = response["data"].get("focusedWindow")
            return f"Activated tab {tab_id}" + (" and focused its window" if focused else "")

        elif action == "window":
            wid = window_id
            if wid is None:
                response = await calls.request("windows.get_last_focused", {"populate": False})
                err = calls.error_of(response)
                if err:
                    return fail(f"Error finding focused window: {err}")
                wid = response["data"].get("window", {}).get("id")
            data: Dict[str, Any] = {"windowId": wid}
            if state: data["state"] = state
            if width: data["width"] = width
            if height: data["height"] = height
            if focus_window: data["focused"] = True
            response = await calls.request("windows.update", data)
            err = calls.error_of(response)
            if err:
                return fail(f"Error updating window {wid}: {err}")
            w = response["data"].get("window", {}) or {}
            return (f"Window {wid} updated: state={w.get('state', state or 'normal')}, "
                    f"{w.get('width', '?')}x{w.get('height', '?')}")

        return invalid(f'Error: unknown action "{action}" (use list | active | find | open | close | activate | window | routing)')

    return tabs
