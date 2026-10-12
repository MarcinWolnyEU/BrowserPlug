"""The one boundary between the connection report and the session detectors.

`kilocode_data`, `pi_data` and `copilot_data` read other agents' private state: Kilo's open
log, pi's session transcript, Copilot Chat's log and chat store. Nothing outside this
module imports them. Callers ask `detect()` or `snapshot()`, and both answer `{}` while
the switch is off, without touching the disk or the process table.

The switch is the popup's "Load Kilo session details" (`kiloLoadDetails` in the
extension's `storage.sync`). The extension sends it with every `server.get_connections`
poll and `record_switch()` keeps the last value it sent, in `config.json` under
`usage_panels`, so it outlives a restart. GET /health, `server_info` and every other
server-side caller read that value; before the popup has ever said, the default is on.
With several browsers the last poll wins.
"""
import logging
import threading

from browserplug_shared import config_store

from server.usage_panels import copilot_data, kilocode_data, pi_data

logger = logging.getLogger(__name__)

SECTION = "usage_panels"
KEY = "details"

_lock = threading.Lock()
_enabled = None          # None = not read from config.json yet
_warned = set()          # detector names that already logged a failure


def enabled():
    """Whether the detectors may read other agents' state. Default on."""
    global _enabled
    with _lock:
        if _enabled is None:
            try:
                _enabled = bool(config_store.read_section(SECTION).get(KEY, True))
            except Exception:
                _enabled = True
        return _enabled


def record_switch(value):
    """Remember the popup's switch. Writes config.json only when the value changes."""
    global _enabled
    value = bool(value)
    if enabled() == value:
        return
    with _lock:
        _enabled = value
    try:
        # Default (on) is stored as absence, like the other sections.
        config_store.write_section(SECTION, {} if value else {KEY: False})
    except Exception as e:
        logger.warning("could not persist the session-details switch: %s", e)


def _warn_once(name, error):
    with _lock:
        first = name not in _warned
        _warned.add(name)
    if first:
        logger.warning("%s session detection failed (logged once; the format is "
                       "private and may have changed): %s", name, error)


def _kilo(pid):
    pm = kilocode_data.detect_provider_model(pid)
    return {"provider": pm.get("provider"), "model": pm.get("model")} if pm else {}


def _pi(pid):
    pm = pi_data.detect_provider_model(pid)
    return {"provider": pm.get("provider"), "model": pm.get("model")} if pm else {}


def _copilot(pid):
    cp = copilot_data.detect_session(pid)
    if not cp:
        return {}
    extra = {"copilot": cp}
    if cp.get("model"):
        extra["provider"] = "github-copilot"
        extra["model"] = cp["model"]
    if cp.get("workspace"):
        extra["cwd"] = cp["workspace"]
        extra["label"] = f"GitHub Copilot Chat — {cp['workspace']}"
    return extra


# Display name of the MCP client (client_identity) -> detector.
_DETECTORS = {"Kilo CLI": _kilo, "pi": _pi, "GitHub Copilot Chat": _copilot}


def detect(friendly, pid, allowed=True):
    """Row fields for one client, or `{}`: no detector, switch off, no pid, or a miss.

    `allowed` is the caller's own veto (the poll's `kilo_details`); the stored switch
    applies on top of it.
    """
    fn = _DETECTORS.get(friendly)
    if fn is None or not pid or not allowed or not enabled():
        return {}
    try:
        return fn(pid)
    except Exception as e:
        _warn_once(friendly, e)
        return {}


def snapshot(rows, allowed=True):
    """Merge `detect()` into each `mcp_client` row (a dict with `friendly` and `pid`)."""
    if not allowed or not enabled():
        return rows
    for row in rows:
        if row.get("kind") == "mcp_client":
            row.update(detect(row.get("friendly"), row.get("pid")))
    return rows


def _reset():
    """Forget the in-memory state (tests only; config.json is left alone)."""
    global _enabled
    with _lock:
        _enabled = None
        _warned.clear()
