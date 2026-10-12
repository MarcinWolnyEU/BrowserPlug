"""Dispatch for what a browser extension sends the server over its WebSocket.

Two tables. MESSAGE_HANDLERS keys on the envelope's `type` (debug_log, extension_hello,
browser_state, request, response, error); REQUEST_ACTIONS keys on the `action` of a
`request`, i.e. the `server.*` calls the extension makes on the server. Adding a call is
one function and one row.

The wire behaviour is old and the extension depends on its edges, so they are kept:
  * debug_log, extension_hello and browser_state are handled before the "Received from
    extension" log line, because they are chatter that line would drown in;
  * a `request` whose action has no row falls out of the request branch and is reported
    as an unhandled message type, not as an unknown action;
  * any exception, including a non-object JSON body, is logged and swallowed. A message
    handler that raised would take the connection loop down with it.

Nothing here imports server.py. Everything the handlers need arrives as a WsRpcHooks.
"""

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable, Optional

from server.usage_panels import usage_panels
from server.reporting.reports import SnapshotCache

logger = logging.getLogger(__name__)

HEALTH_ACTION = 'server.health'


@dataclass(frozen=True)
class WsRpcHooks:
    """The server's side of each handler. Every field is a callable resolved when the
    message arrives, so a test that patches the server instance is seen here."""
    note_activity: Callable[[], None]
    browser_for_ws: Callable[[Any], Any]
    default_browser: Callable[[], Any]
    handle_hello: Callable[[Any, dict], Any]
    refresh_browsers_tool: Callable[[], None]
    handle_browser_state: Callable[[Any, Any], None]
    send: Callable[..., Awaitable[bool]]                 # (message, browser=) -> sent?
    server_start_time: Callable[[], datetime]
    mcp_port: Callable[[], int]
    legacy_extension_version: Callable[[], Optional[str]]
    begin_restart: Callable[[str], None]
    default_browser_payload: Callable[[Any], dict]
    set_default_browser: Callable[[dict, Any], tuple]
    connections_info: Callable[..., dict]                # (kilo_details=, viewer_host=)
    handle_response: Callable[[dict], Awaitable[None]]


