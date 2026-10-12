"""Best-effort GitHub Copilot Chat session details for a VS Code extension host.

Copilot Chat runs inside VS Code's extension host (``Code.exe --type=utility
--utility-sub-type=node.mojom.NodeService``), so that is the pid the server sees for the
"GitHub Copilot Chat" MCP client. Its cwd is the VS Code install dir, which says nothing
about the window, and neither do its command line or environment. The extension host does
hold two files open that do, and its open-file list finds both without guessing. That list
comes from ``handle_scan``, never from a psutil call in this process: on Windows psutil
enumerates every handle on the system for it, and nothing on a /health path may do
unbounded kernel-wide work (see ``handle_scan``'s docstring):

* the window's own log, ``Code\\logs\\<run>\\window<N>\\exthost\\GitHub.copilot-chat\\
  GitHub Copilot Chat.log``: one ``ccreq:`` line per model request, plus the GitHub
  account and plan the Copilot token was issued for;
* ``Code\\User\\globalStorage\\github.copilot-chat\\session-store.db``: SQLite, the chat
  sessions and their turns.

The store is shared by every VS Code window, so the log is what ties a session to this
window: the session whose ``updated_at`` falls within two minutes of this window's last
chat request. The workspace is that session's ``cwd``; without one, the cwd most of the
extension host's children share (language servers run in the workspace folder).

This reads Copilot Chat's PRIVATE, UNDOCUMENTED log and store, as seen in VS Code in
September 2026. Any failure degrades to ``None``, for the whole answer or for one field.
"""
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from server.usage_panels import handle_scan
# Kept under its old name: the child process of handle_scan runs it, and the tests read it here.
from server.usage_panels.handle_scan import child_workspace  # noqa: F401

_LOG_NAME = "github copilot chat.log"
_DB_NAME = "session-store.db"
_TAIL_BYTES = 256 * 1024  # the log grows all day; only the tail holds the recent turns
# Local wall-clock time with no zone, as VS Code's log service writes it.
_TS = rb"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d(?:\.\d+)?) \[\w+\] "
# `ccreq:6ab5a30f.copilotmd | success | gpt-6-luna | 4342ms | [panel/editAgent]`. Only the
# `panel/` contexts are the user's chat; the rest (`progressMessages`,
# `copilotLanguageModelWrapper`, ...) are Copilot's own helper calls on a small model, and
# counting them would badge the helper model instead of the chat model.
_REQ_RE = re.compile(_TS + rb"ccreq:\S+ \| (\w+) \| ([^|\r\n]*?) \| \d+ms \| \[panel/", re.M)
# Failed attempts log "...You are currently logged in as X." first; only a token that
# was actually issued names the account in use, and the last one wins.
_ACCOUNT_RE = re.compile(_TS + rb"Got Copilot token for (\S+)", re.M)
_SKU_RE = re.compile(_TS + rb"copilot token sku: (\S+)", re.M)
# A chat turn is written to the store once the request finishes, seconds after its log
# line. Two minutes covers a long agent turn; when two windows' sessions both fall inside
# it, the closer one wins.
_MATCH_WINDOW_SECONDS = 120


def _text(b):
    return b.decode("utf-8", "replace")


def _local_time(b):
    try:
        return datetime.fromisoformat(_text(b))
    except ValueError:
        return None


def parse_log(data):
    """What a Copilot Chat log tail says, as a dict; fields not found are None.

    ``last_request`` is a naive local datetime of the last chat request of any status;
    ``requests`` and ``model`` count only the successful ones.
    """
    model, requests, last_request = None, 0, None
    for ts, status, req_model in _REQ_RE.findall(data):
        last_request = _local_time(ts) or last_request
        if status == b"success":
            requests += 1
            model = _text(req_model).strip() or model
    accounts = _ACCOUNT_RE.findall(data)
    skus = _SKU_RE.findall(data)
    return {
        "model": model,
        "account": _text(accounts[-1][1]) if accounts else None,
        "sku": _text(skus[-1][1]) if skus else None,
        "requests": requests,
        "last_request": last_request,
    }


def _read_tail(path):
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            start = max(0, fh.tell() - _TAIL_BYTES)
            fh.seek(start)
            data = fh.read()
    except OSError:
        return None
    if start:
        # A cut line could still parse, with a wrong value; drop it.
        data = data.partition(b"\n")[2]
    return data


