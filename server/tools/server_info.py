"""The `server_info` tool: is BrowserPlug working, and what is wrong if not.

`make_server_info_tool(calls)` returns the tool function; its signature and docstring are
pinned by `tests/unit/golden/tools_list.json`.
"""

import asyncio
import json
from typing import Any, Callable

from server import preflight
from server.reporting import reports

from server.tools.browser_calls import BrowserCalls
from server.tools.browsers import browsers_summary


def make_server_info_tool(calls: BrowserCalls) -> Callable[..., Any]:
    """Build the `server_info` tool function over `calls`."""

    async def server_info(verbose: bool = False) -> str:
        """Is BrowserPlug working? Reports the connected browsers, which one this session drives, the MCP clients on the shared server, the request-queue depth and limit, and any failed precondition with its remedy. Call it first when page tools get no response. For file:// tabs, extension_file_access.can_read_opened_file_tab says whether page_* tools can read one the user has open (a per-user toggle); can_open_file_url says whether BrowserPlug can open one: never on Firefox, on Chromium once that toggle is on

        Args:
            verbose: add the full connection dump; large, and the summary has everything diagnostic
        """
        try:
            routed = calls.routed_browser()
            if routed is not None:
                ext, version = routed.ws, routed.version
            else:
                ext, version = calls.server.extension_connection, calls.server.extension_version
            connected = ext is not None and getattr(ext, "close_code", None) is None
            status = (f"Extension {'connected' if connected else 'NOT connected'} "
                      f"(version: {version or 'unknown'})")
            if routed is not None and routed.file_access is not None:
                fa = reports.file_access_report(routed, routed.file_access)
                status += ("\nextension_file_access: can_read_opened_file_tab: "
                           f"{json.dumps(fa['can_read_opened_file_tab'])}, can_open_file_url: "
                           f"{json.dumps(fa['can_open_file_url'])}")
        except Exception as e:
            status = f"Extension status check failed: {e}"
        # The browser roster. With ONE Firefox the `browsers` tool does not exist, so
        # this line is the only place an agent can learn that several are supported.
        try:
            status += "\n" + browsers_summary(calls)
        except Exception as e:
            status += f"\n(browser roster unavailable: {e})"
        try:
            # Off the loop: the report walks the TCP table and the process list, and
            # every other client's request shares this loop.
            info = await asyncio.to_thread(calls.server.get_connections_info)
            # Version skew between the server, the launchers and the extensions.
            # Hoisted out of the JSON dump and onto its own line: it is the one
            # field here that asks the reader to DO something (see version_check.py).
            warning = info.get("version_warning")
            if warning:
                status += f"\nVersion mismatch: {warning}"
            if verbose:
                return status + "\n" + json.dumps(info, indent=1, default=str)
            # This payload had a MEDIAN of 10,047 bytes and a max of
            # 21 KB; a pre-flight health check returning more than a typical page read,
            # nearly all of it a connection dump nobody read. Default to the summary and
            # keep the full thing one argument away. When something is actually WRONG,
            # lead with the failing precondition and its remedy, because that is the
            # question being asked; probe_server=False because we ARE the server.
            checks = await asyncio.to_thread(preflight.run_checks, probe_server=False)
            head = (preflight.render(checks) + "\n\n"
                    if any(c.get("ok") is False for c in checks) else "")
            return (head + status + "\n"
                    + json.dumps(reports.summarize_connections(info),
                                 indent=1, default=str))
        except Exception as e:
            return status + f"\n(connection details unavailable: {e})"

    return server_info
