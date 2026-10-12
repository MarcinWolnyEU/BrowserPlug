"""Tool arguments are redacted where they are recorded (call_telemetry), so neither
/health, the connection map nor a session's command history can carry a typed password,
eval code or a URL's query string.

Review finding S6 (2026-09-29): format_args used to keep every string argument whole
(cut at 200 chars), and page_act steps, page_eval code and full URLs went out on /health
to any local peer and into every connected browser's popup.
"""

import json
from datetime import datetime

import pytest

from server.reporting import call_telemetry, client_identity
from server.server import BrowserPlugServer

PASSWORD = "hunter2-Passw0rd"
CODE_SECRET = "sekrit-value-in-code"
QUERY_SECRET = "abc123sessionid"
USERINFO_SECRET = "ftp-pass-99"


def _call_body(name, arguments, rid=1):
    return json.dumps({"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                       "params": {"name": name, "arguments": arguments}}).encode()


CALLS = [
    ("page_act", {"tab_id": 7, "steps": [
        {"action": "fill", "target": "#password", "value": PASSWORD},
        {"action": "click", "target": "Log in"}]}),
    ("page_eval", {"code": f"localStorage.setItem('t', '{CODE_SECRET}')", "tab_id": 7}),
    ("navigate", {"url": f"https://user:{USERINFO_SECRET}@example.org/a/b?sid={QUERY_SECRET}#frag-{QUERY_SECRET}",
                  "tab_id": 7}),
    ("tabs", {"action": "open", "url": f"https://example.org/x?token={QUERY_SECRET}"}),
    ("requests", {"action": "get", "request_id": "r1", "filter": f"{PASSWORD}",
                  "url_patterns": [f"https://example.org/*?k={QUERY_SECRET}"]}),
]
SECRETS = [PASSWORD, CODE_SECRET, QUERY_SECRET, USERINFO_SECRET]


@pytest.fixture
def srv():
    s = BrowserPlugServer(host="localhost", port=8772, start_mcp=False)
    s.http_clients["sess-1234-abcd"] = {
        "pid": 501, "friendly": "Test client", "cwd": "C:/w", "exe_path": None,
        "cmdline": client_identity.sanitized_cmdline(
            ["client", "--api-key", PASSWORD, f"https://u:{USERINFO_SECRET}@example.org/?k={QUERY_SECRET}"]),
        "start_time": None, "launcher_pid": None, "launcher_start_time": None,
        "last_activity": datetime.now(), "history": []}
    return s


def _record_all(s):
    for i, (name, args) in enumerate(CALLS, 1):
        assert s._record_from_body("sess-1234-abcd", _call_body(name, args, i))
        s.telemetry.note_command_sent({"action": "x.y", "data": {"script": args.get("code"),
                                                                 "value": PASSWORD}})


def test_health_carries_no_argument_secrets(srv):
    _record_all(srv)
    text = json.dumps(srv.get_health_info(), default=repr)
    for secret in SECRETS:
        assert secret not in text


def test_connections_carry_no_argument_secrets(srv):
    _record_all(srv)
    text = json.dumps(srv.get_connections_info(kilo_details=False), default=repr)
    for secret in SECRETS:
        assert secret not in text, secret


def test_history_and_slots_hold_redacted_values(srv):
    _record_all(srv)
    hist = srv.http_clients["sess-1234-abcd"]["history"]
    blob = json.dumps([h["args"] for h in hist], default=repr) + json.dumps(
        [srv.telemetry.last_mcp, srv.telemetry.mcp_call, srv.telemetry.last_command], default=repr)
    for secret in SECRETS:
        assert secret not in blob


def test_shape_survives():
    p = call_telemetry.format_args(CALLS[0][1])
    assert p["tab_id"] == 7
    assert "fill" in p["steps"] and "click" in p["steps"]
    p = call_telemetry.format_args(CALLS[2][1])
    assert p["url"] == "https://example.org/a/b" or p["url"].startswith("https://example.org")
    assert "example.org" in p["url"] and "?" not in p["url"] and "@" not in p["url"]
    p = call_telemetry.format_args({"action": "open", "background": True, "code": "abcdef"})
    assert p["action"] == "open" and p["background"] is True
    assert p["code"] == "<text: 6 chars>"


def test_redaction_is_idempotent():
    once = call_telemetry.format_args(CALLS[0][1])
    assert call_telemetry.format_args(once) == once
    once = call_telemetry.format_args(CALLS[2][1])
    assert call_telemetry.format_args(once) == once


def test_cmdline_urls_lose_userinfo_and_query():
    line = client_identity.sanitized_cmdline(
        ["node", "srv.js", f"https://u:{USERINFO_SECRET}@example.org/p?k={QUERY_SECRET}"])
    assert USERINFO_SECRET not in line and QUERY_SECRET not in line
    assert "example.org" in line
