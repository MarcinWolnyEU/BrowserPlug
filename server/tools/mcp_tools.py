#!/usr/bin/env python3
"""
MCP Tool definitions for BrowserPlug server
These tools bridge browser functions through WebSocket to the Firefox extension.

Tool surface (9 tools, each multi-purpose; kept deliberately small so the
schema overhead in the client's context stays low):

  tabs            - tab AND window management (list/open/close/activate/window);
                    gains routing + per-tab SOCKS proxy on "open" when the
                    extension's optional "proxy" permission is granted
  navigate        - goto/back/forward/reload with settle-by-default; read= ALSO
                    returns the page, and a landing that is not the URL asked
                    for is named (gains an opt-in proxy arg when granted)
  page_read       - page content as markdown (default) / text / html, paginated;
                    url= navigates first, so a read costs one call not two
  page_snapshot   - interactive-element map (refs) + forms and records modes
  page_act        - batched interaction steps, each reporting what it CHANGED
  page_eval       - arbitrary JS escape hatch (isolated or main world)
  page_screenshot - viewable inline image, full-page stitching
  requests        - the headers/bodies a page sends (cookies, auth) + a rolling,
                    retroactive request log
  server_info     - is it working: browsers, clients, queue depth, what failed

  browsers        - 10th tool, registered ONLY while >=2 browsers are connected:
                    list them and switch which one THIS MCP client drives. With one
                    browser connected the surface is exactly the 9 above.

Conventions:
  - tab_id is OPTIONAL everywhere: omitted = the active tab of the focused
    window. A stale tab id fails with a hint to open the agent's own tab, never one
    pointing at the user's active tab (response_hints.stale_tab_text).
  - Perception tools report when a page is unpainted (window unfocused) or
    CSS-hidden instead of silently looking empty.
  - Element targeting: a `target` string is tried as a snapshot ref ("e5"),
    then a CSS selector, then an accessible-name text match.
  - A FAILURE is flagged at the MCP protocol level (see server/tools/tool_outcome.py),
    not merely by returning text that starts with "Error". A degraded SUCCESS
    (truncated, empty, unsettled) stays a success and carries a marker.
  - The total description budget is measured and must not grow: these strings
    are re-transmitted on every tools/list. Steer from the RESPONSE instead
    (server/tools/response_hints.py); plan-time advocacy was measured to be inert.

Layout:
  This module only registers. Each tool is built by a module in server/tools/
  (tabs, navigate, page, requests, browsers, server_info) from a BrowserCalls, which
  wraps the one ServerFacade (server/tools/server_facade.py) the tools may use. What is
  registered here is what changes while the server runs: the proxy argument on
  tabs/navigate, page_eval's script arguments, and the `browsers` tool. The table is a
  tool_registry.ToolRegistry; the MCP server that answers from it is mcp_server.py.

Copyright 2026 Marcin Wolny
Licensed under the MIT License - see LICENSE file for details
"""

import hashlib
import logging
import os
from typing import Optional

# A tool still RETURNS its formatted string; the whole act-timeout report is only useful
# in full, and every existing assertion reads that text. fail()/invalid() hand the
# message straight back while recording what it MEANS, and the server's outcome policy
# (tool_outcome.ToolOutcomePolicy, run by mcp_server.py) turns a recorded failure into a
# protocol-level error after the call. Before this, 648 of 2,903 corpus calls failed and
# is_error was set on 19; a harness cannot retry what it cannot see.
from server.tools.mcp_server import BrowserPlugMCPServer

# The tool table. Gated args (proxy, script_name/script_args) are `hidden`: pruned from
# the advertised schema only, still accepted on a call.
from server.tools.tool_registry import ToolRegistry, ToolSpec

# Cross-tool workflow guidance, delivered once at initialize rather than on every
# tools/list. Its own module so the wording is reviewable and its byte cost stays visible.
from server.tools.instructions import instructions as server_instructions

from server.tools.server_facade import ServerFacade

from server.tools import browsers as browsers_tool
from server.tools import call_order
from server.tools import navigate as navigate_tool
from server.tools import page as page_tool
from server.tools import requests as requests_tool
from server.tools import server_info as server_info_tool
from server.tools import tabs as tabs_tool
from server.tools.browser_calls import BrowserCalls
from server.tools.docstrings import parse_function_docstring

logger = logging.getLogger(__name__)


def _base_description(fn) -> str:
    """The description a tool registered with an explicit one starts from: its
    docstring's first text section. The `Args:` entries already become the parameter
    descriptions, and every tools/list would otherwise carry them twice."""
    return parse_function_docstring(fn).description or ""


