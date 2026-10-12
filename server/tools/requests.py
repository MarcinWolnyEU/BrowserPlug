"""The `requests` tool: the rolling request log and body-capturing monitors.

`make_requests_tool(calls)` returns the tool function; its signature and docstring are
pinned by `tests/unit/golden/tools_list.json`.
"""

import json
from typing import Any, Callable, Dict, List, Optional

from server.tools.tool_outcome import fail, invalid

from server.tools.browser_calls import BrowserCalls
from server.tools.formatting import compact, fmt_ms
from server.tools.validation import request_limit


# Per-body capture budget for `requests` monitor sessions. The extension keeps
# captured bodies under a bounded TOTAL byte budget with oldest-first eviction, so
# an unclamped max_body_size doesn't merely cap one body; a single oversized
# capture blows the whole budget and evicts every stored body, including the one
# just captured. Clamp it here (the extension clamps defensively too).
MAX_BODY_SIZE_DEFAULT = 50_000
MAX_BODY_SIZE_MIN = 1_000
MAX_BODY_SIZE_MAX = 1_000_000


def clamp_max_body_size(value: Any) -> int:
    """Coerce a caller's max_body_size into [1KB, 1MB]; junk falls back to the default."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return MAX_BODY_SIZE_DEFAULT
    return max(MAX_BODY_SIZE_MIN, min(MAX_BODY_SIZE_MAX, n))


def format_recent_requests(r: Dict[str, Any], filter: Optional[str]) -> str:
    """Render a `requests.recent` response."""
    reqs = r.get("requests", [])
    if not reqs:
        return (f"No requests in the log for tab {r.get('tabId')}"
                + (f" matching filter {filter!r}" if filter else "")
                + " (the rolling log covers activity since BrowserPlug 1.3 connected)")
    lines = [f"Tab {r.get('tabId')}: showing {r.get('returned', len(reqs))} of {r.get('total')} logged requests (newest last):"]
    for q in reqs:
        status = q.get("status")
        if q.get("error"):
            status = f"ERR({q['error']})"
        dur = f" ({fmt_ms(q['durMs'])})" if q.get("durMs") is not None else ""
        cache = " [cache]" if q.get("fromCache") else ""
        age = f" {fmt_ms(q['ageMs'])} ago" if q.get("ageMs") is not None else ""
        # Dialed peer address: the field that turns "pending forever" into
        # a diagnosable IPv4-vs-IPv6 split. Explicitly unknown (not silently
        # absent) on stalled/errored entries where Firefox never got far
        # enough to report a peer.
        if q.get("ip"):
            peer = f" [dialed {q['ip']}]"
        elif q.get("error") or status == "pending":
            peer = " [dialed: unknown]"
        else:
            peer = ""
        lines.append(f"- {q.get('method')} {q.get('url')} -> {status}{dur}{cache}{peer} [{q.get('type')}]{age}")
    return "\n".join(lines)


def make_requests_tool(calls: BrowserCalls) -> Callable[..., Any]:
    """Build the `requests` tool function over `calls`."""

    async def requests(
        action: str = "recent",
        tab_id: Optional[int] = None,
        filter: Optional[str] = None,
        limit: int = 30,
        url_patterns: Optional[List[str]] = None,
        monitor_id: Optional[str] = None,
        request_id: Optional[str] = None,
        options: Optional[Dict[str, Any]] = None,
        include_binary: bool = False
    ) -> str:
        """See the headers and bodies a page sends: cookies, auth headers, its own XHRs. Cookie and auth header values come back <redacted>, which still shows they were sent; bodies come back raw by default. recent: the per-tab log of the last ~300 requests (method, URL, status, timing), retroactive, no monitor needed; successful static assets are hidden; no headers or bodies, those need a monitor. start: capture url_patterns such as */api/*; returns a monitor_id that expires after options.ttl_seconds. list: captured request summaries for monitor_id. get: one capture in full; request_id must belong to that monitor. Headers appear ONLY here. stop: end the monitor. get returns request_headers, response_headers, request_body and response_body, each body as {included, content, content_type, encoding, size_bytes, truncated, note}; note says why a body is missing. request_headers == {} never means none were sent; request_headers_note says why they were not captured, usually a cache hit

        Args:
            action: recent, start, stop, list or get
            tab_id: recent: the tab whose log to read, default active tab. start: capture only this tab, default all tabs
            filter: recent: JS regex over the URL
            limit: recent: max entries
            url_patterns: start, required: glob URL patterns to capture
            monitor_id: list, get, stop: the monitor
            request_id: get: the captured request, from list
            include_binary: get: return binary bodies as base64; capture them by widening options.content_types_to_capture
            options: start overrides. ttl_seconds: monitor lifetime, default 10 minutes, 10s..1h. max_body_size: bytes per body, default 50000, clamped 1000..1000000; longer bodies are truncated. content_types_to_capture: default JSON and text. sensitive_headers: extra header names to redact; a display filter, not a capture filter. authorization, cookie, set-cookie and similar are always <redacted> and no argument reveals them
        """
        if isinstance(action, str):
            action = action.strip().lower()
        if action == "recent":
            limit, verr = request_limit(limit)
            if verr:
                return invalid(verr)
            data: Dict[str, Any] = {"limit": limit, "includeStatic": False}
            if tab_id is not None:
                data["tabId"] = tab_id
            if filter:
                data["filter"] = filter
            response = await calls.request("requests.recent", data)
            err = calls.error_of(response)
            if err:
                return fail(f"Error reading request log: {err}")
            r = response["data"]
            return format_recent_requests(r, filter)

        elif action == "start":
            if not url_patterns:
                return invalid('Error: url_patterns is required for action="start" (e.g. ["*/api/*"])')
            # Only options the extension actually reads are sent. (There is no
            # capture_request_bodies flag: request bodies come from the
            # webRequest "requestBody" spec and are always captured/returned.)
            default_options = {
                "capture_response_bodies": True,
                "max_body_size": MAX_BODY_SIZE_DEFAULT,
                "content_types_to_capture": ["application/json", "text/plain"],
                "sensitive_headers": ["Authorization", "Cookie", "Set-Cookie"]
            }
            if options:
                default_options.update(options)
            # An unclamped max_body_size can evict the extension's entire body
            # store (see clamp_max_body_size); clamp before it goes on the wire.
            default_options["max_body_size"] = clamp_max_body_size(
                default_options.get("max_body_size"))
            request_data: Dict[str, Any] = {"url_patterns": url_patterns, "options": default_options}
            if tab_id is not None:
                request_data["tab_id"] = tab_id
            response = await calls.request("requests.start_monitoring", request_data)
            err = calls.error_of(response)
            return fail(f"Error starting capture: {err}") if err else compact(response["data"])

        elif action == "stop":
            if not monitor_id:
                return invalid('Error: monitor_id is required for action="stop"')
            # No drain_timeout: the extension retires the monitor immediately
            # (there is no drain), and its captures stay readable afterwards.
            response = await calls.request("requests.stop_monitoring",
                                           {"monitor_id": monitor_id})
            err = calls.error_of(response)
            return fail(f"Error stopping capture: {err}") if err else compact(response["data"])

        elif action == "list":
            if not monitor_id:
                return invalid('Error: monitor_id is required for action="list"')
            response = await calls.request("requests.list_captured", {"monitor_id": monitor_id})
            err = calls.error_of(response)
            return fail(f"Error listing captured requests: {err}") if err else compact(response["data"])

        elif action == "get":
            if not monitor_id or not request_id:
                return invalid('Error: monitor_id and request_id are required for action="get"')
            request_data = {
                "monitor_id": monitor_id,
                "request_id": request_id,
                "include_binary": include_binary
            }
            response = await calls.request("requests.get_content", request_data)
            err = calls.error_of(response)
            return fail(f"Error getting request content: {err}") if err else json.dumps(response["data"], indent=1, default=str)

        return invalid(f'Error: unknown action "{action}" (use recent | start | stop | list | get)')

    return requests
