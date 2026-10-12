#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""Preflight: "is BrowserPlug installed and working?" answered in ONE call, including
when the thing being asked about is dead.

Two sessions burned 85 tool calls reaching a first successful BrowserPlug call and made
ZERO BrowserPlug calls in the end. The diagnostic tool was itself part of the problem:
`server_info` answers over the very MCP transport it would diagnose, so when the
install collapsed it replied "The socket connection was closed unexpectedly"; it
only speaks when everything it checks is already healthy.

So this module inverts the dependency. Every precondition is reported as its OWN
pass/fail item that NAMES ITS OWN REMEDY, and the whole thing is stdlib-only: it
imports nothing from mcp/websockets/uvicorn at module level (psutil is
reached only inside a try, to name who holds a port), because *their absence is
one of the things it diagnoses* (the observed failure was a bare
`ModuleNotFoundError: websockets`). The project imports are `mcp_port`, which is
stdlib-only for the same reason and already owns the port policy (the seek is
reused, never reimplemented, so the doctor walks exactly the sequence a launcher
walks), and `server_env`, which owns the answer to "which interpreter would a launcher
start the server with".

Shape: the CHECKS are **pure functions over injected state**; all I/O lives in thin
`collect_*` collectors, and every collector degrades to an explicit
"could not check, because X" item (`ok=None`) rather than raising. That is
`wan_diagnosis.py`'s rule: a diagnostic must never become a new way to break,
and it is what makes the three diagnosed scenarios (a checkout whose server environment
is missing or broken; valid install with the extension disconnected; running server whose
WebSocket was severed) unit-testable with no browser, no server and no network.

Three surfaces, one implementation:
  * CLI: `python server/preflight.py [--json]` (or `uv run --script server/preflight.py`),
    the ONLY surface that works when the server process is gone (run it with ANY Python
    3.9+; it is the SCRIPT path because this file imports nothing from the package, and
    the dependencies it is here to find missing are the ones the rest of the package
    needs). It changes nothing: uv is asked only read-only questions.
  * HTTP, `GET /doctor` on the MCP port, beside /health: answers when the WS to the
    browser is dead.
  * MCP,  `server_info` prepends `render(run_checks(probe_server=False))` when any
    check failed, so the failed precondition is stated before the roster.

Copyright 2026 Marcin Wolny
Licensed under the MIT License - see LICENSE file for details
"""
from __future__ import annotations

import asyncio
import importlib
import json
import os
import re
import subprocess
import sys
import time

# Import roots, computed from __file__ and never from the cwd. shared-py/ is a folder, not a
# package: it holds the one package `browserplug_shared`, so the folder goes on sys.path.
# The repo root makes `server` importable. Run as a script (`python server/preflight.py`),
# sys.path[0] is server/ itself, whose server.py would shadow the `server` package, so it
# is dropped first.
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if __package__ in (None, ""):
    sys.path[:] = [p for p in sys.path
                   if os.path.normcase(os.path.abspath(p or os.curdir)) != os.path.normcase(_HERE)]
for _path in (os.path.join(_ROOT, "shared-py"), _ROOT):
    if _path not in sys.path:
        sys.path.insert(0, _path)

# The port policy (default port, the 2830-2942 range, the BROWSERPLUG_MCP_PORT pin, the
# /health identity check) is imported as a MODULE, not as bare names: the doctor must
# walk exactly the sequence launcher.py walks, and holding the module makes that
# reuse obvious (and monkeypatchable in tests). The bootstrap above makes the same
# absolute imports work as a script and as part of the `server` package.
from browserplug_shared import mcp_port, server_env
# The one definition of "this machine" (stdlib-only, so the doctor stays importable).
from server.ws import http_auth


# The order they are reported in is deliberately causal: an interpreter that cannot
# run explains a missing dependency, which explains a server that never bound a port,
# which explains an extension that has nothing to dial. Reading top-down, the FIRST
# failure is the one to fix.
CHECKS = ("interpreter", "dependencies", "mcp_port", "server_process",
          "extension", "heartbeat", "client_config")

# The WebSocket port never moves (.tmp/dev-docs/ports.md), so a foreign holder on it is always
# a finding. Mirrors the server's --port default.
WS_PORT = 8772

# What server.py imports first: the same names as server_env.PROBE_IMPORTS, all declared in
# server/server.py's PEP 723 block (test_preflight pins both). Presence only: this never
# IMPORTS them (see _PROBE_SOURCE): importing mcp costs a second and can fail for reasons
# that have nothing to do with whether it is installed.
REQUIRED_PACKAGES = ("websockets", "mcp", "uvicorn", "pydantic", "psutil", "cryptography")

# A wedged/half-installed interpreter must not hang the doctor: it is the surface a
# human runs when nothing else answers.
PROBE_TIMEOUT_SECONDS = 10

# The extension health-probes the server over the WS every 15 s
# (HEALTH_PROBE_INTERVAL_MS in background.js), and every message refreshes the
# server's `last_seen`. Eight missed probes is not "idle", it is a socket that both
# ends still believe in: the half-open state a page tool would hang on.
HEARTBEAT_STALE_SECONDS = 120

# ~/.claude.json accumulates per-project history and can reach tens of MB. Reading it
# is fine; parsing an arbitrarily large one is not, so a file past this is reported as
# "could not check" instead of being loaded.
MAX_CONFIG_BYTES = 16 * 1024 * 1024


# Remedies live here rather than inline: each one is read by an AI agent mid-task, so
# it has to be a command it can actually run, not a description of one.
def _rebuild_env(root, state: dict | None = None) -> str:
    """The remedy for a server environment that will not run. Under BROWSERPLUG_SERVER_PYTHON
    nothing rebuilds it for you: the packages are the owner's, so the remedy is pip."""
    state = state or {}
    # A server that answers for itself (in_process) runs on the override when the variable is
    # in its own environment.
    override = (state.get("mode") == "override"
                or (state.get("mode") == "in_process"
                    and os.environ.get(server_env.SERVER_PYTHON_ENV)))
    if override and state.get("python"):
        python = state["python"]
        return (f"reinstall the server's dependencies into {python} (named by "
                f"{server_env.SERVER_PYTHON_ENV}): stop the server with python "
                f"server/control.py shutdown, run `{python} -m pip install --upgrade pip`, "
                f"then `{python} -m pip install --requirements-from-script "
                f"{os.path.join(root, 'server', 'server.py')}`, then start the server "
                f"(`{server_env.START_COMMAND}`)")
    return (f"rebuild the server environment: from {root} run "
            f"`{server_env.START_COMMAND}` (stop a running server first with python "
            f"server/control.py shutdown; if it reports a stale lock, run "
            f"`{server_env.LOCK_COMMAND}` first)")


_REPAIR_RUNNING_ENV = (f"stop the server (python server/control.py shutdown), then run "
                       f"`{server_env.START_COMMAND}` or reconnect an MCP client: either one "
                       f"deletes the damaged environment and rebuilds it")
_START_SERVER = ("start the shared server DETACHED: run `python server/control.py start`, or "
                 "open any MCP client, whose launcher (server/launcher.py) seeks the shared "
                 "server and spawns server/server.py detached if none answers. Do not run "
                 "server.py in the foreground from an "
                 "agent shell: it dies with that shell. If none ever appears, read "
                 "the newest browserplug-server*.startup.log next to browserplug-server.log "
                 "(one per launcher spawn), or server/browserplug-restart*.startup.log after "
                 "a /restart with BROWSERPLUG_LOG_FILE unset (one per restart); they capture "
                 "a crash that happens before logging is configured")
_CONNECT_EXTENSION = ('open the browser, enable the BrowserPlug extension, and check that its '
                      'popup says "Connected"; a browser on ANOTHER machine additionally '
                      'needs the WS token pasted into the extension\'s options page (the '
                      '/ws-token endpoint is localhost-only, so it cannot fetch its own)')
_RELOAD_EXTENSION = ("reload the extension (about:addons ▸ BrowserPlug ▸ Reload, or "
                     "chrome://extensions ▸ Reload) or click Reconnect in its popup; "
                     "the server only discovers a half-open socket when it next writes to it")

# Where each MCP client keeps its OWN registry (they do not share one: adding BrowserPlug
# to one client does not make the others see it). `key` is the top-level object the
# servers live under; the parsers try all of them per file, because a hand-edited
# config often uses the neighbouring client's spelling.
_CLIENT_CONFIGS = (
    ("Kilo CLI", "root", "kilo.json"),
    ("VS Code / GitHub Copilot", "appdata", os.path.join("Code", "User", "mcp.json")),
    ("GitLab Duo", "appdata", os.path.join("GitLab", "duo", "mcp.json")),
    ("Claude Code", "home", ".claude.json"),
    ("Claude Code (project .mcp.json)", "root", ".mcp.json"),
    ("Claude Code (project .mcp.json)", "cwd", ".mcp.json"),
)
_SERVER_MAP_KEYS = ("mcpServers", "servers", "mcp")
# The other clients known to register BrowserPlug, the TOML ones included. A file here is
# reported only when it holds a BrowserPlug entry (by its name, or the launcher path it
# runs): these clients are often in use for other servers alone.
_EXTRA_CLIENT_CONFIGS = (
    ("pi", "home", os.path.join(".pi", "agent", "mcp.json")),
    ("Codex CLI", "home", os.path.join(".codex", "config.toml")),
    ("Mistral Vibe", "home", os.path.join(".vibe", "config.toml")),
    ("Mistral Vibe", "home", os.path.join(".vibe", "mcp.json")),
    ("Reasonix", "root", "reasonix.toml"),
)
# The server tables of a TOML config: Codex's [mcp_servers.<name>], Mistral Vibe's
# [[mcp_servers]] and Reasonix's [[plugins]].
_TOML_SERVER_KEYS = ("mcp_servers", "plugins")
# Every file known to have started BrowserPlug from the checkout's `.venv` python, read as
# text. Spawns no longer use .venv, but a client config written before the uv move may still
# name it as the launcher's python, and fails once .venv is deleted.
_VENV_SCAN_CONFIGS = _CLIENT_CONFIGS + _EXTRA_CLIENT_CONFIGS


