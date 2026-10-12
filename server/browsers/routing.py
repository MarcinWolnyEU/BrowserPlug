"""Which browser a client's call reaches: the client key, the selection rules, the pruning.

Pure functions over the registry's data, passed in. Nothing here owns state or imports
server.py: `selection` is the server's `browser_selection` dict (client_key -> instance_id)
and `departed` its `_departed` dict, and both are mutated in place exactly as the methods
this replaced did.

One client is known to the server as a KEY, best identity first (client_key below). The
same function builds the key on the tool side (server/tools/browser_calls.py) and reads it back on the
connection map (server.get_connections_info), so the two can no longer disagree about how
a pid is spelled.

Resolution order (resolve_target):
  1. explicit selection, still connected -> it
  2. explicit selection, GONE            -> an error naming the live browsers. Never a
     silent fallback: acting in the wrong logged-in browser is worse than an error.
  3. no selection, exactly 1 connected   -> it
  4. no selection, >=2 connected         -> the browser pinned in the server's settings
     when it is one of them, else first-connected (default_browser.preferred)
  5. nothing connected                   -> the legacy no-connection error
"""

import logging
from typing import Any, Dict, List, Optional, Union

from server.ws import connection_log
from server.browsers import default_browser as browser_pref
from server.browsers.routing_notice import DepartedBrowser

logger = logging.getLogger(__name__)

#: The key of a call with no HTTP context at all (in-process embed, unit tests): one
#: implicit client, the pre-selection behaviour.
LOCAL_CLIENT_KEY = "local"


def process_started(pid) -> Optional[int]:
    """The OS start time of `pid` in whole epoch seconds, None when it is not running or
    psutil cannot say. Module-level so tests can replace it with a fake process table."""
    try:
        import psutil
        return int(psutil.Process(int(pid)).create_time())
    except Exception:
        return None


def _epoch(started) -> Optional[int]:
    if started is None:
        return None
    if hasattr(started, "timestamp"):
        return int(started.timestamp())
    try:
        return int(float(started))
    except (TypeError, ValueError):
        return None


def pid_client_key(pid, started=None) -> str:
    """The key of a client identified by its process id and start time.

    The start time is what tells a client from a LATER process that inherited its pid;
    without it the newcomer would pick up the dead client's browser selection. `started`
    is an epoch, a datetime, or None to ask the OS. A pid that cannot be looked up keeps
    the bare "pid:N" spelling.
    """
    stamp = _epoch(started) if started is not None else process_started(pid)
    return f"pid:{pid}@{stamp}" if stamp is not None else f"pid:{pid}"


def client_key(client_pid=None, session_id=None, http_clients=None) -> str:
    """Identify the MCP client behind a tool call; the key its browser selection is
    stored under.

    Several Claude Codes (plus Duo, Kilo, Copilot…) share the one server, so the
    selection must never be global. Preference, best identity first:
      1. `x-browserplug-client-pid`; the stdio launcher forwards the REAL client's pid,
         which survives the proxy hop and a reconnect of its HTTP session.
      2. the pid the ASGI wrapper already resolved for this mcp session (it maps the
         launcher's peer port to its PARENT, i.e. the Claude Code that spawned it).
      3. the mcp session id; right client, but a new session loses the selection.
      4. "local"; no HTTP context at all.
    A pid key carries the process start time (pid_client_key).

    The first key resolved for a session sticks for that session's life (stored on its
    `http_clients` entry, so it dies with the entry), with one exception: the "session:"
    fallback is not stored while an identity lookup is in flight for the session (the
    entry's `identity_pending`). A lookup that was merely slow would otherwise pin a client
    that is about to be named to the session key, and whether it did would depend on a
    race. When the lookup lands, the caller moves what was filed under the placeholder
    (rekey_selection). A lookup that finds nothing leaves the session key final. Known limit:
    clients that share one parent process (VS Code's extension host serves several)
    forward the same pid and so share a key.

    `http_clients` is the server's mcp-session-id -> {pid, ...} table.
    """
    entry = None
    if session_id and http_clients is not None:
        entry = http_clients.get(session_id)
        if isinstance(entry, dict) and entry.get("client_key"):
            return entry["client_key"]
    if client_pid:
        key = pid_client_key(client_pid)
    elif session_id:
        e = entry if isinstance(entry, dict) else {}
        if e.get("pid"):
            # The entry's own start time is the one taken when the client was described;
            # ask the OS only when that is missing.
            key = pid_client_key(e["pid"], e.get("start_time"))
        else:
            key = f"session:{session_id}"
    else:
        return LOCAL_CLIENT_KEY
    if isinstance(entry, dict):
        # While the lookup is in flight the placeholder is not final: the pid may arrive.
        if not (key.startswith("session:") and entry.get("identity_pending")):
            entry["client_key"] = key
    return key


