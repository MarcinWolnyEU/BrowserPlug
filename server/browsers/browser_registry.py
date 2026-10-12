"""The browser registry: one record per connected browser, and what changes it.

Owns the records (`browsers`, keyed by the extension's stable instance_id, or by a
provisional `pending-N` key until its hello names it), the slot ordinals (`slots`,
`next_slot`, stable for the server's lifetime), the departed-browser memory (`departed`)
and the operations on them: register a socket, displace an old one, promote a provisional
record on its hello, remove a browser, close everything, apply a hello or a
`browser_state` push.

Which browser a CLIENT reaches is routing.py's business; it reads `live_browsers()` and
the `departed` dict from here. Naming a browser (ExtBrowser, the brand tables) lives in
browser_identity.py, and the class arrives as `make_browser`. Everything else the registry needs from the server arrives as a callable,
resolved on every use because tests patch those seams on the server instance. Nothing here
imports server.py, and importing this module has no side effects.

Threading: the registry is mutated on the WebSocket loop's thread and read from the
uvicorn thread, which is why `by_slot` hands out a snapshot.
"""

import logging
import time
from datetime import datetime
from typing import Any, Callable, Dict, Optional

from server.ws import connection_log
from server.ws.identity_claim import claim_verdict
from server.browsers.client_state import MAX_DEPARTED, ExpiringMap
from server.browsers.routing_notice import DepartedBrowser
from server.ws.ws_auth import WS_CLOSE_NORMAL, peer_of

logger = logging.getLogger(__name__)

class TakeoverRefused(Exception):
    """A hello named a browser whose socket is still open and could not prove it is the
    same one. Carries the log text."""


def _device_label(websocket):
    """The label of the device token this socket logged in with ('' when it has none)."""
    label = getattr(websocket, "device_label", "")
    return label if isinstance(label, str) else ""


def apply_identity(browser, ident):
    if not ident:
        return
    browser.browser_pid = ident.get("pid") or browser.browser_pid
    browser.exe_path = ident.get("exe_path") or browser.exe_path
    browser.proc_started = ident.get("start_time") or browser.proc_started
    browser.engine = ident.get("engine") or browser.engine
    browser.brand = ident.get("brand") or browser.brand
    browser.friendly = ident.get("friendly") or browser.friendly


#: The foreground tab of each window; the extension already caps its push, this is
#: the server-side ceiling on what a (possibly hostile, possibly remote) one can send.
ACTIVE_TAB_LIST_MAX = 12
ACTIVE_TAB_TITLE_MAX = 120


def clean_active_tab_list(value: Any):
    """[{id, title, last_hms}] or None when the push carried no usable list.

    None and [] mean different things here: None = "this extension said nothing
    about it" (an older one), [] = "it says there are none". The tooltip must be
    able to tell those apart, so the empty list is kept as an empty list.
    """
    if not isinstance(value, list):
        return None
    cleaned = []
    for item in value[:ACTIVE_TAB_LIST_MAX]:
        if not isinstance(item, dict):
            continue
        tab_id = item.get("id")
        if isinstance(tab_id, bool) or not isinstance(tab_id, int):
            continue
        title = item.get("title")
        title = title[:ACTIVE_TAB_TITLE_MAX] if isinstance(title, str) else ""
        last_hms = item.get("last_hms")
        cleaned.append({
            "id": tab_id,
            "title": title,
            # The BROWSER's own clock (it is the browser's event); a string or null,
            # never a number we might mistake for our own epoch.
            "last_hms": last_hms if isinstance(last_hms, str) else None,
        })
    return cleaned


def handle_browser_state(browser, state: Any):
    """Store a browser_state push ({windows, tabs, active_tabs, loaded_tabs,
    active_tab_list}).

    Every field is optional and only integers are kept: an older/newer extension may
    send fewer keys, and a malformed push must leave the previous (or empty) state
    alone rather than putting junk on the options page's tooltip. The tab list is
    held to the same standard entry by entry; anything unrecognizable is dropped
    rather than forwarded, since it lands in a tooltip unchanged.
    """
    if not isinstance(state, dict):
        return
    kept = {}
    for key in ("windows", "tabs", "active_tabs", "loaded_tabs"):
        value = state.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            continue
        kept[key] = value
    tab_list = clean_active_tab_list(state.get("active_tab_list"))
    if tab_list is not None:
        kept["active_tab_list"] = tab_list
    if not kept:
        return
    browser.browser_state = kept
    browser.browser_state_at = datetime.now()