def _rows(value) -> list:
    """The dict rows of a /health array, whatever junk the key actually holds.

    Every array this module reads comes off the wire, so "connections" can be an int
    and "browsers" can be a string. A diagnostic that raises on a malformed payload
    has become the second failure, so every list access goes through here.
    """
    if not isinstance(value, (list, tuple)):
        return []
    return [item for item in value if isinstance(item, dict)]


def _check(name: str, ok, detail: str, remedy: str | None = None) -> dict:
    """One reported precondition.

    ok is True / False / **None**, and None is load-bearing: it means "could not
    check, because <detail>". A diagnostic that silently downgrades "I don't know"
    into "it's broken" sends the reader after the wrong thing, which is the whole
    failure this module exists to stop.
    """
    return {"name": name, "ok": ok, "detail": detail, "remedy": remedy}


def _format_age(seconds) -> str:
    """3s / 4m12s / 1h02m: compact enough to sit inside a one-line detail."""
    try:
        seconds = int(max(0, float(seconds)))
    except (TypeError, ValueError):
        return "unknown"
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def repo_root() -> str:
    """The checkout this file lives in (server/preflight.py -> the parent of server/)."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------------------------------------------------------------------------
# Collectors: all the I/O, none of the judgement. Each returns a plain dict and
# NEVER raises; the pure check functions below decide what it means.
# ---------------------------------------------------------------------------

# Printed as TWO json lines so a probe that dies half-way still yields the first one:
# the interpreter facts are exactly what you need when a dependency probe crashes the
# process. Presence is tested with find_spec (no import, no side effects, fast);
# versions come from importlib.metadata, which also does not import the package.
_PROBE_SOURCE = r'''
import json, sys
print(json.dumps({"executable": sys.executable,
                  "version": ".".join(str(p) for p in sys.version_info[:3]),
                  "base_prefix": getattr(sys, "base_prefix", sys.prefix)}), flush=True)
found = {}
try:
    import importlib.util as _util
    try:
        from importlib.metadata import version as _version
    except Exception:
        _version = None
    for _name in sys.argv[1:]:
        try:
            _spec = _util.find_spec(_name)
        except Exception:
            _spec = None
        if _spec is None:
            found[_name] = None
            continue
        try:
            found[_name] = _version(_name) if _version else "installed"
        except Exception:
            found[_name] = "installed"
except Exception as exc:
    found = {"__probe_error__": repr(exc)}
print(json.dumps(found), flush=True)
'''


def _no_window_kwargs() -> dict:
    """CREATE_NO_WINDOW, for the same reason mcp_port.detached_spawn_kwargs() uses it.

    A uv environment's `Scripts/python.exe` is a trampoline that re-execs the real
    interpreter as a child with no creation flags of its own; a console app whose parent
    has no console and asked for nothing gets a FRESH, VISIBLE one from Windows. The server
    runs windowless, so probing it from there would pop a terminal on the user's desktop.
    NOT detached_spawn_kwargs(): this child is waited on, so it must stay in our process
    group and must not be given a new session.
    """
    return {"creationflags": mcp_port.CREATE_NO_WINDOW} if os.name == "nt" else {}


def _read_pyvenv_cfg(env_dir: str) -> dict:
    """The `key = value` pairs of <env>/pyvenv.cfg, or {} if unreadable.

    utf-8-SIG: a hand-edited pyvenv.cfg (Notepad, PowerShell's `Set-Content -Encoding
    utf8`) carries a BOM, and without stripping it the FIRST key reads as "﻿home",
    so `home` silently goes missing and the report loses the one line that explains
    an environment whose base interpreter is gone. We are the only reader here, so be
    tolerant.
    """
    out: dict = {}
    try:
        with open(os.path.join(env_dir, "pyvenv.cfg"), "r", encoding="utf-8-sig",
                  errors="replace") as fh:
            for line in fh:
                key, sep, value = line.partition("=")
                if sep:
                    out[key.strip().lower()] = value.strip()
    except Exception:
        return {}
    return out


def _pyvenv_facts(env_dir) -> dict:
    cfg = _read_pyvenv_cfg(env_dir) if env_dir else {}
    home = cfg.get("home")
    return {"cfg_home": home, "cfg_home_exists": (os.path.isdir(home) if home else None),
            "cfg_version": cfg.get("version")}


def _answers_in_process(root: str, packages) -> bool:
    """Is THIS interpreter the one the question is about?

    True inside the server (server_info, /doctor): the checkout being asked about is the
    one this file lives in, this is a venv interpreter, and every required package is
    findable here. Then spawning a copy of this interpreter, or asking uv about it, would
    be theatre, and on the server_info path it would block the loop on every call.
    """
    try:
        if not _same_path(root, repo_root()):
            return False
        if _same_path(sys.prefix, getattr(sys, "base_prefix", sys.prefix)):
            return False
        import importlib.util
        return all(importlib.util.find_spec(name) is not None for name in packages)
    except Exception:
        return False


def collect_interpreter(root: str | None = None,
                        packages=REQUIRED_PACKAGES,
                        timeout: int = PROBE_TIMEOUT_SECONDS,
                        *, in_process: bool | None = None, environ=None,
                        inspect=None) -> dict:
    """Everything both the `interpreter` and the `dependencies` checks need, about the
    ONE interpreter a launcher would start the server with.

    One probe, not two, and the reason is not just cost: the interpreter facts and
    the module inventory have to describe the SAME interpreter, or a report could
    name a Python that ran and a `websockets` that is missing from a different one.

    In-process (`in_process`, auto-detected by _answers_in_process when None): the
    interpreter running this check answers for itself, with no subprocess and no uv call,
    and reports whether its own environment was gutted under it. Standalone (the CLI):
    server_env.inspect_server_env (`inspect`, for tests) says which interpreter a launcher
    would start, using read-only uv calls only, and one subprocess inventories its packages.
    """
    root = root or repo_root()
    state = {
        "root": root,
        "mode": None,
        "python": None,
        "python_exists": False,
        "env_dir": None,
        "uv": None,
        "env_state": None,
        "problem": None,
        "remedy": None,
        "uv_output": "",
        "damaged": None,
        "cfg_home": None,
        "cfg_home_exists": None,
        "cfg_version": None,
        "ran": None,
        "in_process": False,
        "executable": None,
        "runtime_version": None,
        "base_prefix": None,
        "modules": None,
        "error": None,
    }
    if in_process is None:
        in_process = _answers_in_process(root, packages)
    if in_process:
        state.update(mode="in_process", python=sys.executable, python_exists=True,
                     env_dir=sys.prefix)
        state.update(_pyvenv_facts(sys.prefix))
        state.update(_probe_in_process(packages))
        try:
            state["damaged"] = server_env.running_env_damage()
        except Exception:
            state["damaged"] = None
        return state

    try:
        info = (inspect or server_env.inspect_server_env)(root, environ=environ)
    except Exception as exc:
        info = {"problem": f"could not ask which interpreter a launcher would start: "
                           f"{type(exc).__name__}: {exc}"}
    for key in ("mode", "python", "env_dir", "uv", "env_state", "problem", "remedy",
                "uv_output"):
        if info.get(key) is not None:
            state[key] = info[key]
    python = state["python"]
    state["python_exists"] = bool(python) and os.path.isfile(python)
    state.update(_pyvenv_facts(state["env_dir"]))
    if state["problem"] or not state["python_exists"]:
        return state

    try:
        proc = subprocess.run([python, "-c", _PROBE_SOURCE, *packages],
                              capture_output=True, text=True, timeout=timeout, cwd=root,
                              stdin=subprocess.DEVNULL, **_no_window_kwargs())
    except Exception as exc:
        # A copied or relocated environment fails HERE: the launcher exe exists, and
        # starting it is what fails ("uv trampoline failed to spawn Python child
        # process", WinError 2, ...). Keep the message verbatim; it is evidence.
        state["ran"] = False
        state["error"] = f"{type(exc).__name__}: {exc}"
        return state

    lines = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()]
    parsed = []
    for line in lines[:2]:
        try:
            parsed.append(json.loads(line))
        except Exception:
            break
    if not parsed:
        state["ran"] = False
        state["error"] = (f"exit code {proc.returncode}; "
                          f"{_tail(proc.stderr) or 'no output at all'}")
        return state
    state["ran"] = True
    state["executable"] = parsed[0].get("executable")
    state["runtime_version"] = parsed[0].get("version")
    state["base_prefix"] = parsed[0].get("base_prefix")
    if len(parsed) > 1 and isinstance(parsed[1], dict):
        state["modules"] = parsed[1]
    else:
        state["error"] = (f"the interpreter started but the module probe produced no "
                          f"answer; {_tail(proc.stderr) or 'no stderr'}")
    return state


def _probe_in_process(packages) -> dict:
    """The fast path's answer: this very interpreter, asked directly."""
    import importlib.util
    modules = {}
    for name in packages:
        try:
            spec = importlib.util.find_spec(name)
        except Exception:
            spec = None
        if spec is None:
            modules[name] = None
            continue
        try:
            from importlib.metadata import version as _version
            modules[name] = _version(name)
        except Exception:
            modules[name] = "installed"
    return {
        "ran": True,
        "in_process": True,
        "executable": sys.executable,
        "runtime_version": ".".join(str(p) for p in sys.version_info[:3]),
        "base_prefix": getattr(sys, "base_prefix", sys.prefix),
        "modules": modules,
    }


def _same_path(a, b) -> bool:
    try:
        return (os.path.normcase(os.path.abspath(a))
                == os.path.normcase(os.path.abspath(b)))
    except Exception:
        return False


def _tail(text, limit: int = 200) -> str:
    """The last `limit` characters of a captured stream, whitespace-collapsed."""
    text = " ".join((text or "").split())
    return text[-limit:] if len(text) > limit else text


def collect_server(probe: bool = True, env=None) -> dict:
    """Find the MCP server and read its /health: via mcp_port, never a private copy.

    `probe=False` makes this a pure no-op that touches NO socket (the MCP surface
    calls it that way: server_info is already talking to the server, so re-probing it
    over the network from inside itself would be both slow and circular).
    """
    state = {"probed": bool(probe), "pinned": None, "port": None, "health": None,
             "error": None, "holders": []}
    try:
        state["pinned"] = mcp_port.pinned_mcp_port(env)
    except Exception as exc:
        state["error"] = f"{type(exc).__name__}: {exc}"
        return state
    if not probe:
        return state
    try:
        if state["pinned"] is not None:
            health = mcp_port.browserplug_health(state["pinned"])
            if health is not None:
                state["port"] = state["pinned"]
                state["health"] = health
        else:
            port, health = mcp_port.seek_browserplug()
            state["port"], state["health"] = port, health
    except Exception as exc:
        # The seek is the one collector that talks to the network; a broken resolver
        # or a firewall oddity must produce "could not check", never a traceback.
        state["error"] = f"{type(exc).__name__}: {exc}"
    if state["health"] is None and not state["error"]:
        # Nothing answered /health. That is "no server" only if nothing holds the ports;
        # a wedged server holds them and never replies.
        state["holders"] = collect_port_holders(state["pinned"])
    return state


def _looks_like_ours(cmdline) -> bool:
    text = str(cmdline or "").lower()
    return any(word in text for word in ("server.py", "launcher.py", "browserplug"))


def _listeners(ports) -> dict:
    """{port: {pid, name, cmdline, parent_pid, parent_cmdline}} for TCP listeners on
    `ports`, via psutil when it imports. psutil is optional here on purpose: its absence
    is one of the things the doctor diagnoses, so any failure yields an empty map."""
    found: dict = {}
    try:
        # importlib, not an import statement: test_module_imports_nothing_that_could_be_missing
        # guards the module's top level, and this one sits inside a try by design.
        psutil = importlib.import_module("psutil")
        wanted = set(ports)
        for conn in psutil.net_connections(kind="tcp"):
            if conn.status != psutil.CONN_LISTEN or not conn.laddr:
                continue
            if conn.laddr.port not in wanted or conn.pid is None:
                continue
            row = {"pid": conn.pid, "name": None, "cmdline": "", "parent_pid": None,
                   "parent_cmdline": ""}
            try:
                proc = psutil.Process(conn.pid)
                row["name"] = proc.name()
                row["cmdline"] = " ".join(proc.cmdline())
                parent = proc.parent()
                if parent is not None:
                    row["parent_pid"] = parent.pid
                    row["parent_cmdline"] = " ".join(parent.cmdline())
            except Exception:
                pass
            found.setdefault(conn.laddr.port, row)
    except Exception:
        return {}
    return found


def collect_port_holders(pinned=None, ws_port=None) -> list:
    """Ports a BrowserPlug server would own that something holds, right now.

    Called only after /health went unanswered. The WebSocket port and a pinned MCP port
    count whoever holds them (a bind test, so a listener with no visible owner still
    shows). On the unpinned range only listeners whose command line looks like ours
    count, because an unrelated program on :2831 is not a wedged server. Never raises.
    """
    ws_port = WS_PORT if ws_port is None else ws_port
    try:
        strict = [("websocket", ws_port)]
        if pinned is not None:
            strict.append(("mcp", pinned))
        rng = [] if pinned is not None else list(mcp_port.mcp_port_sequence())
        owners = _listeners([p for _, p in strict] + rng)
        holders = []
        for kind, port in strict:
            row = owners.get(port)
            if row is None and not mcp_port.port_is_occupied(port):
                continue
            holders.append(dict(row or {"pid": None, "cmdline": "", "parent_pid": None,
                                        "parent_cmdline": ""}, kind=kind, port=port))
        for port in rng:
            row = owners.get(port)
            if row and _looks_like_ours(row["cmdline"]):
                holders.append(dict(row, kind="mcp", port=port))
        return holders
    except Exception:
        return []


def _wedged_detail(holders) -> str:
    pids = sorted({h["pid"] for h in holders if h.get("pid")})
    where = ", ".join(f"{h['kind']} :{h['port']}" for h in holders)
    who = (f"pid {', '.join(str(p) for p in pids)}" if pids
           else "owner unknown: psutil is missing or access was denied")
    return (f"a server process is running but not answering /health ({who}; holds {where}). "
            f"It is wedged, not absent: a second server would refuse to start on the "
            f"instance lock")


def _wedged_remedy(holders) -> str:
    """End the wedged process (and its uv trampoline parent) so a launcher can respawn."""
    target = None
    for h in holders:
        if h.get("pid"):
            parent_ours = h.get("parent_pid") and "server.py" in str(h.get("parent_cmdline"))
            target = h["parent_pid"] if parent_ours else h["pid"]
            break
    if target is None:
        return ("find the process holding the port (netstat -ano) and end it, then let an "
                "MCP client's launcher respawn the server; do not start a second one")
    kill = f"taskkill /PID {target} /T /F" if os.name == "nt" else f"kill -9 {target}"
    return (f"end the wedged server, which cannot answer /shutdown or /restart: {kill}. "
            f"Any MCP client's launcher then respawns it detached. Do not start "
            f"another while it lives")


def collect_client_configs(root: str | None = None, env=None) -> list:
    """Every MCP client registry we know the path of, parsed and path-checked.

    Each client keeps its own config (see the table in docs/mcp-clients.md), so "it works in
    Claude Code" says nothing about Kilo. Returns one record per file that EXISTS;
    absent files are not a finding, since nobody has to use every client. The files of
    _EXTRA_CLIENT_CONFIGS count only when they hold a BrowserPlug entry.
    """
    root = root or repo_root()
    env = os.environ if env is None else env
    appdata = env.get("APPDATA") or ""
    home = env.get("USERPROFILE") or env.get("HOME") or os.path.expanduser("~")
    try:
        cwd = os.getcwd()
    except OSError:
        cwd = ""
    bases = {"root": root, "appdata": appdata, "home": home, "cwd": cwd}

    records = []
    seen = set()
    for client, base_key, relative in _CLIENT_CONFIGS + _EXTRA_CLIENT_CONFIGS:
        base = bases.get(base_key)
        if not base:
            continue
        path = os.path.join(base, relative)
        if not os.path.isfile(path):
            continue
        real = os.path.normcase(os.path.realpath(path))
        if real in seen:  # cwd is often the root
            continue
        seen.add(real)
        extra = (client, base_key, relative) in _EXTRA_CLIENT_CONFIGS
        reader = _read_toml_config if path.endswith(".toml") else _read_client_config
        record = reader(client, path, root)
        if extra and not record.get("declares_browserplug"):
            continue
        records.append(record)
    return records


def _launcher_paths(root: str) -> set:
    return {os.path.normcase(os.path.join(root, *parts))
            for parts in (("server", "launcher.py"), ("launcher", "launcher.py"))}


def _is_browserplug_entry(name, paths: list, root: str | None) -> bool:
    """Its name or a path says browserplug, or it runs this checkout's launcher (Codex's
    `livebrowser` and Vibe's `legacy-browser` entries say neither)."""
    if "browserplug" in str(name).lower() or any("browserplug" in str(p).lower()
                                                 for p in paths):
        return True
    launchers = _launcher_paths(root) if root else set()
    return any(os.path.isabs(str(p)) and os.path.normcase(os.path.normpath(str(p))) in launchers
               for p in paths)


def _entry_record(name, key, project, entry: dict, root: str | None):
    """The record of one server entry when it is BrowserPlug's, else None."""
    paths = _entry_paths(entry)
    if not _is_browserplug_entry(name, paths, root):
        return None
    return {
        "name": name,
        "key": key,
        "project": project,
        "enabled": entry.get("enabled", True),
        "transport": (entry.get("type") or entry.get("transport")
                      or ("http" if entry.get("url") else "stdio")),
        "url": entry.get("url"),
        "paths": paths,
        # Only ABSOLUTE paths are checkable: "python" or "npx" resolve through
        # PATH at spawn time and their absence is not this file's fault.
        "missing_paths": [p for p in paths if os.path.isabs(p) and not os.path.exists(p)],
    }


def _new_record(client: str, path: str) -> dict:
    return {"client": client, "path": path, "parsed": False, "error": None,
            "parse_error": False, "entries": [], "declares_browserplug": False}


def _read_text(record: dict, path: str):
    """The file's text, or None with the reason in `record`."""
    try:
        size = os.path.getsize(path)
        if size > MAX_CONFIG_BYTES:
            # Deliberately NOT a parse_error: an oversized ~/.claude.json is this
            # module declining to read it, not the client's config being broken.
            record["error"] = (f"{size // (1024 * 1024)} MB — too large to parse safely "
                               f"(limit {MAX_CONFIG_BYTES // (1024 * 1024)} MB)")
            return None
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except Exception as exc:
        record["error"] = f"{type(exc).__name__}: {exc}"
        record["parse_error"] = True
        return None


def _read_toml_config(client: str, path: str, root: str | None = None) -> dict:
    """A TOML registry (Codex, Mistral Vibe, Reasonix): tomllib where it exists (3.11+), else
    a line reader that knows only the keys a server table uses. Neither ever fails: a file the
    client cannot parse either is not this check's to judge."""
    record = _new_record(client, path)
    text = _read_text(record, path)
    if text is None:
        return record
    record["parsed"] = True
    for name, entry, key in _toml_server_tables(text):
        found = _entry_record(name, key, None, entry, root)
        if found:
            record["declares_browserplug"] = True
            record["entries"].append(found)
    return record


def _toml_server_tables(text: str) -> list:
    """[(name, entry, key)] for every server table under _TOML_SERVER_KEYS."""
    try:
        import tomllib
        data = tomllib.loads(text)
    except Exception:           # no tomllib before 3.11, or a file tomllib refuses
        return _toml_tables_by_line(text)
    tables = []
    for key in _TOML_SERVER_KEYS:
        value = data.get(key)
        if isinstance(value, dict):
            tables += [(name, entry, key) for name, entry in value.items()
                       if isinstance(entry, dict)]
        elif isinstance(value, list):
            tables += [(entry.get("name"), entry, key) for entry in value
                       if isinstance(entry, dict)]
    return tables


_TOML_HEADER = re.compile(r"^\s*\[(\[?)\s*([A-Za-z0-9_.-]+)\s*\]\]?\s*(#.*)?$")
_TOML_KEY = re.compile(r"^\s*(name|command|args|type|transport|url|enabled)\s*=\s*(.*)$")
_TOML_STRING = re.compile(r'"((?:[^"\\]|\\.)*)"|\'([^\']*)\'')


def _toml_strings(fragment: str) -> list:
    """The string values in a TOML fragment: basic strings with their escapes undone, and
    literal ones as written (a Windows path in single quotes keeps its backslashes)."""
    return [re.sub(r"\\(.)", r"\1", basic) if quote_basic else literal
            for quote_basic, basic, literal in
            ((m.group(0).startswith('"'), m.group(1) or "", m.group(2) or "")
             for m in _TOML_STRING.finditer(fragment))]


def _toml_tables_by_line(text: str) -> list:
    tables, current, pending = [], None, None
    for line in text.splitlines():
        if pending is not None:
            pending.append(line)
            if "]" in line:
                current["args"] = _toml_strings("\n".join(pending))
                pending = None
            continue
        header = _TOML_HEADER.match(line)
        if header:
            parts = header.group(2).split(".")
            array = bool(header.group(1))
            current = None
            if parts[0] in _TOML_SERVER_KEYS and len(parts) == (1 if array else 2):
                current = {}
                tables.append((None if array else parts[1], current, parts[0]))
            continue
        found = _TOML_KEY.match(line) if current is not None else None
        if not found:
            continue
        key, value = found.groups()
        if key == "args":
            if "[" in value and "]" not in value:
                pending = [value]
            else:
                current["args"] = _toml_strings(value)
        elif key == "enabled":
            current["enabled"] = value.strip().lower().startswith("true")
        elif _toml_strings(value):
            current[key] = _toml_strings(value)[0]
    return [(name if name is not None else entry.get("name"), entry, key)
            for name, entry, key in tables]


def _read_client_config(client: str, path: str, root: str | None = None) -> dict:
    record = _new_record(client, path)
    text = _read_text(record, path)
    if text is None:
        return record
    try:
        data = json.loads(text)
    except Exception as exc:
        record["error"] = f"{type(exc).__name__}: {exc}"
        record["parse_error"] = True
        return record
    record["parsed"] = True
    if not isinstance(data, dict):
        record["error"] = "top-level JSON is not an object"
        record["parse_error"] = True
        return record

    # Claude Code also keeps project-scoped servers inside ~/.claude.json, under
    # projects.<path>.mcpServers: a registration made with `claude mcp add` in a project
    # lives only there.
    scopes = [(data, None)]
    if isinstance(data.get("projects"), dict):
        scopes.extend((body, name) for name, body in data["projects"].items()
                      if isinstance(body, dict))
    maps = [(key, container[key], project) for container, project in scopes
            for key in _SERVER_MAP_KEYS if isinstance(container.get(key), dict)]
    for key, servers, project in maps:
        for name, entry in servers.items():
            if not isinstance(entry, dict):
                continue
            found = _entry_record(name, key, project, entry, root)
            if found:
                record["declares_browserplug"] = True
                record["entries"].append(found)
    return record


def _entry_paths(entry: dict) -> list:
    """The filesystem arguments of a config entry, whichever spelling it uses.

    Kilo writes `command` as a LIST; Claude Code / VS Code write a `command` string
    plus `args`. Both shapes carry the same two absolute paths (the interpreter and
    launcher.py), which is exactly what a config written on another machine gets wrong.
    """
    out = []
    command = entry.get("command")
    if isinstance(command, str):
        out.append(command)
    elif isinstance(command, (list, tuple)):
        out.extend(str(part) for part in command)
    args = entry.get("args")
    if isinstance(args, (list, tuple)):
        out.extend(str(part) for part in args)
    return [p for p in out if p]


def _venv_lines(text: str, root: str, relative_ok: bool) -> list:
    """1-based numbers of the lines that name `<root>/.venv` (any slash, any case, JSON's
    doubled backslashes), or `.venv/` itself when the file lives in the checkout and so
    names it relative to its own directory."""
    needle = os.path.join(root, ".venv").replace("\\", "/").lower().rstrip("/")
    found = []
    for number, line in enumerate(text.splitlines(), 1):
        flat = line.replace("\\\\", "\\").replace("\\", "/").lower()
        if needle in flat or (relative_ok and ".venv/" in flat):
            found.append(number)
    return found


def collect_venv_references(root: str | None = None, env=None) -> list:
    """Client configs that still start BrowserPlug from the checkout's `.venv` python.

    Read as text, so the TOML configs (Codex, Vibe, Reasonix) are covered too. One record
    {client, path, lines} per file that names it; a file past MAX_CONFIG_BYTES or one that
    cannot be read is skipped (collect_client_configs already reports those). Never raises.
    """
    root = root or repo_root()
    env = os.environ if env is None else env
    home = env.get("USERPROFILE") or env.get("HOME") or os.path.expanduser("~")
    try:
        cwd = os.getcwd()
    except OSError:
        cwd = ""
    bases = {"root": root, "appdata": env.get("APPDATA") or "", "home": home, "cwd": cwd}
    found, seen = [], set()
    for client, base_key, relative in _VENV_SCAN_CONFIGS:
        base = bases.get(base_key)
        if not base:
            continue
        path = os.path.join(base, relative)
        try:
            if not os.path.isfile(path) or os.path.getsize(path) > MAX_CONFIG_BYTES:
                continue
            real = os.path.normcase(os.path.realpath(path))
            if real in seen:
                continue
            seen.add(real)
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        lines = _venv_lines(text, root, relative_ok=_inside(path, root))
        if lines:
            found.append({"client": client, "path": path, "lines": lines})
    return found


# ---------------------------------------------------------------------------
# Checks: pure functions over the collected state. No I/O, no clock, no globals:
# every diagnosed scenario is reachable by handing these a dict.
# ---------------------------------------------------------------------------

_MODE_NOTES = {
    "in_process": " — this is the interpreter running the check",
    "override": f" (named by {server_env.SERVER_PYTHON_ENV})",
}


# inspect_server_env's env_state values that the next launcher spawn repairs by itself,
# with what it does about each.
_SPAWN_FIXES = {
    "not_built": ("the next start (a launcher spawn, or python server/control.py start) builds "
                  "it with `uv sync --active`; a first build on an empty uv cache downloads "
                  "every package and can take tens of seconds"),
    "out_of_date": ("the next start (a launcher spawn, or python server/control.py start) "
                    "updates it with `uv sync --active` once no server runs from it; a server "
                    "already running keeps the packages it started with"),
    "replace": ("the next start (a launcher spawn, or python server/control.py start) "
                "recreates it with `uv sync --active`, but only once no server runs from it"),
}


def check_interpreter(state: dict) -> dict:
    """Can the Python a launcher would start the server with actually start?

    The observed failures: an environment whose base interpreter no longer exists here
    (built on another machine, or its uv Python uninstalled), and one a `uv cache prune`
    gutted under a running server. `pyvenv.cfg` records the base interpreter, so when the
    spawn fails that line is the answer, quoted verbatim, because the path IS the evidence.
    """
    root = state.get("root") or repo_root()
    python = state.get("python") or "the server environment's python"
    home, home_exists = state.get("cfg_home"), state.get("cfg_home_exists")

    if state.get("problem") and state.get("env_state") in _SPAWN_FIXES:
        # Not a failure: these are the states the next start's `uv sync --active` fixes by
        # itself. Informational rather than a pass, because a cold first build can strain
        # the first client's connect budget.
        return _check("interpreter", None,
                      f"{state['problem']} — {_SPAWN_FIXES[state['env_state']]}",
                      state.get("remedy"))

    if state.get("problem"):
        detail = state["problem"]
        if state.get("uv_output") and state["uv_output"] not in detail:
            detail += f" (uv said: {state['uv_output']})"
        return _check("interpreter", False,
                      f"{detail} — until this is fixed a launcher cannot start the server, "
                      f"so every MCP client times out on connect",
                      state.get("remedy") or _rebuild_env(root, state))

    if not state.get("python_exists"):
        return _check("interpreter", False,
                      f"no interpreter at {python} — nothing here can start the server",
                      state.get("remedy") or _rebuild_env(root, state))

    if state.get("damaged"):
        return _check("interpreter", False, state["damaged"], _REPAIR_RUNNING_ENV)

    if state.get("ran") is False or state.get("ran") is None:
        why = state.get("error") or "the probe was never run"
        detail = f"{python} exists but could not run it: {why}"
        if home and home_exists is False:
            detail += (f". pyvenv.cfg home = {home} does not exist on this machine "
                       f"(the environment's base interpreter is gone: it was built elsewhere, "
                       f"or that Python was uninstalled)")
        return _check("interpreter", False, detail, _rebuild_env(root, state))

    version = state.get("runtime_version") or "unknown version"
    base = state.get("base_prefix") or "unknown base"
    detail = f"Python {version} at {python} (base {base})"
    detail += _MODE_NOTES.get(state.get("mode"), "")
    if home and home_exists is False:
        # It RUNS, so this is not a failure: a uv trampoline's own metadata can name an
        # interpreter it does not use. Say so rather than raising a false alarm, but say
        # it, because it is what makes the environment unportable.
        detail += (f". NB pyvenv.cfg home = {home} does not exist here; the environment's "
                   f"metadata does not describe the interpreter it actually runs, so "
                   f"copying it to another machine will not work")
    return _check("interpreter", True, detail)


def check_dependencies(state: dict) -> dict:
    """Are the packages server.py imports first actually installed in that environment?

    `ModuleNotFoundError: websockets` is the observed failure, and it is invisible
    from the outside: the server dies before it binds a port, so every client just
    reports a connect timeout against a server that never existed.
    """
    root = state.get("root") or repo_root()
    modules = state.get("modules")
    if state.get("env_state") in _SPAWN_FIXES and not isinstance(modules, dict):
        why = state.get("problem") or "the server environment is not ready"
        return _check("dependencies", None,
                      f"not checked: {why}; the start that syncs it installs every declared "
                      f"package")
    if not isinstance(modules, dict) or "__probe_error__" in modules:
        why = (modules.get("__probe_error__") if isinstance(modules, dict)
               else state.get("error") or state.get("problem")
               or "the interpreter could not be run")
        return _check("dependencies", None,
                      f"could not check: {why} — fix the `interpreter` item above first, "
                      f"then re-run; nothing can be said about installed packages until "
                      f"some interpreter answers",
                      state.get("remedy") or _rebuild_env(root, state))

    missing = [name for name in REQUIRED_PACKAGES if not modules.get(name)]
    if missing:
        first = missing[0]
        where = state.get("env_dir") or state.get("python") or "the server environment"
        return _check("dependencies", False,
                      f"{', '.join(missing)} not installed in {where} — server.py dies with "
                      f"ModuleNotFoundError: {first} BEFORE it binds a port, so every "
                      f"client sees a connect timeout rather than this error",
                      _rebuild_env(root, state))

    installed = ", ".join(f"{name} {modules.get(name)}" for name in REQUIRED_PACKAGES)
    return _check("dependencies", True, f"all present: {installed}")


def check_mcp_port(state: dict) -> dict:
    """Did anything identifying itself as BrowserPlug answer on the MCP HTTP port?"""
    start, last = mcp_port.MCP_PORT_DEFAULT, mcp_port.MCP_PORT_LAST
    pinned, port, health = state.get("pinned"), state.get("port"), state.get("health")
    start_server = _START_SERVER

    if not state.get("probed"):
        pin_text = (f"{mcp_port.MCP_PORT_ENV}={pinned}" if pinned is not None
                    else f"unpinned (policy range :{start}-:{last})")
        return _check("mcp_port", None,
                      f"not checked: no network probe was requested — {pin_text}. Run "
                      f"`python server/preflight.py` for the probing version")
    if state.get("error"):
        return _check("mcp_port", None,
                      f"could not check: the port probe failed with {state['error']}",
                      start_server)

    holders = state.get("holders") or []
    if health is None and holders:
        return _check("mcp_port", False, _wedged_detail(holders), _wedged_remedy(holders))

    if pinned is not None:
        if health is not None:
            return _check("mcp_port", True,
                          f"MCP server found on :{port} (pinned by "
                          f"{mcp_port.MCP_PORT_ENV}={pinned})")
        return _check("mcp_port", False,
                      f"{mcp_port.MCP_PORT_ENV} pins :{pinned} and nothing answered "
                      f"there. A pin disables the seek entirely, so a server sitting on "
                      f"the policy range :{start}-:{last} would NOT be found either — "
                      f"this is the failure mode where /health answers happily on an "
                      f"off-policy port while every client times out",
                      f"unset {mcp_port.MCP_PORT_ENV} and let shared-py/browserplug_shared/mcp_port.py own the "
                      f"policy, or start the server on :{pinned}")

    if health is not None:
        return _check("mcp_port", True, f"MCP server found on :{port}")
    return _check("mcp_port", False,
                  f"nothing answered on :{start}-:{last} and {mcp_port.MCP_PORT_ENV} is "
                  f"unset — no BrowserPlug server is running on this machine",
                  start_server)


def check_server_process(state: dict) -> dict:
    """Who is that server: pid, build, uptime, plus the version-skew sentence."""
    if not state.get("probed"):
        return _check("server_process", None,
                      "not checked: no network probe was requested")
    health = state.get("health")
    holders = state.get("holders") or []
    if not isinstance(health, dict) and holders:
        return _check("server_process", False, _wedged_detail(holders),
                      _wedged_remedy(holders))
    if not isinstance(health, dict):
        return _check("server_process", False,
                      "no server process answered — there is nothing to report about "
                      "the server because there is no server (see the mcp_port item)",
                      _START_SERVER)

    pid = health.get("server_pid")
    version = health.get("server_version") or "unknown"
    uptime = _format_age(health.get("uptime_seconds") or 0)
    ws_port = health.get("ws_port")
    detail = (f"pid {pid}, version {version}, up {uptime}, WebSocket on :{ws_port}")
    warning = health.get("version_warning")
    if warning:
        # Verbatim: version_check.py already phrases the action, and re-wording a
        # sentence whose exact text is pinned by a unit test would just make two.
        detail += f". Version mismatch: {warning}"
    return _check("server_process", True, detail)


def check_extension(state: dict) -> dict:
    """Is a browser extension connected, and if not, WHICH not-connected is it?

    "Never dialed" and "dialed, then the socket died" have different fixes, and the
    second one is the scenario the old error string could not express: with the
    server alive and the WS severed, every page tool answered "The socket connection
    was closed unexpectedly", which reads like a transport bug rather than a browser
    that needs reloading.
    """
    if not state.get("probed"):
        return _check("extension", None, "not checked: no network probe was requested")
    health = state.get("health")
    if not isinstance(health, dict):
        return _check("extension", None,
                      "could not check: no server answered, so nothing can report "
                      "whether a browser is connected (fix the server first — an "
                      "extension with no server to dial is not a browser problem)")

    browsers = _rows(health.get("browsers"))
    if browsers or health.get("extension_connected"):
        roster = ", ".join(
            f"[{b.get('slot')}] {b.get('name') or b.get('edition') or 'browser'}"
            f" (extension {b.get('version') or '?'})" for b in browsers) or "1 browser"
        return _check("extension", True,
                      f"{max(len(browsers), 1)} connected: {roster}")

    mcp = state.get("port") or health.get("mcp_port")
    ws = health.get("ws_port")
    seen = _rows(health.get("extension_instances"))
    if seen:
        was = ", ".join(
            f"[{i.get('slot')}] {i.get('friendly') or i.get('name') or 'browser'} last "
            f"connected {i.get('last_connect_hms') or 'unknown'}" for i in seen)
        return _check("extension", False,
                      f"A browser extension is REGISTERED but its WebSocket is CLOSED: "
                      f"{was}. The server is up on :{mcp} and still listening on :{ws} — "
                      f"the socket dropped and nothing has re-dialed it. This is not a "
                      f"transport bug and not a timeout: no browser is reachable right "
                      f"now, so every page tool will fail until one reconnects",
                      _RELOAD_EXTENSION)
    return _check("extension", False,
                  f"No browser extension is connected. The server is up on :{mcp} and "
                  f"the WebSocket is listening on :{ws} — nothing has dialed it.",
                  _CONNECT_EXTENSION)


def check_heartbeat(state: dict, now: float | None = None) -> dict:
    """Has the connected extension said anything recently?

    Source is the `connections[]` extension row's `activity_epoch` paired with the
    payload's own `generated_epoch`: both stamps are SERVER clock, so the age never
    mixes in the reader's clock. It is deliberately NOT
    `extension_instances[].last_activity_epoch`; that one excludes heartbeats by
    design, and would report a healthy idle browser as silent for hours.
    """
    if not state.get("probed"):
        return _check("heartbeat", None, "not checked: no network probe was requested")
    health = state.get("health")
    if not isinstance(health, dict):
        return _check("heartbeat", None,
                      "could not check: no server answered, so there is no activity "
                      "stamp to read")
    if not (health.get("browsers") or health.get("extension_connected")):
        return _check("heartbeat", None,
                      "not checked: no extension is connected, so there is no heartbeat "
                      "to be late (see the extension item)")

    age = _extension_activity_age(health, now)
    if age is None:
        return _check("heartbeat", None,
                      "could not check: /health carried no activity stamp for the "
                      "connected extension (an extension too old to report one)")
    if age > HEARTBEAT_STALE_SECONDS:
        return _check("heartbeat", False,
                      f"extension registered but silent for {_format_age(age)} — the "
                      f"socket may be half-open. It health-probes every 15s, so anything "
                      f"past {HEARTBEAT_STALE_SECONDS}s means the messages stopped, not "
                      f"that the browser is idle",
                      _RELOAD_EXTENSION)
    return _check("heartbeat", True,
                  f"last message {_format_age(age)} ago (server clock; the extension "
                  f"probes every 15s)")


def _extension_activity_age(health: dict, now: float | None = None):
    """Seconds since the extension last said anything, or None when unknowable."""
    rows = [c for c in _rows(health.get("connections")) if c.get("kind") == "extension"]
    stamps = [c.get("activity_epoch") for c in rows
              if isinstance(c.get("activity_epoch"), (int, float))]
    if not stamps:
        return None
    # generated_epoch is the server's clock at the moment it built this payload;
    # preferring it keeps the whole subtraction inside one clock. Falling back to our
    # own `now` is only right because /health was fetched a moment ago.
    reference = health.get("generated_epoch")
    if not isinstance(reference, (int, float)):
        reference = time.time() if now is None else now
    return max(0.0, float(reference) - max(stamps))


def _relocated_checkout(missing_paths: list, root: str) -> str | None:
    """The checkout a config was written for, when THAT is what is wrong with it.

    `kilo.json` ships in this repo carrying the author's absolute paths, so every
    checkout anywhere else fails the path check on every `server_info` with a message
    about a config written on another box. It was not; it is this repo's own file, moved.
    Returns the old root when every missing path resolves once its leading directory is
    replaced with `root`, else None, which keeps the general message for the general case.
    """
    missing_paths = [p for p in (missing_paths or []) if isinstance(p, str)]
    if len(missing_paths) < 2 or not root:
        # One path proves nothing: its basename landing under this root is a coincidence
        # away, and a coincidence here renames a real failure.
        return None
    try:
        old_root = os.path.commonpath([os.path.dirname(p) for p in missing_paths])
        if not old_root or os.path.normcase(old_root) == os.path.normcase(root):
            return None
        for path in missing_paths:
            if not os.path.exists(os.path.join(root, os.path.relpath(path, old_root))):
                return None
    except (ValueError, OSError):
        return None          # different drives, or a path the OS will not measure
    return old_root


def _inside(path: str, root: str) -> bool:
    """True when `path` sits under `root` — i.e. it is a file this repo ships."""
    try:
        return os.path.normcase(os.path.commonpath([path, root])) == os.path.normcase(root)
    except (ValueError, OSError, TypeError):
        return False


# The `type` values that mean "a process the client starts and talks to over stdio" (Kilo
# writes `local`).
_STDIO_TRANSPORTS = ("stdio", "local")

# What a `uv run --script` registration costs per open client, measured on Windows 11
# (.tmp/ research, 2026-10-01): uv.exe stays resident for the session, beside the script
# environment's trampoline.
_UV_RUN_COST = ("each open client keeps a uv.exe resident (about 20 MB RSS and 40 MB commit) "
                "plus a trampoline")


def _registration(root: str, uv: str | None, launcher=None) -> str:
    """How a client registers the launcher.

    `launcher` is server_env.launcher_python's (python, why). With a python, uv's
    minor-version link interpreter runs the launcher directly. Without one, uv by its
    absolute path (server_env.find_uv), plus `why`, which says how to get the interpreter.
    A bare `uv` is resolved by the client, and a Windows client can resolve it to a uv.exe
    in the project directory it was opened in."""
    path = os.path.join(root, "server", "launcher.py")
    python, why = launcher if launcher else (None, None)
    if python:
        return f"`{python} {path}` (command `{python}`, args: the launcher path)"
    command = uv or "uv"
    text = (f"`{command} run --script {path}` (command `{command}`, args `run`, "
            f"`--script`, the launcher path)")
    if not uv:
        text += ("; once uv is installed, name it by its full path rather than a bare `uv`, "
                 "which a Windows client may resolve to a uv.exe in the folder it was "
                 "opened in")
    elif why:
        text += (f"; that form keeps a uv.exe resident per client, and uv's own Python runs "
                 f"the launcher with less ({why}, then register that interpreter with the "
                 f"launcher path as its argument)")
    # Without a uv interpreter to name, the README's venv route is the way that needs no uv.
    text += (f"; without uv, register the venv's python from the README's step 1 instead "
             f"(command `<venv python>`, args: the launcher path, env "
             f"`{server_env.SERVER_PYTHON_ENV}=<venv python>`)")
    return text


def _launch_form(paths, minors=None) -> str:
    """How a registration starts the launcher: "interpreter" (a python running launcher.py),
    "interpreter_patch" (one from a uv patch directory, which `uv python upgrade` does not
    move), "interpreter_range" (uv's minor-version link for a minor outside `minors`, the
    server's requires-python range, when that is known), "uv_run", or "other"."""
    paths = [str(p) for p in (paths or [])]
    if not paths or not any(p.replace("\\", "/").lower().endswith(
            ("server/launcher.py", "launcher/launcher.py")) for p in paths[1:]):
        return "other"
    exe = os.path.basename(paths[0].replace("\\", "/")).lower()
    if exe in ("uv", "uv.exe") and "run" in (p.lower() for p in paths[1:]):
        return "uv_run"
    if re.fullmatch(r"pythonw?[0-9.]*(\.exe)?", exe):
        kind, version = server_env.uv_python_dir(paths[0])
        if kind == "patch":
            return "interpreter_patch"
        if kind == "link" and minors and version not in minors:
            return "interpreter_range"
        return "interpreter"
    return "other"


def _missing_remedy(record: dict, missing: list, minors=None):
    """The remedy for a missing interpreter of uv's, or None for any other missing path.
    `minors` is the server's requires-python range; a minor outside it is never
    recommended, the highest inside it is named instead."""
    for path in missing:
        kind, version = server_env.uv_python_dir(path)
        if kind == "link":
            if minors and version not in minors:
                return (f"{record['path']} names uv's Python {version} minor-version link, "
                        f"which is outside the server's Python range ({minors[0]} to "
                        f"{minors[-1]}) and is gone. Run `uv python install {minors[-1]}`, "
                        f"name that minor-version link (`uv python find --managed-python "
                        f"{minors[-1]}` prints it) in {record['path']}, then restart that "
                        f"client")
            return (f"run `uv python install {version}`: it recreates uv's Python {version} "
                    f"minor-version link that {record['path']} names. Then restart that "
                    f"client")
        if kind == "patch":
            minor = ".".join(version.split(".")[:2])
            if minors and minor not in minors:
                minor = minors[-1]
            return (f"uv's Python {version} was removed or upgraded away. In {record['path']}, "
                    f"name uv's minor-version link instead (`uv python find --managed-python "
                    f"{minor}` prints it; `uv python install {minor}` creates it), then "
                    f"restart that client")
    return None


