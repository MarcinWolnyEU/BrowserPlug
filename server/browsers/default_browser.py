#!/usr/bin/env python3
"""Which browser a call goes to when the session never picked one, and whether the
server may start that browser itself.

Two settings, both persisted by the server (config.json, section `default_browser`),
because the answer has to survive the browser it names being closed:

  mode          "first_started" (the historical behaviour) or "pinned"
  instance_id   the pinned browser's profile identity, when mode is "pinned"
  auto_launch   may the server start that browser when nothing is connected at all
  launch_args   extra command-line arguments for that start; local only (config.json),
                never accepted from a WebSocket peer

A pinned browser is remembered by more than its `instance_id`: the stored entry also
carries the name, engine and executable path the browser had when it was picked. That
snapshot is what lets the options page keep listing a pinned browser after it has been
closed, and it is the only thing `auto_launch` has to work with, since a closed browser
is not in the registry to be asked.

The pin is a PREFERENCE, not the per-session selection the `browsers` tool writes. A
session that selected a browser explicitly still refuses to fall back when that one
leaves (see resolve_target_browser); the pin only decides which of several connected
browsers a session with no selection of its own lands on. Pinning a browser that is not
running therefore routes to whatever IS running rather than failing: the pin says
"prefer this one", and a hard failure would take the multi-browser default away from
every client the moment one browser was closed.
"""
import asyncio
import logging
import os
import shlex
import subprocess
import time

from browserplug_shared.config_store import read_section, write_section
from browserplug_shared.mcp_port import detached_spawn_kwargs

logger = logging.getLogger(__name__)

CONFIG_SECTION = "default_browser"

MODE_FIRST_STARTED = "first_started"
MODE_PINNED = "pinned"
MODES = (MODE_FIRST_STARTED, MODE_PINNED)

# How long a launched browser gets to connect before the call that triggered it gives
# up. A cold start plus profile load is several seconds on a slow disk; past this the
# caller is better served by the ordinary "nothing is connected" error than by a wait
# that outlives its own timeout.
LAUNCH_WAIT_ENV = "BROWSERPLUG_BROWSER_LAUNCH_WAIT"
LAUNCH_WAIT_DEFAULT = 25.0
LAUNCH_WAIT_MAX = 90.0

# A browser that was started and did not connect must not be started again on the next
# call: a failing launch would otherwise open a window per tool call.
LAUNCH_RETRY_COOLDOWN_SECONDS = 60.0

_POLL_INTERVAL_SECONDS = 0.25


def defaults():
    """The fresh-install answer: first-connected wins, and the server starts nothing."""
    return {
        "mode": MODE_FIRST_STARTED,
        "instance_id": None,
        "name": None,
        "engine": None,
        "exe_path": None,
        "auto_launch": False,
        "launch_args": "",
    }


def _text(value):
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def normalize(raw):
    """Coerce anything readable into a full settings dict.

    A hand-edited or half-written config must never stop the server, so every unusable
    field falls back to its default instead of raising. The one rule with teeth: a
    "pinned" mode with no instance_id is not pinned at all, and reverts.
    """
    out = defaults()
    if not isinstance(raw, dict):
        return out

    mode = raw.get("mode")
    if mode in MODES:
        out["mode"] = mode
    out["instance_id"] = _text(raw.get("instance_id"))
    out["name"] = _text(raw.get("name"))
    out["engine"] = _text(raw.get("engine"))
    out["exe_path"] = _text(raw.get("exe_path"))
    out["auto_launch"] = bool(raw.get("auto_launch"))
    args = raw.get("launch_args")
    out["launch_args"] = args.strip() if isinstance(args, str) else ""

    if out["mode"] == MODE_PINNED and not out["instance_id"]:
        out = defaults() | {"launch_args": out["launch_args"]}
    if out["mode"] == MODE_FIRST_STARTED:
        # The checkbox only exists next to a pinned browser, and there is nothing to
        # start without one. Storing it as True here would leave a switch set that the
        # options page never shows.
        out["auto_launch"] = False
    return out


def read():
    """The stored settings, normalized. Never raises, never returns a partial dict."""
    return normalize(read_section(CONFIG_SECTION))


def write(settings):
    """Persist `settings` (already normalized). Returns the config path, or None."""
    return write_section(CONFIG_SECTION, settings)


# --- Choosing a browser -----------------------------------------------------------

def preferred(live, settings=None):
    """The browser a client with no selection of its own should reach, out of `live`.

    `live` is in slot order, so element 0 is the first-connected survivor: the answer
    for "first_started", and the fallback whenever the pinned browser is not among the
    connected ones.
    """
    live = list(live or [])
    if not live:
        return None
    settings = settings or read()
    if settings.get("mode") == MODE_PINNED and settings.get("instance_id"):
        pinned = settings["instance_id"]
        for browser in live:
            if getattr(browser, "instance_id", None) == pinned:
                return browser
    return live[0]


