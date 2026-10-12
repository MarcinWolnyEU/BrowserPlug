"""`proxy="tor"`: the value that stands for BrowserPlug's own managed Tor client.

The extension only ever sees a `socks5://127.0.0.1:<port>` endpoint. This module turns the
word into that endpoint, starting (and on the first use installing) the Tor client behind
it, so the per-tab routing and its fail-closed rules in the extension are unchanged.
`tabs` and `navigate` call `resolve` only after their own validation, the proxy capability
check (`BrowserCalls.proxy_capability_error`, which refuses a browser that cannot route, such
as a Chromium build older than the browser-wide proxy) and `refuse_before_start`, which
refuses a browser whose last hello did not report the proxy grant. So a call that is going
to be refused never starts a download or a daemon. The extension still checks the grant
itself and stays the final word. `.tmp/dev-docs/tor.md`.
"""

import asyncio
from urllib.parse import urlsplit

from server.tools import browser_proxy
from server.tools.tool_outcome import fail
from server.tor import manager as tor_manager

TOR = "tor"

# Why a proxy="tor" call stops before Tor starts (`start_blocker`). A navigate goto
# without a url never gets this far: navigate refuses it as an argument error first.
NO_BROWSER = "no_browser"
NOT_PERMITTED = "not_permitted"
# The code the extension's own refusal carries (`tool_outcome._EXT_CODES`).
PERMISSION_CODE = "PROXY_PERMISSION_REQUIRED"


def is_tor(proxy):
    """Whether `proxy` is the word that asks for the managed Tor client."""
    return isinstance(proxy, str) and proxy.strip().lower() == TOR


def start_blocker(proxy, *, routing_error=None, proxy_access=None):
    """Why a call must stop before `resolve` starts Tor, or None. Pure.

    The grant is `has_proxy_permission` from the target browser's last hello: the optional
    permission on Firefox, the popup's switch on Chromium. Missing or unknown counts as not
    granted, because the extension would refuse the call once Tor was up. A grant revoked
    since that hello still meets the extension's own refusal: trusting the hello can waste
    a start, but it cannot route a tab without the grant.
    """
    if not is_tor(proxy):
        return None
    if routing_error:
        return NO_BROWSER
    if not (proxy_access or {}).get("has_proxy_permission"):
        return NOT_PERMITTED
    return None


async def refuse_before_start(calls, proxy):
    """The failed result for a `proxy="tor"` call the browser would refuse, or None.

    Call it right before `resolve`. With nothing connected it first does what `BrowserCalls.request` would: start the pinned
    browser if the user allowed that, so the grant read is that browser's."""
    if not is_tor(proxy):
        return None
    _, routing_error = calls.target_browser()
    if routing_error:
        _, routing_error = await calls.retry_after_launching_browser(routing_error)
    target, access = calls.target_proxy_access()
    blocker = start_blocker(proxy, routing_error=routing_error, proxy_access=access)
    if blocker == NO_BROWSER:
        return fail("Error: " + calls.error_of({"error": routing_error}))
    if blocker == NOT_PERMITTED:
        return fail("Error: " + calls.proxy_grant_refusal(target), code=PERMISSION_CODE)
    return None


async def resolve(proxy):
    """`(proxy to send, error)`. Anything but "tor" comes back unchanged with no error; "tor"
    becomes the managed client's loopback endpoint, started if it is not running."""
    if not is_tor(proxy):
        return proxy, None
    try:
        # Blocking by design (a first call downloads and bootstraps): off the event loop.
        endpoint = await asyncio.to_thread(tor_manager.shared().start)
    except tor_manager.TorError as exc:
        return proxy, f"proxy=\"tor\" could not start Tor: {exc}"
    return endpoint, None


# The extension reports a failed lookup as NS_ERROR_UNKNOWN_HOST ("unknown host - DNS could
# not resolve the domain"); that is what a .onion name gives outside Tor.
_UNRESOLVED_MARKERS = ("unknown_host", "unknown host")


def unrouted(proxy):
    """Whether a call's own `proxy` argument leaves the tab outside any proxy."""
    return proxy in (None, "", "direct")


def is_onion_url(url):
    try:
        return bool(url) and (urlsplit(url).hostname or "").lower().endswith(".onion")
    except ValueError:
        return False


def is_unresolved_onion(url, error_text, proxy):
    """True for a `.onion` URL whose lookup failed on a call that asked for no proxy: the
    one failure `proxy="tor"` fixes. Pure; the caller still has to check the tab is not
    already routed (an offline hidden service fails the same way through Tor)."""
    return (unrouted(proxy) and bool(error_text) and is_onion_url(url)
            and any(m in str(error_text).lower() for m in _UNRESOLVED_MARKERS))


async def onion_hint(calls, *, url, error_text, proxy, tab_id, retry_call):
    """The sentence to append to a failed navigation or tab open, or "".

    `retry_call` is the exact call to repeat, e.g. `navigate(url="…", proxy="tor")`. What the
    hint says depends on why that call cannot be made as is: Chromium cannot route per tab,
    Firefox may lack the optional permission, otherwise it is just the retry."""
    if not is_unresolved_onion(url, error_text, proxy):
        return ""
    if tab_id is not None:
        try:
            routing = (await calls.request("tabs.routing", {"tabId": tab_id})).get("data") or {}
            if routing.get("routed") or routing.get("browserProxy"):
                return ""      # already behind a proxy: the service itself is unreachable
        except Exception:
            pass
    head = "\n  HINT: .onion addresses resolve only inside Tor, so this tab needs routing. "
    refused = calls.proxy_capability_error(TOR)
    if refused:
        return head + refused
    if not calls.target_has_proxy_permission():
        where = calls.proxy_grant_where(calls.target_proxy_access()[0])
        return (head + f"Proxy routing is not allowed in this browser yet: {where}, then call "
                f"{retry_call}.")
    hint = (head + f"Call {retry_call} (BrowserPlug starts its own Tor client; the first use "
            "downloads it and takes about a minute).")
    if calls.target_proxy_is_browser_wide():
        hint += (" This browser cannot route a single tab, so that switches the ENTIRE "
                 "browser, every tab including the ones the user is using, to Tor until you "
                 f"send proxy=\"\" ({browser_proxy.OFF_CALL}).")
    return hint