def _registration_notes(declaring: list, root: str, uv: str | None, launcher) -> tuple:
    """(detail suffix, remedy or None) for working registrations that could be better: the
    `uv run --script` form and a patch-pinned interpreter."""
    minors = server_env.server_python_minors(root)
    forms = [(r, e, _launch_form(e.get("paths"), minors)) for r in declaring
             for e in r.get("entries") or [] if e.get("transport") in _STDIO_TRANSPORTS]
    uv_run = [f"{r['client']} {r['path']} ({e['name']})" for r, e, f in forms if f == "uv_run"]
    pinned = [f"{r['client']} {r['path']} ({e['name']})" for r, e, f in forms
              if f == "interpreter_patch"]
    out_of_range = [f"{r['client']} {r['path']} ({e['name']})" for r, e, f in forms
                    if f == "interpreter_range"]
    notes, remedy = "", None
    if uv_run:
        notes += (f". {len(uv_run)} of them start the launcher with `uv run --script` "
                  f"({'; '.join(uv_run)}): {_UV_RUN_COST}, where the interpreter form runs "
                  f"two processes and no uv")
        python, why = launcher if launcher else (None, None)
        remedy = (f"register {_registration(root, uv, launcher)} instead, then restart each "
                  f"client; the `uv run` form keeps working until then" if python else
                  f"{why or 'run `uv python install` for the server Python'}, then register "
                  f"that interpreter with {os.path.join(root, 'server', 'launcher.py')} as its "
                  f"argument and restart each client")
    if pinned:
        notes += (f". {'; '.join(pinned)} name a uv patch directory, which `uv python "
                  f"upgrade` does not move: name uv's minor-version link instead")
    if out_of_range:
        python, why = launcher if launcher else (None, None)
        notes += (f". {'; '.join(out_of_range)} name a uv Python outside the server's "
                  f"requires-python range ({minors[0]} to {minors[-1]})")
        remedy = remedy or (
            f"register {_registration(root, uv, launcher)} instead, then restart each client"
            if python else
            f"{why or f'run `uv python install {minors[-1]}`'}, then register that "
            f"interpreter with {os.path.join(root, 'server', 'launcher.py')} as its "
            f"argument and restart each client")
    return notes, remedy