def rekey_selection(selection: Dict[str, str], session_id, http_clients) -> bool:
    """Move a browser selection made under the session's placeholder key to the key the
    client has now that its identity is known. True when something moved.

    A selection already filed under the real key wins: that is the same client's earlier
    session, and the placeholder's is dropped. Call it when the identity lookup lands."""
    old = f"session:{session_id}"
    if old not in selection:
        return False
    new = client_key(None, session_id, http_clients)
    if new == old:
        return False
    chosen = selection.pop(old, None)
    if chosen and new not in selection:
        selection[new] = chosen
    return True


def prune_selection(selection: Dict[str, str], departed: Dict[str, Any], live: List[Any]):
    """Drop selections pointing at a browser that is no longer connected, but ONLY
    while ≤1 browser remains.

    With ≥2 alive the `browsers` tool exists and a stale selection is an ERROR the
    agent can act on (pick another). With ≤1 alive that tool has just been hidden, so
    the error would be unrecoverable: the agent would be stuck, unable to select the
    only browser left. Dropping there is what keeps "the tool exists only at ≥2"
    coherent.
    """
    if len(live) > 1:
        return
    alive = {b.instance_id for b in live}
    survivor = live[0].describe() if live else "nothing (no browser is connected)"
    for key, instance_id in list(selection.items()):
        if instance_id not in alive:
            # pop, not del: two calls can prune the same stale key at once.
            selection.pop(key, None)
            # Name both browsers and say why the first one went. A line naming
            # neither would read "dropped a uuid" while the agent on the other
            # end is quietly served by a browser it never asked for.
            gone = departed.get(instance_id)
            went = (f" ({connection_log.close_phrase(gone.close_code, gone.close_reason)}"
                    f"{' at ' + gone.at_hms if gone.at_hms else ''})"
                    if isinstance(gone, DepartedBrowser) else "")
            logger.info(f"Dropped {key}'s selection of {gone or instance_id}"
                        f"{went} — ≤1 browser left, so its calls now route to "
                        f"{survivor}")


def resolve_target(live: List[Any], selection: Dict[str, str], departed: Dict[str, Any],
                   key: str) -> Union[Any, str]:
    """The browser this MCP client's call goes to, or an agent-facing ERROR STRING (the
    caller turns it into the tool's {"error": ...}). Rules in the module docstring.

    Rule 4's pin is a preference and falls back silently when the pinned browser is
    not connected; rule 2's selection is a decision this session made and does not.
    Prunes first (a stale selection must be dropped BEFORE rule 2 reads it).
    """
    # A socket that has not said hello has no identity yet (its key is a server-side
    # placeholder): it is not a browser a call may be routed to.
    live = [b for b in live if getattr(b, "provisional", False) is not True]
    prune_selection(selection, departed, live)
    if not live:
        return "No extension connection available"

    # A call is proof the client is alive; a plain dict (tests) has nothing to refresh.
    touch = getattr(selection, "touch", None)
    if touch is not None:
        touch(key)
    selected = selection.get(key)
    if selected:
        for b in live:
            if b.instance_id == selected:
                return b
        gone = departed.get(selected, f"the browser you selected ({selected})")
        roster = ", ".join(b.describe() for b in live)
        return (f"{gone} is no longer connected, and it is the browser this session "
                f"selected — refusing to fall back to another one (acting in the wrong "
                f"logged-in browser is worse than an error). Connected now: {roster}. "
                f'Switch with browsers(action="use", target=<slot or name>), or just '
                f"wait: if that browser restarts it reconnects as the same browser and "
                f"your selection starts working again.")

    return browser_pref.preferred(live)


def routed_browser(live: List[Any], selection: Dict[str, str], key: str) -> Optional[Any]:
    """Which browser this client's calls would reach right now, or None.

    Read-only twin of resolve_target: same rules, but it never prunes the selection,
    never returns an error string, and returns None where that one would error (a
    selection pointing at a departed browser routes NOWHERE, so the connection map must
    not draw a line to whatever happens to be first). A diagnostic read must not mutate
    routing state.
    """
    if not live:
        return None
    selected = selection.get(key)
    if selected:
        for b in live:
            if b.instance_id == selected:
                return b
        return None
    return browser_pref.preferred(live)


def preferred_browser(live: List[Any]) -> Optional[Any]:
    """Where a client with NO selection of its own lands: the browser pinned in the
    server's settings when it is connected, else first-connected.

    This is the answer routing rule 4 gives, and the only thing a roster may call
    "the server default". The registry's `default_browser` is NOT it: that one is the
    legacy single-socket compat answer, it predates pinning, and every display that read
    it was announcing Chrome as the default while the pinned Firefox took the calls
    (live 2026-08-18).
    """
    return browser_pref.preferred(live)