def display_name(browser):
    """What to call a browser in the picker: its brand, without the process id.

    `ExtBrowser.name` carries "Firefox Developer Edition pid 48116", which is the right
    answer for an agent choosing between sockets and the wrong one for a person choosing
    a browser. A REMOTE browser keeps its address, since two of them are otherwise
    indistinguishable and there is no process behind either to tell them apart.
    """
    edition = getattr(browser, "edition", None)
    if getattr(browser, "is_remote", False):
        addr = getattr(browser, "remote_addr", None)
        if addr:
            return f"{edition} ({addr})" if edition else f"remote-{addr}"
    return edition or getattr(browser, "name", None)


def _browser_entry(browser, settings, requester_id):
    instance_id = getattr(browser, "instance_id", None)
    exe_path = getattr(browser, "exe_path", None)
    remote = bool(getattr(browser, "is_remote", False))
    return {
        "instance_id": instance_id,
        "slot": getattr(browser, "slot", None),
        "name": display_name(browser),
        "engine": getattr(browser, "engine", None),
        "exe_path": exe_path,
        "remote": remote,
        "connected": True,
        "is_current": bool(requester_id) and instance_id == requester_id,
        "is_selected": settings.get("instance_id") == instance_id,
        # A browser the server could start again later. It needs a local executable to
        # run; a remote browser lives on a machine this process cannot reach.
        "can_launch": bool(exe_path) and not remote,
    }


def payload(server, requester=None):
    """Everything the options page needs to draw the setting in one round-trip.

    The candidate list is the connected browsers, plus the pinned one when it is not
    among them, flagged `connected: false`. Dropping a closed pin from the list would
    make the page render "First started" for a server that is still pinned, and the
    next save would silently clear a setting the user never touched.
    """
    settings = read()
    requester_id = getattr(requester, "instance_id", None)

    try:
        live = list(server.live_browsers())
    except Exception:
        live = []

    browsers = [_browser_entry(b, settings, requester_id) for b in live]

    pinned = settings.get("instance_id")
    if pinned and not any(b["instance_id"] == pinned for b in browsers):
        browsers.append({
            "instance_id": pinned,
            "slot": None,
            "name": settings.get("name") or "the browser you selected",
            "engine": settings.get("engine"),
            "exe_path": settings.get("exe_path"),
            "remote": False,
            "connected": False,
            "is_current": False,
            "is_selected": True,
            "can_launch": bool(settings.get("exe_path")),
        })

    return {
        "settings": settings,
        "browsers": browsers,
        "current_instance_id": requester_id,
        "launch_wait_seconds": launch_wait_seconds(),
    }


def apply_request(server, request, requester=None):
    """Validate a settings write from the options page and persist it.

    Returns `(payload, None)` or `(None, error)`; the error text is shown to the user,
    so it says what to do rather than what was wrong with the JSON.
    """
    if not isinstance(request, dict):
        return None, "the settings payload must be an object"

    mode = request.get("mode")
    if mode not in MODES:
        return None, f"mode must be one of: {', '.join(MODES)}"

    stored = read()
    settings = defaults()
    settings["mode"] = mode
    # launch_args become the argv of a process the server starts, and Chromium switches
    # like --renderer-cmd-prefix turn an argument into a command. This request comes over
    # the extension WebSocket from any peer holding a token, a phone included, so it may
    # never set them: they are edited in config.json on this machine. What is stored is
    # kept whatever the mode, so switching the checkbox off and back on loses nothing.
    settings["launch_args"] = stored.get("launch_args") or ""
    if "launch_args" in request:
        sent = request.get("launch_args")
        sent = sent.strip() if isinstance(sent, str) else ""
        if sent != settings["launch_args"]:
            logger.warning(
                "Ignored launch_args in a default-browser write from a WebSocket peer; "
                "they can only be set in config.json on the server host")

    if mode == MODE_PINNED:
        instance_id = _text(request.get("instance_id"))
        if not instance_id:
            return None, "pick a browser to make it the default"

        chosen = None
        try:
            for browser in server.live_browsers():
                if getattr(browser, "instance_id", None) == instance_id:
                    chosen = browser
                    break
        except Exception:
            chosen = None

        if chosen is not None:
            settings["instance_id"] = instance_id
            # The picker's name, so a browser that is later closed keeps reading the
            # same way on the list as it did when it was connected.
            settings["name"] = display_name(chosen)
            settings["engine"] = getattr(chosen, "engine", None)
            settings["exe_path"] = (None if getattr(chosen, "is_remote", False)
                                    else getattr(chosen, "exe_path", None))
        elif stored.get("instance_id") == instance_id:
            # Re-saving the pinned-but-closed browser the page is still listing. Its
            # snapshot is the only description of it left, so carry it over rather
            # than storing an id with no name and no way to start it.
            settings["instance_id"] = instance_id
            settings["name"] = stored.get("name")
            settings["engine"] = stored.get("engine")
            settings["exe_path"] = stored.get("exe_path")
        else:
            return None, ("that browser is not connected, so the server has nothing to "
                          "remember about it; pick one from the list")

        settings["auto_launch"] = bool(request.get("auto_launch"))
        if settings["auto_launch"] and not settings["exe_path"]:
            return None, ("the server does not know how to start that browser (no local "
                          "program path), so it cannot open it for you")

    settings = normalize(settings)
    if write(settings) is None:
        return None, "the server could not write its settings file"
    logger.info("Default browser set to %s%s",
                settings["name"] or settings["mode"],
                " (may be started by the server)" if settings["auto_launch"] else "")
    return payload(server, requester), None