def _venv_reference_check(refs: list, root: str, uv: str | None = None,
                          launcher=None) -> dict:
    """The `.venv` configs: a FAIL once the directory is gone (the client then fails to
    spawn), a passing item that still says what to change while it exists."""
    names = "; ".join(f"{r['client']} {r['path']} (line {', '.join(map(str, r['lines']))})"
                      for r in refs)
    remedy = (f"change each of those entries to start {_registration(root, uv, launcher)}, "
              f"then restart that client")
    if os.path.isdir(os.path.join(root, ".venv")):
        return _check("client_config", True,
                      f"{len(refs)} client config(s) still start BrowserPlug from "
                      f"{os.path.join(root, '.venv')}: {names}. That works only while the "
                      f"checkout's .venv exists; once it is deleted those clients fail to "
                      f"spawn the launcher", remedy)
    return _check("client_config", False,
                  f"{len(refs)} client config(s) start BrowserPlug from "
                  f"{os.path.join(root, '.venv')}, which does not exist: {names}. The client "
                  f"reports this as a spawn failure naming the command, not the path",
                  remedy)


def check_client_config(records: list, state: dict | None = None,
                        root: str | None = None, venv_refs: list | None = None,
                        uv: str | None = None, launcher=None) -> dict:
    """Do the MCP client registries on this machine point at something that exists?

    Also answers the one condition that looks identical to a broken server from the
    client's side: a config that is written CORRECTLY but has not been LOADED. An
    edit to any of these files is not picked up live, so a client started before the
    edit reports "server not found" forever while everything it names is fine.

    `venv_refs` (collect_venv_references) are the configs still naming the checkout's
    .venv; they are reported after every outright failure below. `uv` (server_env.find_uv)
    and `launcher` (server_env.launcher_python's (python, why)) are what every
    registration text names. A working `uv run --script` registration passes with its cost
    and the replacement; a missing interpreter of uv's names `uv python install`.
    """
    records = [r for r in (records or []) if isinstance(r, dict)]
    state = state if isinstance(state, dict) else {}
    root = root or repo_root()
    venv_refs = [r for r in (venv_refs or []) if isinstance(r, dict) and r.get("lines")]

    if not records and venv_refs:
        return _venv_reference_check(venv_refs, root, uv, launcher)
    if not records:
        return _check("client_config", None,
                      "no MCP client config found at any known path (kilo.json, "
                      "%APPDATA%\\Code\\User\\mcp.json, %APPDATA%\\GitLab\\duo\\mcp.json, "
                      "~/.claude.json) — this client may keep its registry elsewhere; "
                      "each MCP client has its OWN, and adding BrowserPlug to one does not "
                      "register it with the others")

    if venv_refs and not os.path.isdir(os.path.join(root, ".venv")):
        # Named ahead of the generic missing-path failure below: same fault, but this one
        # knows the replacement command.
        return _venv_reference_check(venv_refs, root, uv, launcher)
    broken = [(r, e) for r in records for e in r.get("entries") or []
              if e.get("missing_paths")]
    if broken:
        record, entry = broken[0]
        missing = ", ".join(entry["missing_paths"])
        old_root = (_relocated_checkout(entry["missing_paths"], root)
                    if _inside(record["path"], root) else None)
        if old_root:
            # This file is the repo's own, and the repo has moved. Say that, rather than
            # "written on another box": it was written on this one, at another path, and
            # the fix is a prefix, not a rebuild. Still a FAIL, because for anyone who
            # DOES run that client it is genuinely broken — preflight has no tier
            # between "wrong" and "could not check", and inventing one here would put a
            # real spawn failure behind a shrug.
            return _check("client_config", False,
                          f"{record['client']} config at {record['path']} was written "
                          f"for a checkout at {old_root}; this one is at {root}, so the "
                          f"absolute paths it names ({missing}) do not exist here. It is "
                          f"this repo's own sample file, moved with the repo — harmless "
                          f"unless you actually run {record['client']}, which reports it "
                          f"as a spawn failure naming the command rather than the path",
                          f"in {record['path']}, replace the {old_root} prefix with "
                          f"{root} in both paths, then restart that client (config edits "
                          f"are not picked up live)")
        return _check("client_config", False,
                      f"{record['client']} config at {record['path']} points at "
                      f"{missing}, which does not exist on this machine — the config was "
                      f"written on another box, or the interpreter or virtualenv it names "
                      f"was removed. The client reports this as a spawn failure (on Windows, "
                      f"\"spawn <cmd> ENOENT\"), which names the command rather than the "
                      f"missing path",
                      _missing_remedy(record, entry["missing_paths"],
                                      server_env.server_python_minors(root) if root else [])
                      or f"edit {record['path']} so every absolute path resolves here, then "
                         f"RESTART that client (config edits are not picked up live)")

    disabled = [(r, e) for r in records for e in r.get("entries") or []
                if e.get("enabled") is False]
    if disabled:
        record, entry = disabled[0]
        return _check("client_config", False,
                      f"{record['client']} config at {record['path']} declares "
                      f"\"{entry['name']}\" with enabled=false — the client parses it and "
                      f"then skips it, so the server never appears and nothing logs an "
                      f"error",
                      f"set \"enabled\": true in {record['path']}, then restart that client")

    # Reported even when ANOTHER client's config is fine: the registries are separate,
    # so "it works in Claude Code" says nothing about the client whose file is broken,
    # and a broken one is exactly the config nobody can see is broken.
    unreadable = [r for r in records if r.get("parse_error")]
    if unreadable:
        record = unreadable[0]
        return _check("client_config", False,
                      f"{record['client']} config at {record['path']} does not parse: "
                      f"{record['error']}. A client SILENTLY ignores a config it cannot "
                      f"read, so the BrowserPlug server simply never appears in its tool list "
                      f"and nothing anywhere logs a reason",
                      f"fix the JSON in {record['path']} — a trailing comma is the usual "
                      f"cause, and 'Expecting value: line 1 column 1' means a "
                      f"byte-order mark from a Notepad/PowerShell save; then restart "
                      f"that client")

    declaring = [r for r in records if r.get("declares_browserplug")]
    if venv_refs:
        return _venv_reference_check(venv_refs, root, uv, launcher)
    if not declaring:
        paths = ", ".join(r["path"] for r in records)
        detail = (f"found {len(records)} client config(s) ({paths}) and NONE of them "
                  f"declares a browserplug server — the client you are using is not "
                  f"registered with BrowserPlug at all")
        return _check("client_config", False, detail,
                      f"add a browserplug entry that starts "
                      f"{_registration(root, uv, launcher)} over stdio (see the client "
                      f"registry table in docs/mcp-clients.md), then restart the client")

    summary = ", ".join(f"{r['client']} ({r['path']})" for r in declaring)
    notes, better = _registration_notes(declaring, root, uv, launcher)
    # Config valid + server reachable + NOT ONE client session registered is the
    # signature of "written but never loaded". If any client is attached, the file is
    # demonstrably being read and this would be noise.
    if state.get("probed") and isinstance(state.get("health"), dict):
        health = state["health"]
        sessions = [c for c in _rows(health.get("connections"))
                    if c.get("kind") == "mcp_client"]
        if not sessions and not health.get("client_pids"):
            path = declaring[0]["path"]
            return _check("client_config", True,
                          f"config at {path} looks correct — if this client still says "
                          f'"server not found", it has not reloaded the config: restart '
                          f"the client (an edit is not picked up live). The server itself "
                          f"is up and answering; it just has no MCP client attached{notes}",
                          "restart the MCP client (reopen Claude Code / reload the VS "
                          "Code window / restart the Kilo CLI) — nothing on the server "
                          "side needs changing")
    return _check("client_config", True,
                  f"declared by {summary}; every absolute path in them exists here{notes}",
                  better)