class WsRpc:
    def __init__(self, hooks: WsRpcHooks):
        self._h = hooks
        # get_connections reports are built on the report pool and kept for a moment: the
        # popup and the options page both poll, and the build walks the process tree.
        self._snapshots = SnapshotCache()
        # Handled before the receive log line. A handler returns True when it consumed
        # the message; False lets it fall through to the "unhandled" warning.
        self.QUIET_HANDLERS = {
            'debug_log': self._on_debug_log,
            'extension_hello': self._on_extension_hello,
            'browser_state': self._on_browser_state,
        }
        self.MESSAGE_HANDLERS = {
            'request': self._on_request,
            'response': self._on_response,
            'error': self._on_response,
        }
        self.REQUEST_ACTIONS = {
            HEALTH_ACTION: self._action_health,
            'server.restart': self._action_restart,
            'server.get_default_browser': self._action_get_default_browser,
            'server.set_default_browser': self._action_set_default_browser,
            'server.get_connections': self._action_get_connections,
        }

    async def handle_message(self, message: str, websocket=None):
        """Process a message from a browser extension.

        websocket identifies WHICH browser sent it (the registry may hold several);
        omitted -> the default browser, so legacy single-connection callers still work.
        """
        h = self._h
        data = None
        browser = None
        try:
            h.note_activity()
            browser = (h.browser_for_ws(websocket) if websocket is not None
                       else h.default_browser())
            if browser is not None:
                browser.last_seen = datetime.now()
            data = json.loads(message)
            message_type = data.get('type', 'unknown')
            message_id = data.get('id')
            action = data.get('action', 'unknown')

            if websocket is not None and browser is None and message_type == 'request':
                # A socket the registry no longer holds (removed, or displaced and then
                # closed). Answering would go to the DEFAULT browser, which may be a
                # remote one and would receive this socket's payload, and a server.*
                # action (restart, set_default_browser) would run on behalf of nobody.
                logger.warning(f"Ignoring {action} (ID: {message_id}) from an "
                               f"unregistered socket")
                return

            quiet = self.QUIET_HANDLERS.get(message_type)
            if quiet is not None:
                await quiet(data, browser)
                return

            if message_type == 'request' and action == HEALTH_ACTION:
                # Always-on health probe (extension polls every ~15s); keep it out
                # of the info log so it doesn't drown the real traffic.
                logger.debug(f"Received from extension: {message_type} - {action} (ID: {message_id})")
            else:
                logger.info(f"Received from extension: {message_type} - {action} (ID: {message_id})")

            handler = self.MESSAGE_HANDLERS.get(message_type)
            if handler is not None and await handler(data, browser, message_id, action):
                return

            # For other message types, just log for now
            logger.warning(f"Unhandled message type: {message_type}")

        except json.JSONDecodeError:
            logger.error(f"Invalid JSON received: {message}")
        except Exception as e:
            logger.error(f"Error processing extension message: {e}", exc_info=True)
            await self._answer_failed_request(data, browser, e)

    async def _answer_failed_request(self, data, browser, error):
        """Tell the far side a request-bearing frame failed, so it stops waiting for the
        full timeout. Best effort: this runs inside the catch-all and must not raise."""
        try:
            if (browser is None or not isinstance(data, dict)
                    or data.get('type') != 'request' or not data.get('id')):
                return
            await self._h.send({
                "id": data['id'],
                "type": "error",
                "action": data.get('action', 'unknown'),
                "data": {"message": f"Server error handling {data.get('action', 'request')}: "
                                    f"{error}"},
            }, browser=browser)
        except Exception:
            logger.warning("Could not send the error reply", exc_info=True)

    # ---- Message types --------------------------------------------------------------

    async def _on_debug_log(self, data, browser):
        # Handle debug logs specially - print them prominently
        level = data.get('data', {}).get('level', 'log')
        log_message = data.get('data', {}).get('message', '')
        timestamp = data.get('data', {}).get('timestamp', '')

        logger.info("----- EXTENSION DEBUG LOG -----")
        if level == 'error':
            logger.info(f"🔴 EXTENSION ERROR [{timestamp}]: {log_message}")
        else:
            logger.info(f"🔵 EXTENSION LOG [{timestamp}]: {log_message}")
        return True

    async def _on_extension_hello(self, data, browser):
        # Extension announces its identity/version (and diagnostic state) right after
        # connecting, and refreshes it periodically while connected. Every field is
        # optional: an older extension build sends fewer of them and must still work.
        h = self._h
        hello = data.get('data')
        if not isinstance(hello, dict):
            hello = {}
        if browser is None:
            logger.warning("extension_hello from an unregistered socket — ignoring")
            return True
        browser = h.handle_hello(browser, hello)
        logger.info(f"{browser.describe()} reported version: "
                    f"{browser.version or 'unknown'}")
        # The hello is what NAMES a browser (and may merge a provisional entry
        # into an established one), so the roster embedded in the tool's
        # description is only correct once it has landed.
        h.refresh_browsers_tool()
        return True

    async def _on_browser_state(self, data, browser):
        # What the BROWSER currently holds (window/tab counts), pushed on connect,
        # on every health-probe tick and whenever a tab or window comes or goes.
        # Diagnostics only: it must NOT count as browser activity (that would light
        # the connection map's edge on traffic no agent asked for), and it stays out
        # of the info log for the same reason the health probe does.
        if browser is None:
            logger.debug("browser_state from an unregistered socket — ignoring")
            return True
        self._h.handle_browser_state(browser, data.get('data'))
        return True

    async def _on_request(self, data, browser, message_id, action):
        handler = self.REQUEST_ACTIONS.get(action)
        if handler is None:
            return False
        await handler(data, browser, message_id)
        return True

    async def _on_response(self, data, browser, message_id, action):
        # Handle response/error from extension
        await self._h.handle_response(data)
        return True

    # ---- server.* actions -------------------------------------------------------------

    async def _action_health(self, data, browser, message_id):
        # Lightweight health probe over the WS. Unlike a socket that merely
        # reads OPEN, a timely reply here proves the event loop is alive and
        # the connection is truly bidirectional (not half-open / wedged). The
        # extension classifies the connection "green" only once this answers.
        h = self._h
        now = datetime.now()
        await h.send({
            "id": message_id,
            "type": "response",
            "action": "server.health",
            "data": {
                "status": "ok",
                "server_pid": os.getpid(),
                "uptime_seconds": int((now - h.server_start_time()).total_seconds()),
                "mcp_port": h.mcp_port(),
                "extension_version": (browser.version if browser is not None
                                      else h.legacy_extension_version()),
            },
        }, browser=browser)

    async def _action_restart(self, data, browser, message_id):
        # Restart requested over the WS (mirrors POST /restart). Reachable
        # even when the MCP HTTP control port is unreachable, so the options
        # page can cycle a server whose HTTP side is wedged or on a drifted
        # port. Ack first so the extension's request resolves, then cycle.
        h = self._h
        await h.send({
            "id": message_id,
            "type": "response",
            "action": "server.restart",
            "data": {"status": "restarting", "pid": os.getpid()},
        }, browser=browser)

        logger.info("Restart requested via WS server.restart")
        try:
            h.begin_restart("WS server.restart")
        except Exception as e:
            # RestartInProgress: the ack above is still true, a restart IS under way, and
            # a second sequence would only spawn a second replacement.
            logger.warning("WS server.restart ignored: %s", e)

    async def _action_get_default_browser(self, data, browser, message_id):
        # The default-browser setting, read and written by the options page.
        # Over the WebSocket rather than an HTTP endpoint because this socket is
        # already authenticated, so a browser on ANOTHER machine reaches it
        # without a second credential exchange (the MCP port's settings endpoints
        # demand the token in a header on every call).
        await self._h.send({
            "id": message_id,
            "type": "response",
            "action": "server.get_default_browser",
            "data": self._h.default_browser_payload(browser),
        }, browser=browser)

    async def _action_set_default_browser(self, data, browser, message_id):
        payload = data.get('data')
        payload, error = self._h.set_default_browser(
            payload if isinstance(payload, dict) else {}, browser)
        await self._h.send({
            "id": message_id,
            "type": "response",
            "action": "server.set_default_browser",
            "data": ({"ok": False, "error": error} if error
                     else dict(payload, ok=True)),
        }, browser=browser)

    async def _action_get_connections(self, data, browser, message_id):
        # Extension asking the server to report its connections.
        # `data` here is the whole envelope {id,type,action,data} -- the
        # request's own payload is one level down. Reading kilo_details off
        # the envelope always missed and fell back to the True default, so
        # the options-page off-switch did nothing and every 2s popup poll
        # still did per-PID open_files() + log tails.
        payload = data.get('data')
        payload = payload if isinstance(payload, dict) else {}
        kilo_details = payload.get('kilo_details', True)
        # The popup's switch is the one setting; remember it for the callers that have
        # no request of their own (/health, server_info).
        if 'kilo_details' in payload:
            usage_panels.record_switch(kilo_details)
        # The address THIS caller dialled, so the server row names something
        # the caller can actually reach (a phone reached us over the LAN).
        viewer_host = payload.get('server_host')
        h = self._h
        # Off this loop: the report scans processes, and every browser's traffic shares
        # this loop. Copied before the per-requester field goes in, because the cached
        # dict is shared with the next caller.
        payload = dict(await self._snapshots.get(
            ("connections", bool(kilo_details), viewer_host),
            lambda: h.connections_info(kilo_details=kilo_details, viewer_host=viewer_host)))
        # Which browser is asking. The options page's connection map uses
        # this to mark the Firefox it is rendered in as "this browser";
        # only the server knows which socket the question arrived on.
        payload["requester_slot"] = browser.slot if browser is not None else None
        await self._h.send({
            "id": message_id,
            "type": "response",
            "action": "server.get_connections",
            "data": payload,
        }, browser=browser)