def _parse_utc(text):
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def match_session(db_path, last_request_local, window=_MATCH_WINDOW_SECONDS):
    """``{'title','turns','cwd'}`` of the store's session updated closest to the given
    local time, if within ``window`` seconds of it; else ``None``."""
    try:
        when = last_request_local.astimezone(timezone.utc)
        # updated_at is ISO-8601 UTC with a fixed width, so a string range pre-filters;
        # the second of slack covers rows written without milliseconds.
        lo, hi = (when + timedelta(seconds=s) for s in (-window - 1, window + 1))
        fmt = "%Y-%m-%dT%H:%M:%S"
        con = sqlite3.connect(Path(db_path).as_uri() + "?mode=ro", uri=True, timeout=1)
        try:
            rows = con.execute(
                "SELECT id, cwd, summary, updated_at FROM sessions"
                " WHERE updated_at BETWEEN ? AND ?",
                (lo.strftime(fmt), hi.strftime(fmt) + "Z")).fetchall()
            best = None
            for sid, cwd, summary, updated_at in rows:
                at = _parse_utc(updated_at)
                if at is None:
                    continue
                delta = abs((at - when).total_seconds())
                if delta <= window and (best is None or delta < best[0]):
                    best = (delta, sid, cwd, summary)
            if best is None:
                return None
            (turns,) = con.execute("SELECT COUNT(*) FROM turns WHERE session_id = ?",
                                   (best[1],)).fetchone()
        finally:
            con.close()
    except Exception:
        return None
    return {"title": best[3] or None, "turns": turns, "cwd": best[2] or None}


def _newest(paths):
    def mtime(p):
        try:
            return os.path.getmtime(p)
        except OSError:
            return 0
    return max(paths, key=mtime) if paths else None


# Same reasoning as kilocode_data's cache: get_connections_info() is polled by the popup
# and by every launcher's /health. The open-file sweep itself is cached much longer, and
# off this path, by handle_scan; this TTL covers the log tail read and the store query.
_CACHE_TTL_SECONDS = 10
_cache = {}  # pid -> (expires_at_monotonic, result)


def detect_session(pid, _ttl=_CACHE_TTL_SECONDS):
    """Copilot Chat details for a VS Code extension host PID, or ``None``.

    Keys (each may be None): model, account, sku, requests, last_request_hms,
    chat_title, chat_turns, workspace. Results, misses included, are cached for ``_ttl``
    seconds per pid. While the extension host's open files are not known yet (the first
    call for a pid, or a busy machine) the answer is ``None`` and is not cached, so the
    next poll asks again.
    """
    if not pid:
        return None
    import time as _time
    now = _time.monotonic()
    hit = _cache.get(pid)
    if hit is not None and hit[0] > now:
        return hit[1]
    try:
        result = _detect_session_uncached(pid)
    except handle_scan.Pending:
        return None
    except Exception:
        result = None
    if len(_cache) > 64:
        for dead in [k for k, v in _cache.items() if v[0] <= now]:
            del _cache[dead]
    _cache[pid] = (now + _ttl, result)
    return result


def _detect_session_uncached(pid):
    # handle_scan answers from its cache and never waits; the process is not touched here.
    scan = handle_scan.lookup(pid, workspace=True)
    if scan is None:
        raise handle_scan.Pending
    paths = scan.paths

    log = _newest([p for p in paths if os.path.basename(p).lower() == _LOG_NAME])
    db = _newest([p for p in paths if os.path.basename(p).lower() == _DB_NAME
                  and "copilot" in p.lower()])
    info = parse_log((_read_tail(log) if log else None) or b"")
    last = info["last_request"]
    session = match_session(db, last) if db and last else None
    workspace = (session or {}).get("cwd") or scan.workspace

    result = {
        "model": info["model"],
        "account": info["account"],
        "sku": info["sku"],
        "requests": info["requests"],
        "last_request_hms": last.strftime("%H:%M:%S") if last else None,
        "chat_title": (session or {}).get("title"),
        "chat_turns": (session or {}).get("turns"),
        "workspace": workspace,
    }
    if not any(result.values()):
        return None
    return result