def _wants_registration(configs: list, venv_refs: list, root: str | None = None) -> bool:
    """Whether check_client_config will print a registration: only then is the launcher's
    interpreter looked up."""
    if venv_refs or not any(r.get("declares_browserplug") for r in configs):
        return True
    minors = server_env.server_python_minors(root) if root else []
    return any(e.get("missing_paths")
               or _launch_form(e.get("paths"), minors) in ("uv_run", "interpreter_patch",
                                                           "interpreter_range")
               for r in configs for e in r.get("entries") or [])


# ---------------------------------------------------------------------------
# Orchestration and rendering
# ---------------------------------------------------------------------------

def run_checks(root: str | None = None, *, probe_server: bool = True,
               health: dict | None = None, port: int | None = None,
               now: float | None = None, env=None) -> list:
    """Every precondition, in causal order. Never raises.

    probe_server=False makes this touch NO socket. `health` goes further: hand it a
    /health payload and the server-side items are answered from THAT, with no probe at
    all, which is how the in-process surfaces run. A server asking itself over its own
    HTTP port whether it is up is both slow and circular, and it is precisely the
    circularity that made the old diagnostic fall silent exactly when it was needed.
    """
    root = root or repo_root()
    interpreter = collect_interpreter(root, environ=env)
    if health is not None:
        server = {"probed": True, "pinned": mcp_port.pinned_mcp_port(env),
                  "port": port or health.get("mcp_port"), "health": health,
                  "error": None}
    else:
        server = collect_server(probe=probe_server, env=env)
    configs = collect_client_configs(root, env=env)
    venv_refs = collect_venv_references(root, env=env)
    uv = server_env.find_uv(env)
    # In-process (server_info, /doctor) it is read from the server environment, never asked
    # of uv: that path starts no process.
    launcher = (server_env.launcher_python(root, uv=uv, environ=env,
                                           ask_uv=not interpreter.get("in_process"))
                if _wants_registration(configs, venv_refs, root) else None)
    return [
        check_interpreter(interpreter),
        check_dependencies(interpreter),
        check_mcp_port(server),
        check_server_process(server),
        check_extension(server),
        check_heartbeat(server, now=now),
        check_client_config(configs, server, root, venv_refs, uv=uv, launcher=launcher),
    ]


