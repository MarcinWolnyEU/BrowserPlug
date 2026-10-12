"""Records of the newest MCP tools/call and the browser command it sent.

Three slots, each holding only the latest value, because /health and the connection map
show one call at a time:

  last_command  the newest WebSocket command sent to a browser and whether it was
                acknowledged (what /health calls `last_command`)
  last_mcp      the newest tools/call: tool, formatted params, client pid, session
  mcp_call      the timing lifecycle of that call. The stamps behind
                last_command.timings (see .tmp/dev-docs/connection-map.md):
                  recv_at       tools/call POST arrived (ASGI wrapper)
                  ws_sent_at    first WebSocket command of this call dispatched
                  ws_ack_at     last WebSocket answer for this call arrived
                  http_done_at  the HTTP response finished sending
                  launcher      {launcher_pid, total_ms}: the stdio side, POSTed by the
                                launcher to /timing after the call returns to the client

The server admits several concurrent calls, so a newer call overwrites the slots of an
older one. The stamping methods check session and tool before writing. The outcome writer
(`record_call_outcome`) finds its record by (session, JSON-RPC request id) in a small
bounded table, so a verdict never lands on another client's newer call.

`format_args` redacts argument values for display (typed text, code and query strings never
reach a slot, /health or the popup). The session table (`http_clients`) is
owned by the server and reached through a callable, since tests and the server replace it.
Nothing here imports server.py.
"""

import json
from server.reporting.arg_redaction import redact_args
from datetime import datetime
from collections import OrderedDict
from typing import Any, Callable, Dict, Optional, Tuple

#: How many recent call records `record_call_outcome` can still find. Calls are admitted
#: a handful at a time per browser, so this only has to outlast the slowest in flight.
_CALL_TABLE_SIZE = 64


def format_args(args):
    """Display form of a tool's arguments: shape kept, content replaced (arg_redaction.py).

    Idempotent, so history records redacted at recording time can be formatted again by
    the report builders."""
    return redact_args(args)


def note_browser_activity(browser, action):
    """Attribute a REAL browser command to the browser it was sent to. Heartbeats
    (extension_hello), pings, popup polls and reconnects never reach here, so
    last_activity reflects automation only."""
    if browser is None:
        return
    browser.last_activity = datetime.now()
    if action:
        browser.last_action = action


