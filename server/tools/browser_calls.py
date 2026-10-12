"""How a tool reaches the caller's browser: routing, the request, error wording.

`BrowserCalls` holds the one `ServerFacade` the tools are typed against and everything that
turns "run this action for THIS client" into a wire request: the client key, the browser the
call lands in, the notice when that browser changes, the launch-then-retry for a dead
connection, the request envelope, and the wording of the failures a tool can hit. Each tool
module receives a `BrowserCalls` instead of a server, so the facade is the only path to the
server and `tests/unit/test_server_facade.py` can check that nothing else is used.

No module-level state. The routing rules themselves live in `routing.py` and
`routing_notice.py`; this class asks them.
"""

import logging
import re
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional


from server.tools.response_hints import no_browser_hint, stale_tab_text

from server.tools.tor_proxy import is_tor

from server.tools import browser_proxy

from server.browsers import routing

from server.tools import tool_outcome

from server.browsers import routing_notice

from server.browsers import default_browser as browser_pref

from server.tools.server_facade import ServerFacade

# The headers of the /mcp request this call arrived on (bound by HttpEndpoints).
from server.web import request_context

logger = logging.getLogger(__name__)

_REF_RE = re.compile(r'^e\d+$')

#: Wire keys that name a tab or frame of ONE browser.
_TAB_KEYS = ("tabId", "frameId")

REFUSED_AFTER_REROUTE = (
    "Not sent: this call names a tab, frame or snapshot ref, and the browser it would "
    "reach changed since your previous call, so those ids may belong to the browser you "
    "were using before. Nothing was done. Find your tab here with tabs(action=\"find\", "
    "url=...), or open a new one with tabs(action=\"open\", background=true, url=...), "
    "then repeat the call with that tab_id (and page_snapshot again for fresh refs). Only "
    "this one call is refused.")


def carries_browser_local_ids(data: Any) -> bool:
    """True when wire `data` names a tab, a frame or a snapshot ref (e5): ids that mean
    something only inside the browser that issued them."""
    if isinstance(data, dict):
        if any(data.get(k) is not None for k in _TAB_KEYS):
            return True
        return any(carries_browser_local_ids(v) for v in data.values())
    if isinstance(data, (list, tuple)):
        return any(carries_browser_local_ids(v) for v in data)
    return isinstance(data, str) and bool(_REF_RE.match(data.strip()))


_PROXY_RE = re.compile(r'^(socks5h?|socks4a?|socks)://([^\s/:]+):(\d{1,5})/?$', re.IGNORECASE)


def proxy_error(proxy: Optional[str]) -> Optional[str]:
    """None if `proxy` is unset/empty/'direct'/a valid SOCKS endpoint; else a
    human-readable error. '' and 'direct' mean "route directly" (clear routing).
    The endpoint is always caller-supplied; nothing is hard-coded; so the same
    server works on any machine."""
    if proxy in (None, "", "direct") or is_tor(proxy):
        return None
    m = _PROXY_RE.match(proxy.strip())
    if not m:
        return (f'Invalid proxy "{proxy}". Expected e.g. "socks5://127.0.0.1:9050" '
                "(schemes: socks5, socks5h, socks4, socks).")
    if not (1 <= int(m.group(3)) <= 65535):
        return f'Invalid proxy port in "{proxy}".'
    return None