_STATUS = {True: "ok  ", False: "FAIL", None: "??  "}


def render(checks: list, root: str | None = None) -> str:
    """The compact human/agent rendering: a verdict line, then one line per check.

    Deliberately flat text. The reader is usually an agent that has just been told
    "the socket connection was closed unexpectedly" and needs the next action, not a
    structure to walk.
    """
    checks = [c for c in (checks or []) if isinstance(c, dict)]
    failed = [c for c in checks if c.get("ok") is False]
    unknown = [c for c in checks if c.get("ok") is None]
    where = f" [{root}]" if root else ""

    if not checks:
        return "BrowserPlug preflight: no checks ran."
    if not failed and not unknown:
        head = f"BrowserPlug preflight: all {len(checks)} checks passed.{where}"
    else:
        head = (f"BrowserPlug preflight: {len(checks) - len(failed) - len(unknown)} ok, "
                f"{len(failed)} FAILED, {len(unknown)} not checked.{where}")
        if failed:
            head += (f" Fix the FIRST failure listed ({failed[0]['name']}) — the later "
                     f"items usually describe its consequences.")

    width = max(len(c.get("name") or "") for c in checks)
    lines = [head]
    for c in checks:
        lines.append(f"  {_STATUS.get(c.get('ok'), '??  ')} "
                     f"{(c.get('name') or '').ljust(width)}  {c.get('detail') or ''}")
        if c.get("remedy"):
            lines.append(f"       {' ' * width}  -> {c['remedy']}")
    return "\n".join(lines)