# --- Starting a browser -----------------------------------------------------------

def launch_wait_seconds():
    """How long to wait for a launched browser to connect."""
    try:
        value = float(os.environ.get(LAUNCH_WAIT_ENV) or LAUNCH_WAIT_DEFAULT)
    except (TypeError, ValueError):
        return LAUNCH_WAIT_DEFAULT
    return max(1.0, min(value, LAUNCH_WAIT_MAX))


def split_launch_args(text):
    """User-typed arguments as an argv list.

    `posix=False` keeps Windows paths intact (posix mode reads every backslash as an
    escape and `C:\\Users\\me` comes back as `C:Usersme`), at the cost of leaving the
    quotes on a quoted token, which is what the strip below removes.
    """
    if not text or not text.strip():
        return []
    try:
        tokens = shlex.split(text, posix=False)
    except ValueError:
        # Unbalanced quotes. Whitespace splitting is a worse answer than an argument
        # list the user can see is wrong, but it is better than refusing to start.
        tokens = text.split()
    out = []
    for token in tokens:
        if len(token) >= 2 and token[0] == token[-1] and token[0] in ("'", '"'):
            token = token[1:-1]
        if token:
            out.append(token)
    return out


def launch_command(settings):
    """(argv, None) for starting the pinned browser, or (None, reason)."""
    if settings.get("mode") != MODE_PINNED:
        return None, "no browser is pinned as the default"
    if not settings.get("auto_launch"):
        return None, "the server is not allowed to open this browser"
    exe = settings.get("exe_path")
    if not exe:
        return None, "the server does not know which program to run"
    if not os.path.exists(exe):
        return None, f"the program is no longer at {exe}"
    return [exe] + split_launch_args(settings.get("launch_args")), None


class LaunchGuard:
    """One launch attempt at a time, and no retry storm after a failed one.

    Every tool call that finds nothing connected would otherwise start its own browser,
    and a browser that starts but never connects (wrong profile, extension disabled)
    would open a window per call for as long as the agent kept trying.
    """

    def __init__(self):
        self._lock = asyncio.Lock()
        self._blocked_until = 0.0

    def _cooling_down(self):
        return time.monotonic() < self._blocked_until

    def note_failure(self):
        self._blocked_until = time.monotonic() + LAUNCH_RETRY_COOLDOWN_SECONDS

    def clear(self):
        self._blocked_until = 0.0

    async def ensure_running(self, server, settings=None):
        """Start the pinned browser and wait for it to connect. Returns a browser, or None.

        None is not an error path of its own: the caller already had nothing to route
        to and reports that, so a launch that could not happen changes nothing about
        what the agent is told.
        """
        settings = settings or read()
        argv, reason = launch_command(settings)
        if argv is None:
            logger.debug("Not starting a browser: %s", reason)
            return None

        async with self._lock:
            # Another call may have started it while this one waited for the lock.
            existing = preferred(_live(server), settings)
            if existing is not None:
                return existing
            if self._cooling_down():
                logger.debug("Not starting a browser: still cooling down after a failed start")
                return None

            try:
                # Popen on Windows takes tens of ms (CreateProcess); off the loop so other
                # clients' tool calls and /health are not stalled while it runs.
                await asyncio.to_thread(
                    lambda: subprocess.Popen(argv, close_fds=True, **detached_spawn_kwargs()))
            except OSError as e:
                logger.warning("Could not start %s: %s", argv[0], e)
                self.note_failure()
                return None

            logger.info("Started %s and waiting up to %.0fs for it to connect",
                        settings.get("name") or argv[0], launch_wait_seconds())
            deadline = time.monotonic() + launch_wait_seconds()
            while time.monotonic() < deadline:
                await asyncio.sleep(_POLL_INTERVAL_SECONDS)
                browser = preferred(_live(server), settings)
                if browser is not None:
                    self.clear()
                    logger.info("Started browser connected as %s", browser.describe())
                    return browser

            logger.warning("Started %s but it did not connect within %.0fs",
                           settings.get("name") or argv[0], launch_wait_seconds())
            self.note_failure()
            return None


def _live(server):
    try:
        return list(server.live_browsers())
    except Exception:
        return []