class BrowserCalls:
    """The tools' view of the server: one facade, plus the routed request built on it."""

    NO_BROWSER_ERROR = "No extension connection available"

    def __init__(self, server: ServerFacade):
        self.server = server

    def record_outcome(self, tool_name: str, outcome: Optional[Dict[str, Any]],
                       call: Optional[Any] = None) -> None:
        """File this call's structured verdict where an OPERATOR can see it.

        The agent gets the failure through the protocol flag, but monitoring cannot: an MCP
        error result has no `_meta` to carry a code on. `last_command` is already served by
        /health and get_connections_info(), so the verdict rides there instead. Written on
        EVERY call, including clean ones, so the panel can never show a stale failure.

        `call` is (session id, JSON-RPC request id) as the dispatcher saw it. The verdict is
        filed on THAT call's record: with several clients in flight the newest record is
        usually someone else's.
        """
        if call is not None:
            # The header is what the ASGI wrapper keyed the record by, so it wins.
            header = request_context.header("mcp-session-id")
            if header:
                call = (header, call[1])
        self.server.record_call_outcome(tool_name, outcome, call)

    def client_key(self) -> str:
        """Identify the MCP client behind THIS tool call; the key its browser selection
        is stored under. The rule (launcher-forwarded pid, then the session's pid, then the
        session id, then "local") is routing.client_key; this reads the request headers
        for it. Any failure degrades to "local" rather than misrouting."""
        try:
            headers = request_context.current_headers()
            if headers is None:                   # not an HTTP call: stdio, in-process
                return routing.LOCAL_CLIENT_KEY
            return routing.client_key(
                headers.get("x-browserplug-client-pid"), headers.get("mcp-session-id"),
                self.server.http_clients)
        except Exception:
            pass
        return routing.LOCAL_CLIENT_KEY

    def connected_browsers(self) -> List[Any]:
        """Live browser registry; an empty list when the registry cannot answer."""
        try:
            return list(self.server.live_browsers())
        except Exception:
            return []

    def preferred_browser(self, live):
        """Which browser an UNROUTED client lands in: the roster's "server default".

        The server answers it (pinned browser when connected, else first-connected);
        reading `live[0]` here instead is how the roster came to announce the
        first-connected browser as the default while a pinned one took the calls.
        Falls back to first-connected when the server cannot answer.
        """
        fallback = live[0] if live else None
        try:
            return self.server.preferred_browser() or fallback
        except Exception:
            return fallback

    def routed_browser(self):
        """The browser this call will reach, for the legacy single-browser readouts (the
        version-skew hint, server_info's first line). Falls back to where an unrouted
        client lands; None when the server has no registry or no browser is connected."""
        try:
            browser, _err = self.target_browser()
            if browser is not None:
                return browser
            return self.server.preferred_browser()
        except Exception:
            return None

    def target_browser(self):
        """(browser, error) for the calling client. (None, None) means "this server has no
        registry" (a duck-typed test double), so routing stays on the legacy default path."""
        target = self.server.resolve_target_browser(self.client_key())
        if target is None:
            return None, None
        if isinstance(target, str):
            return None, target
        return target, None

    def preference_reason(self, target, live) -> str:
        """Why an unselected session lands in `target`: the one clause the "routing
        moved and both browsers are up" notice needs. "" when neither rule explains it."""
        try:
            settings = browser_pref.read()
            if (settings.get("mode") == browser_pref.MODE_PINNED
                    and settings.get("instance_id") == target.instance_id):
                return routing_notice.PINNED_REASON
            if live and live[0] is target:
                return routing_notice.FIRST_REASON
        except Exception:
            pass
        return ""

    def note_routing_change(self, target) -> bool:
        """Remember which browser served this call, and speak up when it CHANGED.

        This is the whole of the fix for the silent reroute: the routing itself was
        already right, and what the agent got was a correct answer from a browser it had
        never asked for. The notice is recorded here and delivered by
        ToolOutcomeMiddleware at the FRONT of the response.

        Returns True when routing moved since this client's previous call (the caller
        refuses a tab-bearing call then). Never raises. A diagnostic that can fail a tool
        call is worse than no diagnostic.
        """
        memory = self.server.routing_memory
        instance_id = getattr(target, "instance_id", None)
        if not instance_id:
            return False
        try:
            previous_id = memory.served(self.client_key(), instance_id)
            if not previous_id:
                return False                # first call, or the same browser as before
            live = self.connected_browsers()
            still_up = next((b for b in live
                             if getattr(b, "instance_id", None) == previous_id), None)
            if still_up is not None:
                text = routing_notice.notice(
                    now_serving=target.describe(), previous=still_up.describe(),
                    departed=False, connected_count=len(live),
                    reason=self.preference_reason(target, live))
            else:
                gone = self.server._departed.get(previous_id)
                text = routing_notice.notice(
                    now_serving=target.describe(),
                    previous=str(gone) if gone else
                    f"the browser that served your previous call ({previous_id})",
                    departed=True,
                    departed_hms=getattr(gone, "at_hms", None),
                    close_code=getattr(gone, "close_code", None),
                    close_reason=getattr(gone, "close_reason", None),
                    connected_count=len(live))
            routing_notice.record(text)
            return True
        except Exception as e:
            logger.debug("Could not build a routing-change notice: %s", e)
            return True                     # memory did report a change

    async def retry_after_launching_browser(self, routing_error: str):
        """Nothing is connected: start the browser the user pinned, then route again.

        Only the "nothing is connected" failure qualifies. Every other routing error
        means a browser IS connected and the call must not be answered by starting a
        second one. Returns the same (browser, error) pair the caller already had when
        there is nothing to start or the start does not land.
        """
        if self.NO_BROWSER_ERROR not in routing_error:
            return None, routing_error
        try:
            if await self.server.ensure_default_browser_running() is None:
                return None, routing_error
        except Exception:
            # The module logs its own failures; here the original error still stands.
            return None, routing_error
        return self.target_browser()

    async def request(self, action: str, data: Optional[Dict[str, Any]] = None,
                       timeout: float = 30.0, _note_wire: bool = True) -> Dict[str, Any]:
        """Build a protocol request, send it to the CALLER's Firefox, and await the response.

        Every tool goes through here, which is how multi-browser routing needs no `browser`
        argument on any tool schema: the target is derived from who is calling (see
        client_key / resolve_target_browser). With one Firefox connected this resolves to
        that one, i.e. exactly what it always did.

        `timeout` is threaded through to the WebSocket wait so long operations
        (waits, settles, full-page reads) are bounded by THEIR budget instead
        of dying at a generic 30s.
        """
        target, routing_error = self.target_browser()
        if routing_error:
            target, routing_error = await self.retry_after_launching_browser(routing_error)
        if routing_error:
            # Same envelope send_request_and_wait uses for its own failures, so error_of
            # surfaces it like any other (including the plain "No extension connection
            # available" when nothing is connected).
            response = {"error": routing_error}
            if _note_wire:
                tool_outcome.note_wire(response)
            return response
        # Before the send, so a call that then fails still says where it was aimed.
        rerouted = self.note_routing_change(target)
        if rerouted and carries_browser_local_ids(data):
            # A tab id or ref from before the change would act on the same number in the
            # OTHER browser. The memory already moved on, so only this call is refused.
            response = {"error": REFUSED_AFTER_REROUTE}
            if _note_wire:
                tool_outcome.note_wire(response)
            return response
        response = await self._send(action, data, timeout, target)
        if _note_wire:
            # The wire layer's retryable/code verdict rides to fail(); a helper request
            # made only to enrich an error (_note_wire=False) must not overwrite it.
            tool_outcome.note_wire(response)
        return response

    async def _send(self, action, data, timeout, target) -> Dict[str, Any]:
        request = {
            "id": str(uuid.uuid4()),
            "type": "request",
            "action": action,
            "data": data or {},
            "timestamp": datetime.now().isoformat()
        }
        if target is None:
            return await self.server.send_request_and_wait(request, timeout=timeout)
        return await self.server.send_request_and_wait(
            request, timeout=timeout, browser=target)

    async def tab_state_after_timeout(self, tab_id: Optional[int]) -> str:
        """What does the tab look like now, after a request overran its budget?

        The extension is still perfectly responsive when a `content.act` overruns;
        that batch is just ONE in-flight request; so a cheap tabs.list still
        answers. Without this the agent gets a bare "Request timed out after 22.1
        seconds", which reads as "nothing happened" and is wrong often enough to
        derail a session (a timed-out click that had in fact navigated, once to an
        unintended page). Best-effort: any failure here adds nothing rather than
        replacing the real error.
        """
        try:
            resp = await self.request("tabs.list", {}, timeout=10.0, _note_wire=False)
            if self.error_of(resp):
                return ""
            tabs = (resp.get("data") or {}).get("tabs") or []
        except Exception:
            return ""
        chosen = None
        for t in tabs:
            if not isinstance(t, dict):
                continue
            if tab_id is not None:
                if t.get("id") == tab_id:
                    chosen = t
                    break
            elif t.get("active"):
                chosen = t
                break
        if not chosen:
            return (" (the tab could not be re-read afterwards; it may have been closed; "
                    "list tabs before retrying)")
        title = str(chosen.get("title") or "")[:80]
        return (f" The tab is NOW at {chosen.get('url', '?')} (\"{title}\", "
                f"status={chosen.get('status', '?')}). A timeout does NOT mean nothing happened: "
                "re-read the page and confirm where you are before retrying; the action may have "
                "completed after the wait was abandoned, possibly navigating somewhere unintended.")

    def error_of(self, response: Dict[str, Any]) -> Optional[str]:
        """Extract an error message from a WS response, or None on success."""
        err: Optional[str] = None
        if "error" in response:
            err = str(response["error"])
        elif response.get("type") == "error":
            # `data` can be null or a bare string on this envelope; only a dict carries a message.
            data = response.get("data")
            msg = data.get("message") if isinstance(data, dict) else None
            err = str(msg) if msg else "Unknown error"
        elif not (response.get("type") == "response" and "data" in response):
            err = "No response from extension (is a browser running with the BrowserPlug extension connected? Check server_info)"
        # "Unknown ... action" means the connected extension build predates this
        # server's tool surface (version skew); name that explicitly so the agent
        # reloads the extension instead of retrying the dead tool or guessing.
        # Nothing attached is the one failure the agent cannot route around, and the bare
        # sentence read as "this call failed" rather than "stop and talk to the human",
        # which is how sessions ended up installing a second browser instead of asking.
        # Appended HERE rather than at the source: server.py's string is pinned by equality
        # and tool_outcome.fail() must return its argument untouched.
        err = (err + no_browser_hint(err)) if err else err
        # Never "omit tab_id to target the active tab": that is the user's tab.
        err = stale_tab_text(err, self.owner_label()) if err else err
        if err and re.search(r"Unknown \w+ action:", err):
            routed = self.routed_browser()
            ver = ((routed.version if routed is not None
                    else self.server.extension_version) or "unknown")
            err += (f"; version skew: the connected extension (v{ver}) is older than this "
                    "server's tool surface and does not implement this action. Reload the "
                    "BrowserPlug extension (Firefox: about:debugging ▸ This Firefox ▸ Reload; "
                    "Chrome, Edge, Brave: the extensions page ▸ Reload) or install "
                    "the freshly built package, then retry.")
        # The file:// refusal is already a complete, MEASURED answer; whether a tab
        # was in fact created, the exact next call, the toggle that governs reading one, and
        # the current permission state. Wrapping it in "Error opening tab: Tabs API error:"
        # would bury all of that behind a prefix that implies a generic API failure.
        if err and err.startswith("the browser refused to "):
            return err
        # The extension's refusal names its code on the frame, not in the message.
        ext_code = tool_outcome.extension_code(response)
        if err and (ext_code == "PROXY_PERMISSION_REQUIRED" or "PROXY_PERMISSION_REQUIRED" in err):
            err = self.proxy_grant_hint() + err
        # PROXY_UNSUPPORTED comes back from a Chromium extension: per-tab SOCKS routing
        # does not exist on Blink at all. Even if the server-side pre-check (§3) was
        # bypassed, enrich the extension's own error with the "switch to a Firefox" hint.
        if err and (ext_code == "PROXY_UNSUPPORTED" or "PROXY_UNSUPPORTED" in err):
            err = self.proxy_unsupported_hint() + err
        return err

    def proxy_grant_hint(self) -> str:
        """The `proxy` arg is exposed when ANY connected browser holds the optional "proxy"
        permission, but the extension is fail-closed PER browser; so the call can land in
        one that lacks it. Name the browser that HAS it; switching is one tool call."""
        try:
            target, _ = self.target_browser()
        except Exception:
            return ""
        return self.proxy_grant_refusal(target) + " Extension error: "

    def proxy_grant_refusal(self, target) -> str:
        """The sentence refusing a proxy in `target` for want of the grant. It names a
        connected browser that holds the grant, if one does."""
        holders = [b for b in self.connected_browsers()
                   if b is not target
                   and (getattr(b, "proxy_access", None) or {}).get("has_proxy_permission")]
        where = self.proxy_grant_where(target)
        if not holders:
            return f"Proxy routing is not allowed in this browser yet; {where}, then retry."
        b = holders[0]
        return (f"Proxy routing is not allowed in this browser, but {b.describe()} allows "
                f"it; switch to it with "
                f'browsers(action="use", target={b.slot}) and retry, or {where}.')

    @staticmethod
    def proxy_grant_where(browser) -> str:
        """The clause telling the user how to allow proxy use in `browser`: Firefox has a
        real optional permission; Chromium allows none, so there it is a switch BrowserPlug
        keeps in its popup."""
        if browser is not None and getattr(browser, "engine", None) == "blink":
            return ("turn it on in the BrowserPlug popup (toolbar button, Permissions, "
                    "SOCKS proxy, Allow the browser-wide proxy)")
        return "grant the optional proxy permission in about:addons ▸ Permissions"

    def target_proxy_access(self):
        """`(browser, its hello's proxyAccess dict)` for the browser this session drives;
        `(None, {})` when there is none yet."""
        try:
            target, _ = self.target_browser()
        except Exception:
            target = None
        if target is None:
            return None, (self.server.extension_proxy_access or {})
        return target, (getattr(target, "proxy_access", None) or {})

    def owner_label(self) -> str:
        """`[2] Firefox Developer Edition`: the browser this client's calls reach, for a
        result that hands out a browser-local id. "" while only one browser is connected,
        where naming it tells the agent nothing."""
        try:
            if len(self.connected_browsers()) < 2:
                return ""
            target, _ = self.target_browser()
            if target is None:
                return ""
            name = getattr(target, "edition", None) or getattr(target, "name", None) or ""
            slot = getattr(target, "slot", None)
            return f"[{slot}] {name}" if slot is not None else str(name)
        except Exception:
            return ""

    def target_browser_name(self) -> str:
        """The driven browser as the agent should read it in a notice."""
        target, _ = self.target_proxy_access()
        try:
            return target.describe() if target is not None else "this browser"
        except Exception:
            return "this browser"

    def target_proxy_is_browser_wide(self) -> bool:
        """Whether `proxy` on the driven browser switches the whole browser (Chromium)
        rather than one tab (Firefox). The hello says so; Blink is browser-wide anyway."""
        target, pa = self.target_proxy_access()
        return browser_proxy.is_browser_wide(pa) or getattr(target, "engine", None) == "blink"

    def target_has_proxy_permission(self) -> bool:
        """Whether the driven browser itself holds the optional proxy permission."""
        return bool(self.target_proxy_access()[1].get("has_proxy_permission"))

    def proxy_capable_holder(self, exclude=None):
        """A connected browser that CAN do per-tab SOCKS routing AND holds the grant:
        engine is not Blink (a Chromium switches the whole browser, which is not what an
        alternative to an outdated build should silently be), proxy_supported is not
        explicitly False (a Firefox omits it (or MISSING means supported), and
        has_proxy_permission is set. None if there is none."""
        try:
            for b in self.connected_browsers():
                if b is exclude:
                    continue
                if getattr(b, "engine", None) == "blink":
                    continue
                pa = (getattr(b, "proxy_access", None) or {})
                if pa.get("proxy_supported") is False:
                    continue
                if pa.get("has_proxy_permission"):
                    return b
        except Exception:
            return None
        return None

    def proxy_capability_error(self, proxy) -> Optional[str]:
        """Fail-closed BEFORE the wire: refuse a `proxy=…` call routed to a browser whose
        hello declared proxy_supported:false, which is a Chromium extension build from
        before the browser-wide proxy (it would only answer PROXY_UNSUPPORTED). Names a
        capable+granted browser to switch to when one exists. `proxy` MISSING/empty/'direct'
        is always fine, and so is a current Chromium: there `proxy` switches the whole
        browser (.tmp/dev-docs/proxy-connections.md). A browser that omits proxy_supported is
        treated as supported (only an explicit False blocks)."""
        if proxy in (None, "", "direct"):
            return None
        try:
            target, _ = self.target_browser()
        except Exception:
            return None
        if target is None:
            return None
        pa = (getattr(target, "proxy_access", None) or {})
        if pa.get("proxy_supported") is not False:
            return None
        holder = self.proxy_capable_holder(exclude=target)
        hint = (f"; switch to {holder.describe()} with "
                f'browsers(action="use", target={holder.slot}) and retry.'
                if holder is not None else
                "; reload the BrowserPlug extension to its current build and retry.")
        return (f"{target.describe()} runs a BrowserPlug extension build that does not "
                f"support proxy routing{hint}")

    def proxy_unsupported_hint(self) -> str:
        """Prefix for a PROXY_UNSUPPORTED extension error: this browser is Chromium and
        cannot route per-tab; name a Firefox that can (and holds the grant) if one is
        connected, so the agent can switch with one call."""
        holder = self.proxy_capable_holder()
        if holder is None:
            return ("This browser is Chromium-based and cannot do per-tab SOCKS routing "
                    "(no proxy.onRequest API). Extension error: ")
        return (f"This browser is Chromium-based and cannot do per-tab SOCKS routing; "
                f"{holder.describe()} can; switch to it with "
                f'browsers(action="use", target={holder.slot}) and retry. '
                f"Extension error: ")

    def proxy_permission_granted(self) -> bool:
        """Aggregate: ANY connected browser holds the grant.

        The arg is a schema-level thing and the schema is one per server, so it cannot be
        per-browser. Exposing it whenever some browser can use it is safe because the
        extension is fail-closed at call time (PROXY_PERMISSION_REQUIRED), and that error
        now names the browser that does have the grant (see proxy_grant_hint)."""
        browsers = self.connected_browsers()
        if browsers:
            return any((b.proxy_access or {}).get("has_proxy_permission") for b in browsers)
        pa = self.server.extension_proxy_access
        return bool(pa and pa.get("has_proxy_permission"))