def verdict(checks: list) -> str:
    """One line: the headline of `render`, for callers that only want the summary."""
    return render(checks).splitlines()[0]


# ---------------------------------------------------------------------------
# HTTP: GET /doctor, beside /health. server.py's only involvement is one dispatch
# line, exactly the wan_diagnosis.py shape.
# ---------------------------------------------------------------------------

# Loopback-only by its own peer check, like /ws-token (and unlike /health, which any
# peer the http_auth gate admits may read): this payload names local file paths, config
# locations and pids, which is new information rather than a re-run of what /health
# already says.

def _summarize_connections(health):
    """The compact roster is owned by reports.py; imported on use because only the
    in-server surface needs it and the CLI path must stay stdlib-only."""
    from server.reporting import reports
    return reports.summarize_connections(health)


async def handle_doctor_request(scope, send, server) -> None:
    """The full ASGI leaf handler for GET /doctor: server.py just dispatches here.

    Answers when the WebSocket to the browser is dead, because it never touches it:
    the server-side items are read out of the server's own `get_health_info()`, so the
    endpoint is exactly as available as /health is.
    """
    host = (scope.get("client") or ("", 0))[0]
    if not http_auth.is_local_peer(host):
        await send({"type": "http.response.start", "status": 403,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body",
                    "body": json.dumps({"error": "localhost only"}).encode()})
        return

    try:
        # Both the health report and the checks read the process table, the disk and the
        # network, so they run on a worker thread: this handler is on the loop every MCP
        # client shares.
        health = await asyncio.to_thread(server.get_health_info)
    except Exception as exc:
        # A /health that cannot be built is itself a finding, not a 500: the caller is
        # asking what is wrong, so answering "I could not read my own state" is the
        # useful reply.
        health = None
        checks = [_check("server_process", None,
                         f"could not check: this server failed to describe itself "
                         f"({type(exc).__name__}: {exc})",
                         "read the server log (BROWSERPLUG_LOG_FILE) — the server is running "
                         "but its own health payload raised")]
    else:
        checks = await asyncio.to_thread(
            run_checks, health=health, port=getattr(server, "mcp_port", None))

    payload = json.dumps({
        "service": mcp_port.HEALTH_SERVICE,
        "ok": not any(c.get("ok") is False for c in checks),
        "checks": checks,
        "summary": render(checks),
        "connections": _summarize_connections(health) if health else None,
    }).encode("utf-8")
    await send({"type": "http.response.start", "status": 200,
                "headers": [(b"content-type", b"application/json"),
                            (b"cache-control", b"no-store")]})
    await send({"type": "http.response.body", "body": payload})


