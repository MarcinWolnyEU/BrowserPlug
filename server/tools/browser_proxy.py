"""What an agent is told when a proxy switches a WHOLE browser.

Chromium (Chrome, Edge, Brave) cannot route one tab: `chrome.proxy` is browser-global. So a
`proxy` on `tabs` / `navigate` there switches every tab, including the ones the user is
looking at, and nothing in the call's own result would say so. These texts do, on every
switch and in the readbacks, because an agent that does not know a proxy is still on will
leave a person's whole browser on Tor. They state four things: that it happened, that
`proxy=""` undoes it, that it should be undone at the first opportunity after the current
set of operations, and that switching it on again costs nothing beyond the same effect.

Pure text and no I/O, so `tests/unit/test_browser_proxy.py` pins the wording. The
extension does the switching (`extension-chromium/background.js`, `.tmp/dev-docs/proxy-connections.md`).
"""

# The cheapest call that carries a proxy and opens or loads nothing.
OFF_CALL = 'tabs(action="routing", proxy="")'


def is_browser_wide(proxy_access):
    """Whether a browser's hello says its `proxy` means the whole browser."""
    return (proxy_access or {}).get("proxy_scope") == "browser"


def is_proxied(routing):
    """Whether the tab a `tabs.routing` response describes has its traffic going through a
    proxy: the extension's own `isProxy` when it sends one, else inferred (a routed tab, or
    a browser-wide proxy that covers every tab). The generic answer, not a Tor one: it is
    the same for any SOCKS endpoint, which an external "is this Tor" page cannot say."""
    if "isProxy" in routing:
        return bool(routing["isProxy"])
    return bool(routing.get("routed") or routing.get("browserProxy"))


def label(endpoint, via_tor):
    """How to name the proxy to the agent: Tor when it asked for the managed client."""
    return f"Tor ({endpoint})" if via_tor else endpoint


def on_notice(browser_name, endpoint, *, via_tor=False, already=False):
    """Appended to the result of a call that switched the browser to a proxy."""
    name = label(endpoint, via_tor)
    first = (f"The browser-wide proxy was already on: every tab of {browser_name} still goes "
             f"through {name}."
             if already else
             f"{browser_name} cannot route a single tab, so the proxy was applied to the "
             f"ENTIRE BROWSER. Every tab, including the ones the user may be using right "
             f"now, now goes through {name}.")
    return (f"\n  BROWSER-WIDE PROXY ON: {first}"
            f"\n  Turn it off at the first opportunity after you finish the current set of "
            f'operations: {OFF_CALL} (any call with proxy="" does it).'
            f"\n  Switching it on again is free, with no extra cost other than the same "
            f"effect: all tabs, including those the user may be actively using, are "
            f"switched to {name} each time it is on. So do not leave it on between tasks.")


def off_notice(browser_name, was):
    """Appended to the result of a call that switched the proxy off."""
    if was:
        return (f"\n  BROWSER-WIDE PROXY OFF: every tab of {browser_name} is back on a direct "
                f"connection (it had been routing everything through {was}).")
    return (f"\n  No browser-wide proxy was on in {browser_name}; nothing changed, every tab "
            f"was already direct.")


def state_line(browser_name, browser_proxy):
    """A readback line for `tabs` list and routing; "" when there is no proxy to report."""
    if not browser_proxy:
        return ""
    return (f"BROWSER-WIDE PROXY ON in {browser_name}: every tab goes through "
            f"{browser_proxy.get('endpoint')}. Turn it off with {OFF_CALL} as soon as you "
            f"are done.")


def change_notice(browser_name, change, *, via_tor=False):
    """The notice for a response's `browserProxyChange`, or "" when the call changed nothing."""
    if not change:
        return ""
    if change.get("off"):
        return off_notice(browser_name, change.get("was"))
    return on_notice(browser_name, change.get("endpoint"), via_tor=via_tor,
                     already=bool(change.get("already")))