class CallTelemetry:
    def __init__(self, http_clients: Callable[[], Dict[str, Any]]):
        self._http_clients = http_clients
        self.last_command = None
        self.last_mcp = None
        self.mcp_call = None
        # (session prefix, str(request id)) -> the same dict mcp_call held for that call.
        self._calls: "OrderedDict[Tuple[str, str], Dict[str, Any]]" = OrderedDict()

    def record_call_outcome(self, tool_name: str, outcome: Optional[Dict[str, Any]],
                            call: Optional[Tuple[Any, Any]] = None) -> None:
        """File a finished call's structured verdict on ITS record.

        `call` is (session id, JSON-RPC request id). Without it (a caller that cannot see
        the request) the newest record for this tool is used, and when there is none the
        verdict is dropped: writing it to whatever call is newest is the bug this replaced.
        """
        session, request_id = call if call is not None else (None, None)
        record = None
        if session is not None and request_id is not None:
            record = self._calls.get((str(session)[:8], str(request_id)))
        else:
            for candidate in reversed(self._calls.values()):
                if candidate.get("tool") == tool_name and (
                        session is None or candidate.get("session") == str(session)[:8]):
                    record = candidate
                    break
        if record is not None:
            record["outcome"] = outcome

    def record_command(self, session_id, tool, args, req_id=None):
        """Append an MCP tool invocation to a session's recent-command history."""
        entry = self._http_clients().get(session_id)
        if entry is None:
            return
        hist = entry.setdefault("history", [])
        # Skip duplicate delivery of the same JSON-RPC request (same id seen twice).
        if hist and req_id is not None and hist[-1].get("_id") == req_id:
            return
        now = datetime.now()
        params = format_args(args)
        hist.append({"time": now, "tool": tool, "args": params, "_id": req_id})
        if len(hist) > 10:
            del hist[:-10]
        # Remember the most recent MCP call so the browser-level exchange can attribute
        # it to a client when building last_command / the /health report.
        self.last_mcp = {
            "tool": tool,
            "params": params,
            "client_pid": entry.get("pid"),
            "session": str(session_id)[:8],
            "at": now,
        }
        # Open this call's timing record; the stamps fill in as the call flows
        # through the WS exchange and back out over HTTP (docstring at __init__).
        # Carries params too: a call that never sends a WS command (browsers,
        # server_info) is represented in last_command from THIS record alone.
        self.mcp_call = {
            "tool": tool,
            "params": params,
            "session": str(session_id)[:8],
            "client_pid": entry.get("pid"),
            "launcher_pid": entry.get("launcher_pid"),
            "recv_at": now,
            "recv_epoch": now.timestamp(),
            "ws_sent_at": None,
            "ws_ack_at": None,
            "http_done_at": None,
            "browser_slot": None,
            "launcher": None,
        }
        if req_id is not None:
            self._calls[(str(session_id)[:8], str(req_id))] = self.mcp_call
            while len(self._calls) > _CALL_TABLE_SIZE:
                self._calls.popitem(last=False)

    def record_from_body(self, sid, body):
        """Parse a JSON-RPC request body and record any tools/call invocation.
        Returns True when one was recorded (the ASGI wrapper then times the
        HTTP response completion for that call)."""
        if not body:
            return False
        try:
            obj = json.loads(body)
        except Exception:
            return False
        items = obj if isinstance(obj, list) else [obj]
        recorded = False
        for it in items:
            if isinstance(it, dict) and it.get("method") == "tools/call":
                params = it.get("params") or {}
                self.record_command(sid, params.get("name"), params.get("arguments"),
                                    req_id=it.get("id"))
                recorded = True
        return recorded

    def note_mcp_http_done(self, sid):
        """Stamp the moment the HTTP response for the current MCP call finished
        sending; the end of the server's part of the cycle."""
        mc = self.mcp_call
        if mc is None or mc.get("http_done_at") is not None:
            return
        if mc.get("session") != str(sid)[:8]:
            return
        mc["http_done_at"] = datetime.now()

    def record_launcher_timing(self, report):
        """Merge a launcher's stdio-side report (POST /timing) into the current call.

        The launcher is the only process that can see the FULL cycle as the client
        experiences it (stdio in → stdio out); everything here is diagnostics, so
        matching is deliberately tolerant: same tool, overlapping time window, and
        the client pid when both sides know it. Returns True when it matched."""
        if not isinstance(report, dict):
            return False
        mc = self.mcp_call
        try:
            tool = report.get("tool")
            started = float(report.get("started_epoch"))
            total_ms = float(report.get("total_ms"))
        except (TypeError, ValueError):
            return False
        if mc is None or not tool or mc.get("tool") != tool:
            return False
        recv = mc.get("recv_epoch") or 0
        # The server saw the call while the launcher's clock says it was running
        # (same machine, same wall clock; ±2s of slack for scheduling).
        if not (started - 2.0 <= recv <= started + total_ms / 1000.0 + 2.0):
            return False
        client_pid = report.get("client_pid")
        if client_pid and mc.get("client_pid") and int(client_pid) != int(mc["client_pid"]):
            return False
        mc["launcher"] = {
            "launcher_pid": report.get("launcher_pid"),
            "total_ms": round(total_ms, 1),
        }
        return True

    @staticmethod
    def _ms_between(a, b):
        """(b - a) in milliseconds, or None when either stamp is missing."""
        if a is None or b is None:
            return None
        return round((b - a).total_seconds() * 1000.0, 1)

    def _attach_call_timings(self, out, mc):
        """Fill `out` with the per-hop timing breakdown of call record `mc`."""
        launcher = mc.get("launcher") or {}
        server_ms = self._ms_between(mc.get("recv_at"), mc.get("http_done_at"))
        launcher_ms = launcher.get("total_ms")
        out["browser_slot"] = mc.get("browser_slot")
        out["launcher_pid"] = launcher.get("launcher_pid") or mc.get("launcher_pid")
        out["timings"] = {
            # The map keys replay-once-per-call on this.
            "started_epoch": mc.get("recv_epoch"),
            # tools/call arrival → first WS dispatch: the server's own overhead.
            # None for a call that never touches a browser (browsers, server_info).
            "server_dispatch_ms": self._ms_between(mc.get("recv_at"), mc.get("ws_sent_at")),
            # first WS dispatch → last WS answer: the browser leg.
            "browser_ms": self._ms_between(mc.get("ws_sent_at"), mc.get("ws_ack_at")),
            # last WS answer → HTTP response fully sent: result formatting.
            "server_finalize_ms": self._ms_between(mc.get("ws_ack_at"), mc.get("http_done_at")),
            # the whole server-side stay.
            "server_ms": server_ms,
            # the whole cycle as the CLIENT saw it (launcher-reported).
            "launcher_ms": launcher_ms,
            # stdio framing + HTTP transport + proxy work.
            "proxy_overhead_ms": (round(launcher_ms - server_ms, 1)
                                  if launcher_ms is not None and server_ms is not None
                                  else None),
            "complete": mc.get("http_done_at") is not None,
        }

    def last_command_info(self):
        """The `last_command` object for /health and the connections payload.

        Two shapes, because not every MCP call sends a browser command:
          * the newest MCP call sent WS command(s) → the WS-level record
            (last_command) plus that call's timing breakdown;
          * the newest MCP call is SERVER-SIDE ONLY (browsers, server_info) → the
            call record itself: action/ack fields empty, browser-leg timings None.
            Without this branch such calls were invisible here while still showing
            in the per-session history, the inconsistency reported 2026-07-15.
        """
        lc = self.last_command
        mc = self.mcp_call

        def fmt_ms(dt):
            return dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] if dt else None

        lc_tool = (lc.get("tool") or lc.get("action")) if lc else None
        lc_matches = (lc is not None and mc is not None
                      and mc.get("tool") == lc_tool
                      and mc.get("session") == lc.get("session"))

        # The newest MCP call has no WS command of its own: represent it directly
        # rather than showing a stale older command (or nothing at all).
        if (mc is not None and not lc_matches
                and (lc is None or (mc.get("recv_at") is not None
                                    and lc.get("at") is not None
                                    and mc["recv_at"] >= lc["at"]))):
            out = {
                "tool": mc.get("tool"),
                "action": None,                     # no browser-level action ran
                "params": mc.get("params"),
                "at": fmt_ms(mc.get("recv_at")),
                "client_pid": mc.get("client_pid"),
                "session": mc.get("session"),
                "acknowledged_by_extension": None,  # nothing to acknowledge
                "acknowledged_at": None,
                # The structured verdict. An MCP error result carries no _meta, so
                # this is the only channel that tells an operator a call FAILED and whether
                # retrying it is sane. Monitoring that reads the protocol flag alone saw a
                # 0.7% failure rate for a system failing at 22%.
                "outcome": mc.get("outcome"),
            }
            self._attach_call_timings(out, mc)
            return out

        if not lc:
            return None

        out = {
            "tool": lc_tool,
            "action": lc.get("action"),
            "params": lc.get("params"),
            "at": fmt_ms(lc.get("at")),
            "client_pid": lc.get("client_pid"),
            "session": lc.get("session"),
            "acknowledged_by_extension": lc.get("acknowledged"),
            "acknowledged_at": fmt_ms(lc.get("acknowledged_at")),
            # The structured verdict; see the sibling branch above. Only meaningful when
            # this WS command belongs to the newest MCP call; an older command has no
            # verdict of its own.
            "outcome": mc.get("outcome") if lc_matches else None,
        }
        if lc_matches:
            self._attach_call_timings(out, mc)
        return out

    def note_command_sent(self, request):
        """Record a browser command being sent to the extension (ack filled in later)."""
        lm = self.last_mcp or {}
        record = {
            "tool": lm.get("tool"),
            "action": request.get("action"),
            "params": lm.get("params") if lm.get("params") is not None
                      else format_args(request.get("data")),
            "client_pid": lm.get("client_pid"),
            "session": lm.get("session"),
            "at": datetime.now(),
            "acknowledged": False,
            "acknowledged_at": None,
        }
        self.last_command = record
        # Dispatch stamp: only the FIRST WebSocket command of an MCP call marks
        # ws_sent_at: "recv → first dispatch" is the server's own overhead. A
        # multi-command tool (page_snapshot over frames) keeps the first stamp;
        # its later commands are part of the browser leg, not of dispatch.
        mc = self.mcp_call
        if (mc is not None and mc.get("tool") == lm.get("tool")
                and mc.get("session") == lm.get("session")
                and mc.get("ws_sent_at") is None):
            mc["ws_sent_at"] = record["at"]
        return record