# ---------------------------------------------------------------------------
# CLI: the surface that still answers when the server process is gone.
# ---------------------------------------------------------------------------

_USAGE = """usage: python server/preflight.py [--json] [--no-probe] [--root DIR]
       uv run --script server/preflight.py [...]

Answers "is BrowserPlug installed and working?" — every precondition as its own pass/fail
item naming its own remedy. Stdlib only, so it runs under ANY Python 3.9+, including
when the server's environment is the thing that is broken. It changes nothing.

  --json      the structured result (a list of {name, ok, detail, remedy})
  --no-probe  skip the network probe (no socket is touched)
  --root DIR  the checkout to inspect (default: the one this file lives in)

Exit code is 0 when nothing FAILED, 1 otherwise ("not checked" is not a failure)."""


def main(argv=None) -> int:
    # The remedies carry the repo's house punctuation (— and ▸, as in mcp_tools.py's
    # extension errors). Piped stdout on Windows encodes with the ANSI codepage and
    # STRICT errors, so printing them there would raise UnicodeEncodeError: a doctor
    # that crashes while reporting the diagnosis is the exact failure this module
    # exists to remove. Degrade the characters, never the answer.
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    argv = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in argv
    probe = "--no-probe" not in argv
    root = None
    if "--root" in argv:
        index = argv.index("--root")
        if index + 1 >= len(argv):
            print(_USAGE, file=sys.stderr)
            return 2
        root = argv[index + 1]
        del argv[index:index + 2]
    for flag in ("--json", "--no-probe"):
        while flag in argv:
            argv.remove(flag)
    if argv:
        print(f"unknown argument: {argv[0]}\n\n{_USAGE}", file=sys.stderr)
        return 2

    root = root or repo_root()
    checks = run_checks(root, probe_server=probe)
    print(json.dumps(checks, indent=1) if as_json else render(checks, root=root))
    return 1 if any(c.get("ok") is False for c in checks) else 0


if __name__ == "__main__":
    sys.exit(main())