class BrowserRegistry:
    def __init__(self, *, make_browser: Callable[..., Any],
                 probe_identity: Callable[[Any], dict],
                 fail_pending: Callable[..., None],
                 reassign_pending_owner: Callable[[str, str], None],
                 drop_gate: Callable[[str], None],
                 close_socket: Callable[[Any], None],
                 refresh_browsers_tool: Callable[[], None],
                 refresh_proxy_tool_visibility: Callable[[], None],
                 hello_window: Callable[[], float],
                 move_gate: Optional[Callable[[str, str], None]] = None):
        """`make_browser(ws, instance_id, slot, remote_addr, is_remote)` builds a record;
        `probe_identity(ws)` is the local-process enrichment (empty for a remote peer);
        `fail_pending(reason, instance_id=None)` and `reassign_pending_owner(old, new)`
        are the request table's side of a socket change; `drop_gate(instance_id)` forgets
        the browser's admission gate; `hello_window()` is the seconds a hello may take;
        `move_gate(old, new)` carries a gate across a re-key (without it the old gate is
        dropped, which loses only counters)."""
        self._make_browser = make_browser
        self._probe_identity = probe_identity
        self._fail_pending = fail_pending
        self._reassign_pending_owner = reassign_pending_owner
        self._drop_gate = drop_gate
        self._close_socket = close_socket
        self._refresh_browsers_tool = refresh_browsers_tool
        self._refresh_proxy_tool_visibility = refresh_proxy_tool_visibility
        self._hello_window = hello_window
        self._move_gate = move_gate

        # One entry per connected browser, keyed by the extension's stable instance_id (a
        # provisional key until its extension_hello names it). Several browsers may be
        # connected at once.
        self.browsers: Dict[str, Any] = {}
        self.slots = {}     # instance_id -> slot ordinal, stable for this server's lifetime
        self.next_slot = 1
        self.conn_seq = 0   # counter behind the provisional keys of pre-hello sockets
        # What departed browsers left behind (routing_notice.DepartedBrowser: a str
        # carrying describe(), plus when and how it went), so the "your browser is gone"
        # error can still NAME what the client had selected, and the routing notice can
        # say what happened to it. Capped at MAX_DEPARTED, oldest departure evicted first.
        self.departed: Dict[str, DepartedBrowser] = ExpiringMap(max_entries=MAX_DEPARTED)

    def by_slot(self):
        """Registry snapshot in slot order. A snapshot (not a live view): the registry
        is mutated on the WebSocket loop's thread and read from the uvicorn thread."""
        return sorted(self.browsers.values(), key=lambda b: b.slot)

    @property
    def default_browser(self) -> Optional[Any]:
        """The browser every unrouted call goes to: first-connected, still alive.

        The second pass keeps the legacy contract that a socket object which never
        exposes `close_code` (a test double) is still sendable; `rpc.send_to_extension`
        has always been a plain truthiness check on the socket.
        """
        browsers = self.by_slot()
        for b in browsers:
            if b.connected:
                return b
        for b in browsers:
            if b.ws is not None:
                return b
        return None

    def live_browsers(self):
        """Browsers a tool call can actually be routed to, in slot order.

        Mirrors default_browser's two passes exactly, so routing and the legacy
        single-slot path agree on what "connected" means: a socket that never exposes a
        real close_code (a MagicMock in the unit tests) reads as NOT connected yet has
        always been sendable.
        """
        browsers = self.by_slot()
        live = [b for b in browsers if b.connected]
        if live:
            return live
        return [b for b in browsers if b.ws is not None]

    def browser_for_ws(self, websocket) -> Optional[Any]:
        if websocket is None:
            return None
        for b in list(self.browsers.values()):
            if b.ws is websocket:
                return b
        return None

    def register(self, websocket, accepted_at=None, ident=None) -> Any:
        """Register a freshly connected socket. Pre-hello its identity is unknown, so it
        gets a provisional key; the hello promotes it (promote).

        One reconnect can be recognized before the hello: the local pid probe. Same
        Firefox pid == same browser (an extension reload keeps the process), so its
        socket is displaced immediately, as it was under the single-slot rule, instead
        of leaving a dead socket as the default for the round-trip the hello takes.

        `accepted_at` is this socket's monotonic accept time, which the hello's latency
        and the disconnect line are both measured from. It rides the RECORD because the
        record is what every later reader has; the socket is gone by then.
        """
        existing = self.browser_for_ws(websocket)
        if existing is not None:
            return existing

        remote_addr, is_remote = peer_of(websocket)
        # `ident` lets the async connect handler resolve the peer's process on an executor
        # and hand the answer in; without it the probe runs here, on the caller's thread.
        if is_remote:
            ident = {}
        elif ident is None:
            ident = self._probe_identity(websocket)
        pid = ident.get("pid")
        # Only a family whose process IS one profile may be recognized by pid. Every
        # profile of one Chromium install shares its main process, so the same pid there
        # means "same install", not "same profile"; matching would merge two profiles and
        # have them evict each other in a reconnect loop. Their hello's instance_id alone
        # decides. An unresolved engine keeps the pid match (the pre-existing behavior).
        if pid and ident.get("engine") in (None, "gecko"):
            for b in self.by_slot():
                if b.browser_pid == pid:
                    self.displace(b, websocket)
                    apply_identity(b, ident)
                    b.device_label = _device_label(websocket)
                    b.accepted_at = accepted_at
                    return b

        self.conn_seq += 1
        key = f"pending-{self.conn_seq}"
        browser = self._make_browser(websocket, key, self.next_slot, remote_addr, is_remote)
        self.next_slot += 1
        apply_identity(browser, ident)
        browser.device_label = _device_label(websocket)
        browser.accepted_at = accepted_at
        self.browsers[key] = browser
        return browser

    def displace(self, browser: Any, websocket, claimed_instance_id: Optional[str] = None):
        """Point an existing browser record at a new socket (Firefox restart / extension
        reload / same instance_id reconnecting).

        The displaced socket's in-flight requests are failed HERE, not left to its own
        handler's finally: that finally is skipped once the record no longer points at it,
        and when the old socket is ALREADY closed nothing suspends in between, so the old
        handler never gets a chance to run first. Every request owned by this browser at
        this instant necessarily belongs to the OLD socket; the successor is not bound
        yet, so no tool can have sent on it, which makes failing them safe BY
        CONSTRUCTION rather than by scheduling order.

        `claimed_instance_id` is the instance_id the incoming socket's hello carried, when
        the caller knows it. A record that already has a different, real instance_id
        belongs to another profile: its socket is left open and nothing is failed,
        because closing it only starts a reconnect loop between the two profiles.
        Returns False when it refused.
        """
        old = browser.ws
        if old is websocket:
            return True
        if (claimed_instance_id and old is not None
                and not getattr(browser, "provisional", False)
                and browser.instance_id != claimed_instance_id):
            logger.warning(
                f"Not displacing the socket of {browser.describe()}: the new socket "
                f"claims a different instance_id")
            return False
        if old is not None:
            self._fail_pending(
                f"{browser.describe()} reconnected — the previous connection was "
                f"displaced before it answered",
                instance_id=browser.instance_id)
        browser.ws = websocket
        browser.connect_count += 1
        browser.last_seen = datetime.now()
        # State belongs to the extension process behind the OLD socket: cleared until the
        # new one sends its hello.
        browser.version = None
        browser.file_access = None
        browser.proxy_access = None
        browser.api_support = None
        if old is not None and getattr(old, "close_code", None) is None:
            logger.info(f"Closing the previous socket of {browser.describe()}")
            self._close_socket(old)
        return True

    def promote(self, browser: Any, instance_id: str) -> Any:
        """An extension_hello named this browser: re-key its provisional entry, or merge
        it into the established record for that instance_id (same profile reconnecting).
        Returns the registry entry that now owns the socket."""
        if browser.instance_id == instance_id:
            browser.provisional = False
            return browser

        old_key = browser.instance_id
        established = self.browsers.get(instance_id)
        if established is not None and established is not browser:
            if established.connected:
                allowed, why = claim_verdict(established, browser)
                if not allowed:
                    raise TakeoverRefused(
                        f"refused {instance_id!r} from {browser.remote_addr or 'an unknown peer'}: "
                        f"{why}")
            # Same profile, new socket: keep the established record (its slot, its
            # history) and move the socket onto it. Requests already sent on this socket
            # (pre-hello, while it was provisional) move with it.
            self.browsers.pop(old_key, None)
            self.displace(established, browser.ws, claimed_instance_id=instance_id)
            established.remote_addr = browser.remote_addr or established.remote_addr
            established.is_remote = browser.is_remote
            established.device_label = getattr(browser, "device_label", "")
            apply_identity(established, {
                "pid": browser.browser_pid, "exe_path": browser.exe_path,
                "start_time": browser.proc_started, "friendly": browser.friendly})
            # The accept time belongs to the SOCKET, which has just moved here; leaving
            # the established record's own would measure this hello against a connection
            # that ended minutes ago.
            established.accepted_at = browser.accepted_at
            # Also per-socket: the record kept its old value from the previous socket.
            established.encrypted = browser.encrypted
            established.provisional = False
            self._rekey_identity(old_key, established.instance_id)
            return established

        browser.instance_id = instance_id
        browser.provisional = False
        # It went away and it is back, so this record inherits the account of how it
        # went. Without this the reconnect ERASES the only record of the drop, and the
        # one moment anybody asks "why did it go?" is the moment after it comes back.
        gone = self.departed.get(instance_id)
        if isinstance(gone, DepartedBrowser) and browser.last_disconnect_at is None:
            browser.last_disconnect_at = gone.at
            browser.last_close_code = gone.close_code
            browser.last_close_reason = gone.close_reason
        # A profile that connected earlier in this server's life keeps its slot.
        browser.slot = self.slots.get(instance_id, browser.slot)
        self.slots[instance_id] = browser.slot
        self._rekey_identity(old_key, instance_id)
        self.browsers[instance_id] = browser
        return browser

    def _rekey_identity(self, old_key: str, new_key: str):
        """Move everything keyed by `old_key` onto `new_key`: the ONE place a re-key is
        applied, so a structure cannot be forgotten.

        A browser's identity lives in:
          * `browsers`         instance_id -> record (dropped here; the caller re-adds)
          * `slots`            instance_id -> slot (keyed by real ids only, never `pending-N`)
          * `departed`         instance_id -> how it went (real ids only, kept on purpose)
          * `pending_owners`   request id -> instance_id (ExtensionRpc; reassigned)
          * admission gates    instance_id -> BrowserGate (RequestQueue; moved, or dropped
                               when `new_key` already has one)
        plus per-socket state on the record itself (`encrypted`), which the merge branch
        of promote() copies.
        """
        self.browsers.pop(old_key, None)
        self._reassign_pending_owner(old_key, new_key)
        if self._move_gate is not None:
            self._move_gate(old_key, new_key)
        else:
            self._drop_gate(old_key)

    def departed_record(self, browser: Any) -> DepartedBrowser:
        """What is kept about a browser once it is out of the registry: its name (which a
        selection error has always needed) plus when and how it went, which is what a
        routing notice has to say to be worth reading."""
        return DepartedBrowser(browser.describe(), instance_id=browser.instance_id,
                               at=browser.last_disconnect_at or datetime.now(),
                               close_code=browser.last_close_code,
                               close_reason=browser.last_close_reason,
                               slot=browser.slot)

    def remove(self, browser: Any):
        if self.browsers.get(browser.instance_id) is browser:
            del self.browsers[browser.instance_id]
        # Remember what it was called: a client that had SELECTED this browser must be
        # told which one went away, and the record is about to be unreachable.
        self.departed[browser.instance_id] = self.departed_record(browser)
        browser.ws = None
        # Forget its admission gate too, so a browser that reconnects starts with a
        # clean depth rather than inheriting stale counters. In-flight callers each still
        # run their own release() against the gate object they captured, so this strands
        # nobody, and rpc.fail_pending_requests is already settling their futures.
        self._drop_gate(browser.instance_id)

    def close_all(self, reason: str):
        """Close every extension socket (server shutdown / restart) and fail everything
        in flight; nobody is left to answer it."""
        browsers, self.browsers = list(self.browsers.values()), {}
        if not browsers:
            return
        self._fail_pending(reason)
        for b in browsers:
            b.last_disconnect_at = datetime.now()
            b.last_close_code = WS_CLOSE_NORMAL
            b.last_close_reason = reason
            self.departed[b.instance_id] = self.departed_record(b)
            conn, b.ws = b.ws, None
            if conn is not None:
                self._close_socket(conn)
                logger.info(f"{b.describe()} connection closed")
        self._refresh_browsers_tool()

    def handle_hello(self, browser: Any, hello: Dict[str, Any]) -> Any:
        """Apply an extension_hello to its browser record; returns the record that owns
        the socket afterwards (the hello may promote/merge a provisional entry).

        Contract with the extension (background.js): every field is optional and may be
        absent on an older build; nothing here may depend on one being present.
          instance_id (or instanceId): UUID minted once per profile into storage.local.
          version, fileAccess {...}, proxyAccess {...}: as before.
          apiSupport {documentId: bool, ...}: optional platform APIs the extension
            PROBED for. Absent on a build older than the probe, which must not be read
            as "the API is missing"; absent means unknown.
          platform {os, arch}, browserInfo {name, vendor, version, buildID}: display
            only, and only as reliable as the browser's own report; a fork says "Firefox" here. It
            is all we get about a REMOTE browser, which has no process to probe.
          journal [{t, e, code?, reason?, attempt?}]: what the extension did between its
            last hello and this one - opened, closed, retried, was rejected. Capped and
            sanitized (connection_log.clean_journal) because it crosses the socket from a
            browser that may be remote, and it is logged and served unchanged.
        """
        instance_id = hello.get('instanceId') or hello.get('instance_id')
        if instance_id is not None and not isinstance(instance_id, str):
            instance_id = str(instance_id)
        if not instance_id and browser.browser_pid:
            # Version skew: an extension too old to mint an instance_id. Synthesize one
            # from the browser pid; stable for as long as that browser runs, so a
            # reconnect still finds its record. Degraded (a restart looks like a new
            # browser) but functional. Opaque key; no consumer parses it.
            instance_id = f"browser-pid:{browser.browser_pid}"
        if instance_id:
            try:
                browser = self.promote(browser, instance_id)
            except TakeoverRefused as refusal:
                # The record stays where it is; this socket goes. Its hello is not applied,
                # so nothing it claims (version, platform) lands on anyone.
                logger.warning(f"Hello rejected, {refusal}")
                if browser.ws is not None:
                    self._close_socket(browser.ws)
                return browser

        version = hello.get('version')
        if version:
            browser.version = version
        if 'apiSupport' in hello:
            browser.api_support = hello.get('apiSupport')
        if 'fileAccess' in hello:
            browser.file_access = hello.get('fileAccess')
        if 'proxyAccess' in hello:
            browser.proxy_access = hello.get('proxyAccess')
            # Show/hide the `proxy` arg on tabs/navigate to match the current grant.
            # Idempotent; a no-op unless the state flipped.
            try:
                self._refresh_proxy_tool_visibility()
            except Exception as e:
                logger.error(f"Failed to refresh proxy tool visibility: {e}")
        platform = hello.get('platform')
        if not isinstance(platform, dict):
            platform = {k: hello[k] for k in ('os', 'arch') if k in hello}
        if platform:
            browser.platform = platform
        browser_info = hello.get('browserInfo') or hello.get('browser_info')
        if isinstance(browser_info, dict):
            browser.browser_info = browser_info
            # A REMOTE browser has no process to probe, so the hello's self-declared
            # engine is the only authoritative signal we get. A Chromium port sends
            # engine:"blink"; Firefox sends no engine (stays whatever the probe found,
            # or None). Never overwrite a probe-derived engine with a missing one.
            eng = browser_info.get('engine')
            if isinstance(eng, str) and eng.strip():
                browser.engine = eng.strip().lower()

        # How long the hello took to arrive, measured once per socket. The extension
        # re-sends its hello every ~30s for the life of the connection; measuring there
        # too would report the age of the CONNECTION as the age of a hello, and every
        # long-lived browser would read as a sick one.
        accepted = browser.accepted_at
        if accepted is not None and (browser.hello_seen_at is None
                                     or browser.hello_seen_at < accepted):
            browser.hello_seen_at = time.monotonic()
            browser.hello_latency_ms = int(max(0.0, browser.hello_seen_at - accepted) * 1000)
            slow = connection_log.hello_latency_line(
                browser.describe(), browser.hello_latency_ms,
                window_seconds=self._hello_window())
            if slow:
                logger.info(slow)

        # What the extension says happened while it was away. The browser's own view is
        # the half of a wedge that no server-side log can reconstruct, and it is lost the
        # moment the browser is killed - which is how the investigation usually starts.
        journal = connection_log.clean_journal(hello.get('journal'))
        if journal:
            browser.reported_journal = journal
            logger.info(connection_log.journal_line(browser.describe(), journal))
        return browser