class BrowserPlugTools:
    """Builds the MCP server and keeps its tool surface current."""

    def __init__(self, websocket_server: ServerFacade):
        """Initialize with reference to WebSocket server"""
        self.websocket_server = websocket_server
        # Every tool reaches the server through this one object, and it only touches the
        # ServerFacade members.
        self.calls = BrowserCalls(websocket_server)
        # The server used to declare a NAME and nothing else, so the whole
        # self-description a client could see was the tool list; the one channel that is
        # truncated by one client and re-transmitted 780 times by another. Instructions ride
        # the initialize handshake instead: sent once, and the only place cross-tool
        # workflow can be stated at all.
        # registry.put replaces a tool IN PLACE, in one step: the refreshes below run on the
        # WebSocket thread while uvicorn serves list/call, and remove-then-add left a gap
        # in which a call got "Unknown tool" (and moved the tool to the end of the list).
        self.registry = ToolRegistry()
        # A client's browser switch and its other calls take effect in arrival order.
        self.call_order = call_order.CallOrder()
        # The server turns a recorded failure into a protocol-level error, and a degraded
        # success into a machine-readable marker, AFTER the tool has formatted its text.
        # `record` also files the structured verdict on the current MCP call, because an
        # MCP error result carries no _meta; that is the only route to the operator.
        self.mcp = BrowserPlugMCPServer(self.registry, instructions=server_instructions(),
                                        record=self.calls.record_outcome,
                                        signature=lambda: self.tools_signature,
                                        call_gate=self.call_gate)
        # Whether the `proxy` param + its description are exposed on tabs/navigate.
        # Driven by the extension's optional "proxy" permission (see
        # refresh_proxy_tool_visibility); starts hidden until the extension
        # reports the grant. `tools_signature` changes whenever this flips, which
        # pushes tools/list_changed to connected clients (list_changed.py).
        self._proxy_tools_enabled = False
        self.tools_signature = None
        # Set by _setup_browser_tools so proxy visibility can be re-applied later.
        self._tabs_fn = None
        self._navigate_fn = None
        self._tabs_desc = ""
        self._nav_desc = ""
        # The `browsers` tool exists ONLY while >=2 Firefoxes are connected (a
        # single-browser install keeps today's 9-tool surface byte for byte). Its
        # description embeds the live roster, so it is re-registered on every
        # connect/disconnect; see refresh_browsers_tool. None == not registered.
        self._browsers_fn = None
        self._browsers_desc = None
        self._setup_tools()

    def call_gate(self, name, arguments):
        """The ordering context one tools/call runs in (call_order.py). Keyed like the
        browser selection, so it orders exactly the calls a switch can redirect. Only
        `browsers(action="use")` switches; `list` reads and waits like any other call."""
        action = str((arguments or {}).get("action") or "").strip().lower()
        return self.call_order.admitted(
            self.calls.client_key(), switches=(name == "browsers" and action == "use"),
            label=call_order.call_label(name, arguments))

    def _setup_tools(self):
        """Set up all MCP tool definitions"""
        self._setup_browser_tools()
        self._setup_page_tools()
        self._setup_data_tools()
        self._setup_server_tools()

    # =========================================================================
    # Browser-level tools: tabs (incl. windows), navigate
    # =========================================================================

    def _setup_browser_tools(self):
        # Register tabs + navigate with proxy visibility applied. Kept as
        # function refs so the pair can be re-registered when the extension's
        # optional "proxy" permission is granted/revoked.
        self._tabs_fn = tabs_tool.make_tabs_tool(self.calls)
        self._navigate_fn = navigate_tool.make_navigate_tool(self.calls)
        self._register_browser_proxy_tools()

    # ---- proxy tool visibility ----------------------------------------------
    # The `proxy` argument on tabs/navigate is gated on the connected extension
    # holding the optional "proxy" WebExtension permission. When it isn't granted
    # the arg, and with it every proxy mention, is dropped from the tool schema
    # (ToolSpec `hidden`), so a client never sees routing it can't use. Visibility is refreshed from the extension_hello state; a change
    # bumps tools_signature (exposed on /health), which pushes tools/list_changed to
    # connected clients (list_changed.py).

    def proxy_tools_enabled(self) -> bool:
        """Whether tabs/navigate currently expose the `proxy` argument (i.e. the
        connected extension reports the optional "proxy" permission is granted)."""
        return self._proxy_tools_enabled

    def refresh_proxy_tool_visibility(self) -> bool:
        """(Re)apply proxy visibility from the extension's reported permission.

        Idempotent: re-registers tabs/navigate (and bumps tools_signature) only
        when the derived on/off state actually changes, so it's safe to call on
        every extension_hello. Returns True if the visibility changed."""
        want = self.calls.proxy_permission_granted()
        if want == self._proxy_tools_enabled and self.tools_signature is not None:
            return False
        self._proxy_tools_enabled = want
        self._register_browser_proxy_tools()
        return True

    def _register_browser_proxy_tools(self):
        """Register (or replace) tabs + navigate with the current proxy visibility."""
        if self._tabs_fn is None or self._navigate_fn is None:
            return
        enabled = self._proxy_tools_enabled
        # The proxy text lives in the `proxy` parameter's own description, so it comes and
        # goes with the parameter and never sits in front of the calls everyone makes.
        tabs_desc = _base_description(self._tabs_fn)
        nav_desc = _base_description(self._navigate_fn)
        hidden = () if enabled else ("proxy",)
        # Schema-only hide: the fn keeps accepting proxy, so direct calls and the
        # wire path are unchanged. No remove first: the tool is replaced in place.
        self.registry.put(ToolSpec.from_function(self._tabs_fn, name="tabs",
                                                 description=tabs_desc, hidden=hidden))
        self.registry.put(ToolSpec.from_function(self._navigate_fn, name="navigate",
                                                 description=nav_desc, hidden=hidden))
        self._tabs_desc, self._nav_desc = tabs_desc, nav_desc
        self._recompute_tools_signature()

    def _recompute_tools_signature(self):
        """The server pushes tools/list_changed when this moves (list_changed.py), and the
        launchers poll /health for (server_pid, tools_signature) to announce it to their
        own clients, which hold no GET stream. So EVERYTHING that can reshape the surface
        while the server runs must be hashed here: the proxy arg on tabs/navigate, and the
        `browsers` tool's presence AND its description (which embeds the live roster; a
        browser connecting or leaving changes it, and clients need the new one).
        page_eval's script_name/script_args gate is deliberately absent: it is read from
        the environment once, at startup, so changing it takes a restart, and a restart
        already moves server_pid."""
        self.tools_signature = self._compute_tools_signature(
            self._tabs_desc, self._nav_desc, self._proxy_tools_enabled, self._browsers_desc)
        self.mcp.list_changed.surface_changed()

    @staticmethod
    def _compute_tools_signature(tabs_desc: str, nav_desc: str, enabled: bool,
                                 browsers_desc: Optional[str] = None) -> str:
        h = hashlib.sha1()
        h.update(b"1" if enabled else b"0")
        h.update(tabs_desc.encode("utf-8", "replace"))
        h.update(nav_desc.encode("utf-8", "replace"))
        h.update(b"\x00browsers\x00")
        h.update((browsers_desc or "").encode("utf-8", "replace"))
        return h.hexdigest()[:12]

    # =========================================================================
    # Page tools: read, snapshot, act, eval, screenshot
    # =========================================================================

    def _setup_page_tools(self):
        # Name from the function, description from its docstring (the text before `Args:`).
        self.registry.put(ToolSpec.from_function(
            page_tool.make_page_read_tool(self.calls, self._navigate_fn)))
        self.registry.put(ToolSpec.from_function(page_tool.make_page_snapshot_tool(self.calls)))
        self.registry.put(ToolSpec.from_function(page_tool.make_page_act_tool(self.calls)))

        page_eval = page_tool.make_page_eval_tool(self.calls)
        # The predefined-script mode ERRORS unless $BROWSERPLUG_EXT_SCRIPTS is set in the
        # SERVER's environment, and script_name/script_args were passed 0 times in 209 calls.
        # Every install without that variable was paying ~90 words for a feature it cannot
        # use, on all 780 tool listings. Gate the args AND their prose on availability;
        # the same conditional-registration mechanism the proxy documentation already uses.
        scripts_on = bool(os.environ.get("BROWSERPLUG_EXT_SCRIPTS"))
        self.registry.put(ToolSpec.from_function(
            page_eval, name="page_eval",
            description=_base_description(page_eval) + (
                page_tool.PAGE_EVAL_SCRIPTS_DOC if scripts_on else ""),
            hidden=() if scripts_on else ("script_name", "script_args")))

        self.registry.put(ToolSpec.from_function(
            page_tool.make_page_screenshot_tool(self.calls)))

    # =========================================================================
    # Data tools: requests
    # =========================================================================

    def _setup_data_tools(self):
        self.registry.put(ToolSpec.from_function(requests_tool.make_requests_tool(self.calls)))

    # =========================================================================
    # Browser selection: the `browsers` tool (only while >=2 are connected)
    # =========================================================================

    def refresh_browsers_tool(self) -> bool:
        """(Re)apply the `browsers` tool to the current registry: registered only while
        >=2 Firefoxes are connected, with the live roster in its description.

        Idempotent; a no-op unless the rendered description actually changed, so the
        extension's periodic hello causes no churn. Returns True if the surface changed
        (which also flips tools_signature, and so pushes tools/list_changed)."""
        desc = browsers_tool.tool_description(self.calls)
        if desc == self._browsers_desc:
            return False
        self._browsers_desc = desc
        # Same primitive as the proxy-arg gate: a new description replaces the tool in one
        # step; only "no longer needed" removes it (and a re-added one goes last).
        if desc is not None and self._browsers_fn is not None:
            self.registry.put(ToolSpec.from_function(self._browsers_fn, name="browsers",
                                                     description=desc))
        else:
            self.registry.remove("browsers")
        self._recompute_tools_signature()
        return True

    # =========================================================================
    # Server diagnostics
    # =========================================================================

    def _setup_server_tools(self):
        self._browsers_fn = browsers_tool.make_browsers_tool(self.calls)
        # Registered lazily: it must not exist while a single Firefox is connected.
        self.refresh_browsers_tool()

        self.registry.put(ToolSpec.from_function(server_info_tool.make_server_info_tool(self.calls)))

    def get_mcp_app(self):
        """The MCP server (mcp_server.BrowserPlugMCPServer, the SDK's low-level Server)."""
        return self.mcp
