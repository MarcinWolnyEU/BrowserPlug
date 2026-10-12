"""Unit tests for launcher/stdio_relay.py, the launcher's stdlib MCP transport.

The relay replaced an MCP-library proxy to cut the launcher from ~90 MB RSS to ~30 MB (that
proxy is now the opt-in server/proxy/ transport, a child process of its own), so the tests
split in two along that reason:

  BEHAVIOUR: the relay has to be indistinguishable from the proxy at the stdio boundary.
             Frames go out as the bytes the server sent (a re-serializing relay could
             reorder keys or re-escape a string and nobody would notice until a client
             compared payloads), a notification draws no reply, and a session that dies
             under a /restart is rebuilt and the call retried, which is the one property
             the proxy got for free from its per-request backend sessions.

  WEIGHT:    test_relay_path_never_imports_the_fastmcp_stack is the point of the whole
             exercise. It runs in a SUBPROCESS on purpose: this test process has mcp
             imported by its neighbours, so an in-process check would pass no matter what
             launcher.py did. The guard is not hypothetical. A single module-level
             `from mcp.shared._httpx_utils import ...` in launcher.py pulled 641 modules
             and 50 MB into a relay that never calls httpx, and it survived the first
             round of this work precisely because nothing failed when it did.

No network and no sleeps: the backend is scripted and poll_seconds is 0.
"""

import email.message
import io
import json
import subprocess
import sys
import textwrap
import threading
import time

import pytest

from launcher import launcher
from launcher.stdio_relay import StdioRelay, messages_from
from test_relay_era import VERSION, Backend, modern_frame

_REPO_ROOT = str(__import__("pathlib").Path(launcher.__file__).resolve().parent.parent)
_SHARED_PY = _REPO_ROOT + "/shared-py"


def hdrs(**kw):
    """An HTTP header mapping with .get() that is case-insensitive, as urllib returns."""
    m = email.message.Message()
    for k, v in kw.items():
        m[k.replace("_", "-")] = v
    return m


def sse(*frames):
    return "".join("event: message\ndata: %s\n\n" % f for f in frames)


def make_relay(out=None):
    logged = []
    relay = StdioRelay(2830, log=lambda *a: logged.append(" ".join(str(x) for x in a)),
                       out=out, poll_seconds=0)
    relay.logged = logged
    return relay


@pytest.fixture
def backend():
    """Fake /mcp endpoints on loopback sockets (test_relay_era.Backend), closed afterwards."""
    made = []

    def make(respond=None):
        made.append(Backend(respond))
        return made[-1]

    yield make
    for b in made:
        b.close()


class FakeBackend:
    """A scripted POST: returns queued (status, headers, text) and records what it saw."""

    def __init__(self, relay, *responses):
        self.relay = relay
        self.responses = list(responses)
        self.calls = []

    def __call__(self, body, timeout=None):
        self.calls.append({"body": body,
                           "session": relay_session(self.relay),
                           "protocol": self.relay._protocol_version})
        if not self.responses:
            raise AssertionError("backend called more times than the test scripted")
        return self.responses.pop(0)


def relay_session(relay):
    return relay._session_id


class TestFrameParsing:
    def test_sse_and_plain_json_bodies_both_yield_their_frames(self):
        body = sse('{"id":1}', '{"id":2}')
        assert messages_from(hdrs(Content_Type="text/event-stream"), body) == \
            ['{"id":1}', '{"id":2}']
        assert messages_from(hdrs(Content_Type="application/json"), '{"id":3}') == \
            ['{"id":3}']

    @pytest.mark.parametrize("sep", [" ", " ", "\x85", "\x0b", "\x0c",
                                     "\x1c", "\x1d", "\x1e"])
    def test_a_frame_holding_a_unicode_line_separator_stays_whole(self, sep):
        """SSE ends a line at CR, LF or CRLF only. str.splitlines() also cuts at these
        characters, which JSON leaves unescaped, so the frame arrived truncated and the
        client waited for its id until its own timeout."""
        frame = '{"jsonrpc":"2.0","id":7,"result":{"text":"a%sb"}}' % sep
        got = messages_from(hdrs(Content_Type="text/event-stream"), sse(frame))
        assert got == [frame]
        if ord(sep) >= 0x20:    # a raw C0 character is not legal JSON; the others are
            assert json.loads(got[0])["result"]["text"] == "a%sb" % sep

    @pytest.mark.parametrize("eol", ["\r\n", "\r", "\n"])
    def test_every_sse_line_ending_still_splits_data_lines(self, eol):
        body = eol.join(["event: message", 'data: {"id":1}', "", "event: message",
                         'data: {"id":2}', "", ""])
        assert messages_from(hdrs(Content_Type="text/event-stream"), body) == \
            ['{"id":1}', '{"id":2}']

    def test_an_empty_body_yields_nothing(self):
        """A 202 for a notification carries no frame, and must not fabricate one."""
        assert messages_from(hdrs(Content_Type="application/json"), "") == []
        assert messages_from(hdrs(Content_Type="text/event-stream"), "") == []


class TestForwarding:
    def test_response_frames_reach_stdout_as_the_bytes_the_server_sent(self):
        """Verbatim, not re-serialized: odd spacing and key order must survive.

        json.dumps would normalize both, so a relay that round-trips the payload looks
        correct in every test that compares parsed objects and still alters what the
        client receives.
        """
        import io
        out = io.BytesIO()
        relay = make_relay(out=out)
        quirky = '{"jsonrpc":"2.0",  "id":7, "result":{"z":1,"a":2}}'
        relay.post = FakeBackend(relay, (200, hdrs(Content_Type="text/event-stream"),
                                         sse(quirky)))
        relay.handle_frame('{"jsonrpc":"2.0","id":7,"method":"tools/list"}')
        assert out.getvalue() == quirky.encode() + b"\n"

    def test_a_notification_draws_no_reply(self):
        import io
        out = io.BytesIO()
        relay = make_relay(out=out)
        relay.post = FakeBackend(relay, (202, hdrs(Content_Type="application/json"), ""))
        relay.handle_frame('{"jsonrpc":"2.0","method":"notifications/initialized"}')
        assert out.getvalue() == b""

    def test_an_unparseable_frame_is_dropped_and_logged_not_raised(self):
        relay = make_relay()
        relay.post = FakeBackend(relay)  # must never be called
        relay.handle_frame("not json at all")
        assert relay.post.calls == []
        assert any("unparseable" in line for line in relay.logged)


class TestSession:
    def test_initialize_captures_the_session_and_later_calls_carry_it(self):
        relay = make_relay()
        init_result = json.dumps({"jsonrpc": "2.0", "id": 1,
                                  "result": {"protocolVersion": "2025-06-18"}})
        relay.post = FakeBackend(
            relay,
            (200, hdrs(Content_Type="text/event-stream", Mcp_Session_Id="abc123"),
             sse(init_result)),
            (200, hdrs(Content_Type="text/event-stream"), sse('{"id":2}')),
        )
        relay.handle_frame(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}))
        assert relay._session_id == "abc123"
        assert relay._protocol_version == "2025-06-18"

        relay.handle_frame(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}))
        assert relay.post.calls[1]["session"] == "abc123"
        assert relay.post.calls[1]["protocol"] == "2025-06-18"

    def test_a_dead_session_is_rebuilt_and_the_call_retried_once(self):
        """A /restart must stay invisible to the client.

        The replacement comes back on the SAME port with no memory of our session, so the
        next call gets a 404. The proxy path survived this through per-request backend
        sessions; here it is an explicit rebuild, and the client must still see one clean
        answer to the one call it made.
        """
        import io
        out = io.BytesIO()
        relay = make_relay(out=out)
        relay._session_id = "stale"
        relay._initialize_frame = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize"}).encode()

        fresh_init = json.dumps({"jsonrpc": "2.0", "id": 1,
                                 "result": {"protocolVersion": "2025-06-18"}})
        relay.post = FakeBackend(
            relay,
            (404, hdrs(Content_Type="application/json"), '{"error":"session not found"}'),
            (200, hdrs(Content_Type="text/event-stream", Mcp_Session_Id="fresh"),
             sse(fresh_init)),                                   # the rebuild
            (202, hdrs(Content_Type="application/json"), ""),    # notifications/initialized
            (200, hdrs(Content_Type="text/event-stream"), sse('{"id":9,"result":{}}')),
        )
        relay.handle_frame('{"jsonrpc":"2.0","id":9,"method":"tools/call",'
                           '"params":{"name":"server_info"}}')

        assert relay._session_id == "fresh"
        # exactly one answer to the one call, and no 404 body leaked to the client
        assert out.getvalue() == b'{"id":9,"result":{}}\n'
        assert relay.post.calls[-1]["session"] == "fresh"

    def test_a_rebuild_is_not_attempted_before_the_client_ever_initialized(self):
        """No stored initialize frame means nothing to replay; failing is the honest answer."""
        relay = make_relay()
        relay._session_id = None
        relay.post = FakeBackend(relay)  # must never be called
        assert relay.reestablish() is False
        assert relay.post.calls == []

    def test_a_failed_rebuild_still_answers_the_call_once_under_its_own_id(self):
        """The /restart case where the replayed initialize is refused: mcp's 404 carries id
        null, and the client must get exactly one error under the id it is waiting on."""
        import io
        out = io.BytesIO()
        relay = make_relay(out=out)
        reported = []
        relay.report_timing = lambda tool, started, ms: reported.append(tool)
        relay._session_id = "dead"
        relay._initialize_frame = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize"}).encode()
        relay.post = FakeBackend(
            relay,
            (404, hdrs(Content_Type="application/json"),
             '{"jsonrpc":"2.0","id":null,"error":{"code":-32600,"message":"Session not found"}}'),
            (503, hdrs(Content_Type="text/plain"), "Service Unavailable"),  # the replay
        )
        relay.handle_frame('{"jsonrpc":"2.0","id":9,"method":"tools/call",'
                           '"params":{"name":"server_info"}}')
        lines = out.getvalue().splitlines()
        assert len(lines) == 1
        reply = json.loads(lines[0])
        assert reply["id"] == 9
        assert reply["error"]["message"] == "Session not found"
        assert reported == ["server_info"]


class TestFailureIsAnswered:
    def test_an_unreachable_backend_answers_the_request_instead_of_hanging(self):
        import io
        out = io.BytesIO()
        relay = make_relay(out=out)

        def boom(body, timeout=None):
            raise OSError("connection refused")

        relay.post = boom
        relay.handle_frame('{"jsonrpc":"2.0","id":4,"method":"tools/list"}')
        reply = json.loads(out.getvalue())
        assert reply["id"] == 4
        assert reply["error"]["code"] == -32603
        assert "could not reach the shared server" in reply["error"]["message"]

    def test_an_unreachable_backend_stays_silent_for_a_notification(self):
        import io
        out = io.BytesIO()
        relay = make_relay(out=out)

        def boom(body, timeout=None):
            raise OSError("connection refused")

        relay.post = boom
        relay.handle_frame('{"jsonrpc":"2.0","method":"notifications/cancelled"}')
        assert out.getvalue() == b""

    def test_an_http_error_with_no_body_still_answers_the_client(self):
        import io
        out = io.BytesIO()
        relay = make_relay(out=out)
        relay.post = FakeBackend(relay, (500, hdrs(Content_Type="application/json"), ""))
        relay.handle_frame('{"jsonrpc":"2.0","id":5,"method":"tools/list"}')
        reply = json.loads(out.getvalue())
        assert reply["id"] == 5
        assert "HTTP 500" in reply["error"]["message"]

    def test_an_error_the_server_sent_with_a_null_id_is_reissued_under_the_request_id(self):
        """mcp answers a sessionless non-initialize request with id null ("Missing session
        ID"); forwarded as is, the client waits on its own id until it times out."""
        import io
        out = io.BytesIO()
        relay = make_relay(out=out)
        relay.post = FakeBackend(relay, (400, hdrs(Content_Type="application/json"),
                                         '{"jsonrpc":"2.0","id":null,"error":{"code":-32600,'
                                         '"message":"Bad Request: Missing session ID"}}'))
        relay.handle_frame('{"jsonrpc":"2.0","id":"probe-1","method":"server/discover"}')
        lines = out.getvalue().splitlines()
        assert len(lines) == 1
        reply = json.loads(lines[0])
        assert reply["id"] == "probe-1"
        assert reply["error"] == {"code": -32600, "message": "Bad Request: Missing session ID"}

    def test_a_plain_text_error_body_never_reaches_stdout(self):
        """mcp 2's body limiter answers 413 in text/plain and Starlette answers an escaped
        exception with a text/plain 500; stdout may carry nothing but JSON-RPC."""
        import io
        out = io.BytesIO()
        relay = make_relay(out=out)
        relay.post = FakeBackend(relay, (413, hdrs(Content_Type="text/plain"),
                                         "Request body too large"))
        relay.handle_frame('{"jsonrpc":"2.0","id":5,"method":"tools/call",'
                           '"params":{"name":"page_eval"}}')
        lines = out.getvalue().splitlines()
        assert len(lines) == 1
        reply = json.loads(lines[0])
        assert reply["id"] == 5
        assert reply["error"]["message"] == (
            "shared server returned HTTP 413 for tools/call: Request body too large")

    def test_a_refused_notification_draws_no_reply(self):
        """JSON-RPC forbids answering a notification; mcp's refusal has id null."""
        import io
        out = io.BytesIO()
        relay = make_relay(out=out)
        relay.post = FakeBackend(relay, (400, hdrs(Content_Type="application/json"),
                                         '{"jsonrpc":"2.0","id":null,"error":{"code":-32600,'
                                         '"message":"Bad Request: Missing session ID"}}'))
        relay.handle_frame('{"jsonrpc":"2.0","method":"notifications/cancelled"}')
        assert out.getvalue() == b""
        assert any("Missing session ID" in line for line in relay.logged)

    def test_an_error_that_already_carries_the_request_id_passes_verbatim(self):
        import io
        out = io.BytesIO()
        relay = make_relay(out=out)
        frame = ('{"jsonrpc":"2.0","id":6,"error":{"code":-32022,"message":"Unsupported '
                 'protocol version","data":{"supported":["2025-11-25"],"requested":"2026-07-28"}}}')
        relay.post = FakeBackend(relay, (400, hdrs(Content_Type="application/json"), frame))
        relay.handle_frame('{"jsonrpc":"2.0","id":6,"method":"server/discover"}')
        assert out.getvalue() == frame.encode() + b"\n"


class TestTiming:
    def test_a_tools_call_reports_its_timing_and_other_methods_do_not(self):
        relay = make_relay()
        reported = []
        relay.report_timing = lambda tool, started, ms: reported.append(tool)
        relay.post = FakeBackend(
            relay,
            (200, hdrs(Content_Type="text/event-stream"), sse('{"id":1}')),
            (200, hdrs(Content_Type="text/event-stream"), sse('{"id":2}')),
        )
        relay.handle_frame('{"jsonrpc":"2.0","id":1,"method":"tools/call",'
                           '"params":{"name":"navigate"}}')
        relay.handle_frame('{"jsonrpc":"2.0","id":2,"method":"tools/list"}')
        assert reported == ["navigate"]


class TestToolSurfaceWatch:
    def _run_watch(self, relay, payloads):
        """Drive watch_tools over a scripted /health sequence, then stop the loop."""
        seq = list(payloads)

        def health():
            if not seq:
                relay._stopping.set()
                raise RuntimeError("scripted sequence exhausted")
            return seq.pop(0)

        relay.health = health
        relay.watch_tools()

    def test_the_first_observation_is_a_baseline_and_pushes_nothing(self):
        """Otherwise every launcher would announce a change the moment it started."""
        import io
        out = io.BytesIO()
        relay = make_relay(out=out)
        self._run_watch(relay, [{"server_pid": 1, "tools_signature": "a"},
                                {"server_pid": 1, "tools_signature": "a"}])
        assert out.getvalue() == b""

    def test_a_changed_pid_or_signature_pushes_tools_list_changed(self):
        import io
        out = io.BytesIO()
        relay = make_relay(out=out)
        self._run_watch(relay, [{"server_pid": 1, "tools_signature": "a"},
                                {"server_pid": 1, "tools_signature": "b"},
                                {"server_pid": 2, "tools_signature": "b"}])
        pushed = [json.loads(l) for l in out.getvalue().splitlines()]
        assert len(pushed) == 2
        assert all(p["method"] == "notifications/tools/list_changed" for p in pushed)
        assert all("id" not in p for p in pushed)  # a notification carries no id


class TestRelaySelection:
    def test_the_relay_is_the_default_and_zero_is_the_escape_hatch(self, monkeypatch):
        monkeypatch.delenv(launcher.RELAY_ENV, raising=False)
        assert launcher.relay_enabled() is True
        for off in ("0", "false", "no", "FALSE", " 0 "):
            monkeypatch.setenv(launcher.RELAY_ENV, off)
            assert launcher.relay_enabled() is False, off
        for on in ("1", "true", "yes", ""):
            monkeypatch.setenv(launcher.RELAY_ENV, on)
            assert launcher.relay_enabled() is True, on

    def test_main_routes_to_the_relay_by_default_and_to_the_proxy_when_told(
            self, monkeypatch):
        """Pins the DEFAULT, which no other test can see.

        tests/unit/test_launcher.py forces the proxy for its whole module, so if the
        default silently flipped back every suite here would still be green while every
        client on the box quietly went back to paying 90 MB.
        """
        calls = []
        monkeypatch.setattr(launcher, "ensure_server", lambda: 2830)
        monkeypatch.setattr(launcher, "serve_via_relay",
                            lambda port: calls.append(("relay", port)))
        monkeypatch.setattr(launcher, "serve_via_proxy",
                            lambda port: calls.append(("proxy", port)))
        monkeypatch.setattr(launcher, "browserplug_health", lambda port: {})

        monkeypatch.delenv(launcher.RELAY_ENV, raising=False)
        launcher.main()
        monkeypatch.setenv(launcher.RELAY_ENV, "0")
        launcher.main()

        assert calls == [("relay", 2830), ("proxy", 2830)]


class TestWeight:
    """The reason the relay exists. See this module's docstring for why it subprocesses."""

    # httpx2/httpcore2/mcp_types: the fastmcp>=4 / mcp>=2 stack; httpx stays for older ones.
    HEAVY = ("fastmcp", "mcp", "mcp_types", "httpx", "httpx2", "httpcore2", "pydantic",
             "uvicorn", "starlette", "jsonschema", "watchfiles", "asyncio")

    def test_relay_path_never_imports_the_fastmcp_stack(self):
        probe = textwrap.dedent("""
            import sys
            sys.path[:0] = [%r, %r]
            import launcher.launcher, launcher.stdio_relay
            heavy = sorted({m.split('.')[0] for m in sys.modules} & set(%r))
            print(",".join(heavy))
        """) % (_REPO_ROOT, _SHARED_PY, self.HEAVY)
        out = subprocess.run([sys.executable, "-c", probe],
                             capture_output=True, text=True, timeout=120)
        assert out.returncode == 0, out.stderr
        pulled = [x for x in out.stdout.strip().split(",") if x]
        assert pulled == [], (
            "importing launcher.py pulled %s. Every one of these rides the relay's "
            "memory budget for a path that never calls them; move the import inside "
            "the proxy-path function that needs it." % pulled)

    def test_the_relay_module_alone_imports_nothing_heavy(self):
        probe = textwrap.dedent("""
            import sys
            sys.path[:0] = [%r, %r]
            import launcher.stdio_relay
            heavy = sorted({m.split('.')[0] for m in sys.modules} & set(%r))
            print(",".join(heavy))
        """) % (_REPO_ROOT, _SHARED_PY, self.HEAVY)
        out = subprocess.run([sys.executable, "-c", probe],
                             capture_output=True, text=True, timeout=120)
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == ""


class TestSessionRaces:
    """The session id is shared by every worker thread; a restart makes them all race.

    These run the real post() against a fake HTTP server, since the bugs sit between
    reading the session id and sending it, which a stubbed post() skips entirely.
    """

    @staticmethod
    def start_backend(live, inits, slow_dead_call=None):
        import http.server
        import itertools
        import threading
        import time
        counter = itertools.count(2)

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def reply(self, status, payload, headers=None):
                data = json.dumps(payload).encode() if payload is not None else b""
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                msg = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                sid = self.headers.get("Mcp-Session-Id")
                method, mid = msg.get("method"), msg.get("id")
                if method == "initialize":
                    time.sleep(0.2)     # one real round trip to the new server
                    new = "S{}".format(next(counter))
                    live.add(new)
                    inits.append(new)
                    return self.reply(200, {"jsonrpc": "2.0", "id": mid, "result": {
                        "protocolVersion": "2025-06-18"}}, {"Mcp-Session-Id": new})
                if method == "notifications/initialized":
                    return self.reply(202, None)
                if slow_dead_call == mid and sid not in live:
                    time.sleep(0.6)     # this call's 404 lands after the rebuild
                if sid is None:
                    return self.reply(400, {"jsonrpc": "2.0", "id": None, "error": {
                        "code": -32600, "message": "Bad Request: Missing session ID"}})
                if sid not in live:
                    return self.reply(404, {"jsonrpc": "2.0", "id": None, "error": {
                        "code": -32600, "message": "Session not found"}})
                return self.reply(200, {"jsonrpc": "2.0", "id": mid, "result": {"sid": sid}})

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server

    def run_two_calls(self, slow_dead_call, gap):
        import io
        import threading
        import time
        live, inits = {"S1"}, []
        live.discard("S1")      # the restart: the relay's S1 is gone
        server = self.start_backend(live, inits, slow_dead_call)
        try:
            out = io.BytesIO()
            relay = StdioRelay(server.server_address[1], log=lambda *a: None,
                               host="127.0.0.1", out=out)
            relay.report_timing = lambda *a: None
            relay._initialize_frame = (b'{"jsonrpc":"2.0","id":0,"method":"initialize",'
                                       b'"params":{}}')
            relay._session_id = "S1"
            frames = ['{{"jsonrpc":"2.0","id":"{}","method":"tools/call","params":{{}}}}'
                      .format(i) for i in "AB"]
            threads = [threading.Thread(target=relay.handle_frame, args=(f,)) for f in frames]
            for t in threads:
                t.start()
                time.sleep(gap)
            for t in threads:
                t.join(30)
            replies = {json.loads(l)["id"]: json.loads(l)
                       for l in out.getvalue().decode().splitlines()}
            return replies, inits
        finally:
            server.shutdown()
            server.server_close()

    def test_a_call_during_a_rebuild_waits_for_the_new_session_not_none(self):
        replies, inits = self.run_two_calls(slow_dead_call=None, gap=0.05)
        assert sorted(replies) == ["A", "B"]
        assert all("result" in r for r in replies.values()), replies
        assert len(inits) == 1, inits

    def test_a_late_404_does_not_wipe_the_session_another_call_rebuilt(self):
        replies, inits = self.run_two_calls(slow_dead_call="B", gap=0.0)
        assert sorted(replies) == ["A", "B"]
        assert all("result" in r for r in replies.values()), replies
        assert len(inits) == 1, "a second initialize replay orphans a server session: %s" % inits


class TestFakeServerRegressions:
    """L6 and L13 of the 2026-09-29 launcher review, against a real fake HTTP server."""

    @staticmethod
    def start(script):
        """`script(method, session_id, msg)` returns (status, content_type, body, headers)."""
        import http.server
        import threading
        posts = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                msg = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                sid = self.headers.get("Mcp-Session-Id")
                posts.append((msg.get("method"), sid))
                status, ctype, body, extra = script(msg.get("method"), sid, msg)
                data = body.encode()
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                for k, v in extra.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server, posts

    @staticmethod
    def relay_for(server, out):
        relay = StdioRelay(server.server_address[1], log=lambda *a: None,
                           host="127.0.0.1", out=out, poll_seconds=0)
        relay.report_timing = lambda *a: None
        relay._initialize_frame = b'{"jsonrpc":"2.0","id":0,"method":"initialize","params":{}}'
        relay._session_id = "live"
        return relay

    @pytest.mark.parametrize("body", [
        "id: 0\ndata: \n\n: ping - 2026-09-29\n\n",
        'event: message\ndata: {"jsonrpc":"2.0","method":"notifications/message",'
        '"params":{"level":"info","data":"x"}}\n\n',
    ], ids=["priming-event-only", "log-notification-only"])
    def test_a_2xx_stream_that_ends_without_the_answer_is_answered_with_an_error(self, body):
        import io
        server, posts = self.start(lambda m, s, msg: (200, "text/event-stream", body, {}))
        try:
            out = io.BytesIO()
            relay = self.relay_for(server, out)
            relay.handle_frame('{"jsonrpc":"2.0","id":42,"method":"tools/call",'
                               '"params":{"name":"navigate"}}')
        finally:
            server.shutdown()
            server.server_close()
        frames = [json.loads(l) for l in out.getvalue().decode().splitlines()]
        answers = [f for f in frames if f.get("id") == 42]
        assert len(answers) == 1, frames
        assert answers[0]["error"]["code"] == -32603
        assert "without an answer" in answers[0]["error"]["message"]
        assert len(posts) == 1, "a tools/call must never be replayed blindly"

    def test_a_2xx_stream_with_the_answer_still_passes_verbatim(self):
        import io
        answer = '{"jsonrpc":"2.0","id":42,"result":{"ok":true}}'
        server, _ = self.start(lambda m, s, msg: (
            200, "text/event-stream", "id: 0\ndata: \n\n" + sse(answer), {}))
        try:
            out = io.BytesIO()
            relay = self.relay_for(server, out)
            relay.handle_frame('{"jsonrpc":"2.0","id":42,"method":"tools/list"}')
        finally:
            server.shutdown()
            server.server_close()
        assert out.getvalue() == answer.encode() + b"\n"

    def test_a_malformed_frame_400_does_not_tear_down_the_live_session(self):
        import io
        parse_error = json.dumps({"jsonrpc": "2.0", "id": None, "error": {
            "code": -32700, "message": "Parse error: Invalid JSON"}})
        server, posts = self.start(
            lambda m, s, msg: (400, "application/json", parse_error, {}))
        try:
            out = io.BytesIO()
            relay = self.relay_for(server, out)
            relay.handle_frame('{"jsonrpc":"2.0","id":5,"method":"tools/call","params":{}}')
        finally:
            server.shutdown()
            server.server_close()
        assert relay._session_id == "live"
        assert [m for m, _ in posts] == ["tools/call"], posts   # no initialize replay
        replies = [json.loads(l) for l in out.getvalue().decode().splitlines()]
        assert [r["id"] for r in replies] == [5]
        assert replies[0]["error"]["code"] == -32700

    def test_a_400_naming_a_missing_or_invalid_session_still_rebuilds(self):
        import io
        for message in ("Bad Request: Missing session ID",
                        "Not Found: Invalid or expired session ID"):
            calls = {"n": 0}

            def script(method, sid, msg, message=message, calls=calls):
                if method == "initialize":
                    return (200, "application/json", json.dumps(
                        {"jsonrpc": "2.0", "id": 0, "result": {"protocolVersion": "2025-06-18"}}),
                        {"Mcp-Session-Id": "fresh"})
                if method == "notifications/initialized":
                    return 202, "application/json", "", {}
                if sid != "fresh":
                    return (400, "application/json", json.dumps({
                        "jsonrpc": "2.0", "id": None,
                        "error": {"code": -32600, "message": message}}), {})
                return (200, "application/json", json.dumps(
                    {"jsonrpc": "2.0", "id": msg["id"], "result": {}}), {})

            server, posts = self.start(script)
            try:
                out = io.BytesIO()
                relay = self.relay_for(server, out)
                relay.handle_frame('{"jsonrpc":"2.0","id":5,"method":"tools/list"}')
            finally:
                server.shutdown()
                server.server_close()
            assert relay._session_id == "fresh", message
            assert json.loads(out.getvalue())["result"] == {}, message


class TestDeadlinesAndLanes:
    """L10 and L14 of the 2026-09-29 launcher review, driven through run() and a real
    fake HTTP server so the transport, the pools and the deadline are all the real ones."""

    @staticmethod
    def run_relay(relay, lines, until, hold=5.0):
        """Feed `lines`, keep stdin open until `until()` is true or `hold` s pass."""
        import threading
        import time

        def stdin():
            for line in lines:
                yield (line + "\n").encode()
            end = time.monotonic() + hold
            while not until() and time.monotonic() < end:
                time.sleep(0.02)

        result = {}
        t = threading.Thread(target=lambda: result.update(rc=relay.run(stdin())),
                             daemon=True)
        t.start()
        return t, result

    @staticmethod
    def frames(out):
        return [json.loads(l) for l in out.getvalue().decode().splitlines()]

    def test_a_stream_that_only_pings_hits_an_overall_deadline(self):
        """sse-starlette pings every 15 s, so a per-read timeout never fires on a wedged
        call; the request needs a monotonic deadline of its own."""
        import io
        import threading
        import time
        import http.server
        stop = threading.Event()

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                try:
                    while not stop.is_set():
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        time.sleep(0.05)
                except OSError:
                    pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            out = io.BytesIO()
            relay = StdioRelay(server.server_address[1], log=lambda *a: None,
                               host="127.0.0.1", out=out, poll_seconds=0)
            relay.report_timing = lambda *a: None
            for name in ("control_deadline_seconds", "tool_deadline_seconds"):
                setattr(relay, name, 0.6)
            t = threading.Thread(target=relay.handle_frame, daemon=True, args=(
                '{"jsonrpc":"2.0","id":9,"method":"initialize","params":{}}',))
            started = time.monotonic()
            t.start()
            t.join(4.0)
            elapsed = time.monotonic() - started
            assert not t.is_alive(), "handle_frame is still blocked on the ping stream"
            assert elapsed < 3.0
            reply = [f for f in self.frames(out) if f.get("id") == 9]
            assert len(reply) == 1 and reply[0]["error"]["code"] == -32001
        finally:
            stop.set()
            server.shutdown()
            server.server_close()

    def test_ping_and_cancel_do_not_queue_behind_wedged_tool_calls(self, monkeypatch):
        import io
        import threading
        from launcher import stdio_relay
        monkeypatch.setattr(stdio_relay, "MAX_WORKERS", 2)
        release = threading.Event()
        out = io.BytesIO()
        relay = make_relay(out=out)
        relay.report_timing = lambda *a: None

        def post(body, timeout=None):
            if b"tools/call" in body:
                release.wait(8)
                return 200, hdrs(Content_Type="application/json"), \
                    '{"jsonrpc":"2.0","id":1,"result":{}}'
            return 200, hdrs(Content_Type="application/json"), \
                '{"jsonrpc":"2.0","id":99,"result":{}}'

        relay.post = post
        lines = ['{"jsonrpc":"2.0","id":%d,"method":"tools/call","params":{}}' % i
                 for i in (1, 2, 3, 4)] + ['{"jsonrpc":"2.0","id":99,"method":"ping"}']
        t, _ = self.run_relay(relay, lines,
                              until=lambda: b'"id":99' in out.getvalue(), hold=3.0)
        answered = b'"id":99' in out.getvalue() or _wait(
            lambda: b'"id":99' in out.getvalue(), 2.0)
        release.set()
        t.join(10)
        assert answered, "ping sat behind the wedged tools/call workers"

    def test_end_of_input_drops_queued_tool_calls(self, monkeypatch):
        import io
        import threading
        from launcher import stdio_relay
        monkeypatch.setattr(stdio_relay, "MAX_WORKERS", 1)
        release = threading.Event()
        started = []
        relay = make_relay(out=io.BytesIO())
        relay.report_timing = lambda *a: None

        def post(body, timeout=None):
            started.append(body)
            release.wait(5)
            return 200, hdrs(Content_Type="application/json"), \
                '{"jsonrpc":"2.0","id":1,"result":{}}'

        relay.post = post
        lines = ['{"jsonrpc":"2.0","id":%d,"method":"tools/call","params":{}}' % i
                 for i in (1, 2, 3)]
        threading.Timer(0.5, release.set).start()
        t, _ = self.run_relay(relay, lines, until=lambda: True)
        t.join(10)
        assert len(started) == 1, "queued tools/call still ran after the client left"

    def test_unparseable_and_batch_frames_are_answered(self):
        import io
        out = io.BytesIO()
        relay = make_relay(out=out)
        relay.report_timing = lambda *a: None
        relay.post = lambda body, timeout=None: (
            200, hdrs(Content_Type="application/json"),
            '{"jsonrpc":"2.0","id":5,"result":{}}')
        lines = ["not json at all",
                 '[{"jsonrpc":"2.0","id":5,"method":"tools/list"},'
                 '{"jsonrpc":"2.0","method":"notifications/x"}]',
                 "[]"]
        t, _ = self.run_relay(relay, lines, until=lambda: len(out.getvalue().splitlines()) >= 3)
        t.join(10)
        got = self.frames(out)
        parse = [f for f in got if f.get("error", {}).get("code") == -32700]
        assert len(parse) == 1 and parse[0]["id"] is None
        assert any(f.get("id") == 5 and "error" in f for f in got), got
        assert any(f.get("id") is None and f.get("error", {}).get("code") == -32600
                   for f in got), got


def _wait(cond, seconds):
    import time
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


class TestRecovery:
    """L2: a dead or moved server is found (or respawned) again, not just reported.

    The spawn hook is `relay.recover`, standing in for launcher.ensure_server (seek, then
    spawn). These tests never start a process.
    """

    @staticmethod
    def refused():
        import urllib.error
        return urllib.error.URLError(ConnectionRefusedError(10061, "refused"))

    @staticmethod
    def reset():
        return ConnectionResetError(10054, "reset by peer")

    def relay_with(self, *script, recover=None):
        """A relay whose post() plays `script`: an Exception is raised, a tuple returned."""
        import io
        out = io.BytesIO()
        relay = make_relay(out=out)
        relay.out_buffer = out
        relay.recovery_backoff_base = 0.0
        relay.recover = recover
        relay.seen_urls = []
        steps = list(script)

        def post(body, timeout=None):
            relay.seen_urls.append(relay.mcp_url)
            step = steps.pop(0)
            if isinstance(step, Exception):
                raise step
            return step

        relay.post = post
        return relay

    @staticmethod
    def frames(relay):
        return [json.loads(l) for l in relay.out_buffer.getvalue().splitlines()]

    @staticmethod
    def ok(msg_id, result=None):
        return (200, hdrs(Content_Type="application/json"),
                json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": result or {}}))

    @pytest.mark.parametrize("method", ["ping", "tools/list"])
    def test_an_idempotent_request_recovers_swaps_the_port_and_retries_once(self, method):
        calls = []
        relay = self.relay_with(self.refused(), self.ok(7),
                                recover=lambda: calls.append(1) or 2833)
        relay.handle_frame(json.dumps({"jsonrpc": "2.0", "id": 7, "method": method}))
        assert calls == [1]
        assert [f["id"] for f in self.frames(relay)] == [7]
        assert "error" not in self.frames(relay)[0]
        assert relay.port == 2833
        assert relay.seen_urls == ["http://127.0.0.1:2830/mcp", "http://127.0.0.1:2833/mcp"]
        assert relay.health_url.endswith(":2833/health")

    def test_a_reset_mid_response_recovers_the_same_way(self):
        relay = self.relay_with(self.reset(), self.ok(3), recover=lambda: 2830)
        relay.handle_frame('{"jsonrpc":"2.0","id":3,"method":"ping"}')
        assert "error" not in self.frames(relay)[0]

    def test_initialize_is_retried_on_the_new_server(self):
        relay = self.relay_with(self.refused(), self.ok(1, {"protocolVersion": "2025-06-18"}),
                                recover=lambda: 2831)
        relay.handle_frame(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                       "params": {}}))
        assert "error" not in self.frames(relay)[0]

    def test_the_retry_happens_once_not_in_a_loop(self):
        calls = []
        relay = self.relay_with(self.refused(), self.refused(),
                                recover=lambda: calls.append(1) or 2831)
        relay.handle_frame('{"jsonrpc":"2.0","id":9,"method":"ping"}')
        assert calls == [1]
        (reply,) = self.frames(relay)
        assert reply["id"] == 9 and reply["error"]["code"] == -32603

    def test_a_tools_call_is_not_replayed_and_says_whether_it_may_have_run(self):
        relay = self.relay_with(self.reset(), recover=lambda: 2831)
        relay.handle_frame(json.dumps({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                                       "params": {"name": "page_act", "arguments": {}}}))
        assert len(relay.seen_urls) == 1          # never re-sent
        (reply,) = self.frames(relay)
        msg = reply["error"]["message"]
        assert reply["id"] == 5
        assert "may have already run" in msg
        assert ":2831" in msg

    def test_a_read_only_tool_call_is_told_it_is_safe_to_repeat(self):
        relay = self.relay_with(self.reset(), recover=lambda: 2831)
        relay.handle_frame(json.dumps({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                                       "params": {"name": "page_read", "arguments": {}}}))
        msg = self.frames(relay)[0]["error"]["message"]
        assert "safe to call again" in msg and "may have already run" not in msg

    def test_a_refused_connection_means_the_call_never_ran(self):
        relay = self.relay_with(self.refused(), recover=lambda: 2831)
        relay.handle_frame(json.dumps({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                                       "params": {"name": "page_act", "arguments": {}}}))
        msg = self.frames(relay)[0]["error"]["message"]
        assert "did not run" in msg

    def test_a_failed_recovery_answers_and_backs_off(self):
        calls = []
        relay = self.relay_with(self.refused(), self.refused(), self.refused(),
                                recover=lambda: calls.append(1))     # returns None
        relay.recovery_backoff_base = 60.0
        for i in (1, 2):
            relay.handle_frame(json.dumps({"jsonrpc": "2.0", "id": i, "method": "ping"}))
        replies = self.frames(relay)
        assert [r["id"] for r in replies] == [1, 2]
        assert all("error" in r for r in replies)
        assert calls == [1], "a second call inside the backoff must not re-run the seek/spawn"

    def test_concurrent_failures_share_one_recovery(self):
        import threading
        calls = []
        gate = threading.Event()

        def recover():
            calls.append(1)
            gate.wait(2)
            return 2831

        relay = self.relay_with(self.refused(), self.refused(), self.ok(1), self.ok(2),
                                recover=recover)
        threads = [threading.Thread(target=relay.handle_frame, args=(json.dumps(
            {"jsonrpc": "2.0", "id": i, "method": "ping"}),)) for i in (1, 2)]
        for t in threads:
            t.start()
        deadline = time.time() + 5
        while not calls and time.time() < deadline:
            time.sleep(0.01)
        time.sleep(0.3)     # let the second thread reach the lock
        gate.set()
        for t in threads:
            t.join(5)
        assert calls == [1]
        assert sorted(f["id"] for f in self.frames(relay)) == [1, 2]
        assert all("error" not in f for f in self.frames(relay))

    def test_without_a_recover_hook_behaviour_is_unchanged(self):
        relay = self.relay_with(self.refused())
        relay.handle_frame('{"jsonrpc":"2.0","id":4,"method":"ping"}')
        assert "could not reach the shared server" in self.frames(relay)[0]["error"]["message"]

    def test_the_health_watch_recovers_instead_of_swallowing_failures(self):
        import io
        relay = make_relay(out=io.BytesIO())
        relay.recovery_backoff_base = 0.0
        recovered = []
        relay.recover = lambda: recovered.append(1) or 2840
        seq = [{"server_pid": 1, "tools_signature": "a"}, OSError("down"), OSError("down"),
               {"server_pid": 2, "tools_signature": "a"}]

        def health():
            if not seq:
                relay._stopping.set()
                raise RuntimeError("exhausted")
            step = seq.pop(0)
            if isinstance(step, Exception):
                raise step
            return step

        relay.health = health
        relay.watch_tools()
        assert recovered == [1]
        assert relay.port == 2840


class TestTimingIsSigned:
    def test_the_report_carries_the_auth_header(self, monkeypatch):
        """S2: /timing demands X-BrowserPlug-Auth; an unsigned report is silently lost."""
        from browserplug_shared import local_auth, request_signing
        monkeypatch.setenv("BROWSERPLUG_WS_TOKEN", "t" * 43)
        monkeypatch.setattr(local_auth, "_master_cache", None)
        relay = make_relay()
        sent = []

        class Opener:
            def open(self, req, timeout=None):
                sent.append(req)
                return type("R", (), {"close": lambda self: None})()

        relay._opener = Opener()
        relay.report_timing("tabs", 1.0, 2.0)
        header = sent[0].get_header(request_signing.AUTH_HEADER.capitalize())
        assert header.startswith("v2 ")
        assert header.split(" ")[4] == request_signing.body_hash(sent[0].data)


def test_initialize_gets_its_error_before_the_client_gives_up():
    """A client abandons `initialize` at about 30 s and kills the launcher with no JSON-RPC
    error, so the control-lane deadline has to fire well inside that budget (L10)."""
    from launcher import stdio_relay
    relay = stdio_relay.StdioRelay(1, log=lambda *a: None, host="127.0.0.1")
    assert relay.control_deadline_seconds == stdio_relay.CONTROL_DEADLINE_SECONDS
    assert 5 <= relay.control_deadline_seconds <= 25


# --- 2026-07-28 through the relay -----------------------------------------------------------

def make_modern_relay(port=2830, era="all", out=None, **kwargs):
    """A relay for `era` with a log, a fixed connection id and a recording timing report."""
    logged, reported = [], []
    kwargs.setdefault("connection_id", "ab" * 16)
    relay = StdioRelay(port, log=lambda *a: logged.append(" ".join(str(x) for x in a)),
                       out=out if out is not None else io.BytesIO(), poll_seconds=0,
                       client_pid=4242, era=era, **kwargs)
    relay.logged, relay.reported = logged, reported
    relay.report_timing = lambda tool, started, ms: reported.append(tool)
    return relay


def relay_era_limit():
    from launcher import relay_era
    return relay_era.MAX_NAME_HEADER


def written(relay):
    """The frames the relay has written to stdout, parsed."""
    return [json.loads(line) for line in relay._out.getvalue().splitlines()]


class Lanes:
    """Executors that keep what they are handed, so a test decides when a worker runs."""

    def __init__(self):
        self.queued = []

    def submit(self, fn, *args):
        self.queued.append((fn, args))

    def run_all(self):
        while self.queued:
            fn, args = self.queued.pop(0)
            fn(*args)


def cancel_frame(request_id, reason="user"):
    return {"jsonrpc": "2.0", "method": "notifications/cancelled",
            "params": {"requestId": request_id, "reason": reason}}


class TestModern:
    """The relay under `mcp.era = all`: what a 2026-07-28 client behind it gets (headers, no
    session, cancel by abort, a locally answered listen) and what a handshake client still
    gets (everything it always got)."""

    # -- the request ----------------------------------------------------------------------

    def test_a_modern_request_carries_the_routing_headers_and_no_session(self, backend):
        server = backend()
        relay = make_modern_relay(server.port)
        # A handshake session exists as well (a client that opened one first): a modern
        # request must still go out without it.
        relay._session_id, relay._protocol_version = "S1", "2025-06-18"
        relay.handle_frame(json.dumps(modern_frame("tools/call", 3, name="server_info",
                                                   arguments={})))
        _, headers, _ = server.requests[0]
        assert headers["mcp-protocol-version"] == VERSION
        assert headers["mcp-method"] == "tools/call" and headers["mcp-name"] == "server_info"
        assert headers["x-browserplug-client-pid"] == "4242"
        assert headers["x-browserplug-connection-id"] == "ab" * 16
        assert "mcp-session-id" not in headers
        assert [f["id"] for f in written(relay)] == [3]
        assert relay.reported == ["server_info"], "the call's timing is still reported"

    def test_a_non_ascii_tool_name_travels_as_the_sentinel(self, backend):
        server = backend()
        relay = make_modern_relay(server.port)
        relay.handle_frame(json.dumps(modern_frame("tools/call", 3, name="café")))
        assert server.requests[0][1]["mcp-name"] == "=?base64?Y2Fmw6k=?="

    @staticmethod
    def never_recovers(relay):
        """A recovery is for a lost server; a request refused locally must not start one."""
        relay.recover = lambda: pytest.fail("a refused request ran the server recovery")

    def test_a_lone_surrogate_in_a_tool_name_is_refused_not_blamed_on_the_server(
            self, backend):
        server = backend()
        relay = make_modern_relay(server.port)
        self.never_recovers(relay)
        raw = json.dumps(modern_frame("tools/call", 3, name="page_read\ud800"))
        assert "\\ud800" in raw, "the frame carries the escape, as a client would send it"
        relay.handle_frame(raw)
        assert server.requests == []
        (reply,) = written(relay)
        assert reply["id"] == 3 and reply["error"]["code"] == -32602
        message = reply["error"]["message"]
        assert "unpaired surrogate" in message and "request was not sent" in message
        assert "could not reach" not in message and "utf-8" not in message
        assert relay.reported == []

    @pytest.mark.parametrize("size", [8193, 70000])
    def test_an_oversized_name_is_refused_with_no_connection_made(self, backend, size):
        """Past ~64 KB the server resets the connection without a word, which the relay would
        take for a lost server and answer "it may have already run". The call never ran."""
        server = backend()
        relay = make_modern_relay(server.port)
        self.never_recovers(relay)
        relay.handle_frame(json.dumps(modern_frame("tools/call", 4, name="n" * size)))
        assert server.requests == []
        (reply,) = written(relay)
        assert reply["id"] == 4 and reply["error"]["code"] == -32602
        assert "too long" in reply["error"]["message"]
        assert "may have already run" not in reply["error"]["message"]

    def test_a_name_at_the_limit_still_goes_to_the_server(self, backend):
        server = backend()
        relay = make_modern_relay(server.port)
        name = "n" * relay_era_limit()
        relay.handle_frame(json.dumps(modern_frame("tools/call", 4, name=name)))
        assert server.requests[0][1]["mcp-name"] == name
        assert [f["id"] for f in written(relay)] == [4]

    def test_a_refused_notification_is_dropped_without_a_reply(self, backend):
        server = backend()
        relay = make_modern_relay(server.port)
        frame = modern_frame("tools/call", None, name="n" * 9000)
        relay.handle_frame(json.dumps(frame))
        assert server.requests == [] and written(relay) == []
        (line,) = [line for line in relay.logged if "not sent" in line]
        # The refusal's own text already names the method; the log adds nothing before it.
        assert line.startswith("tools/call: params.name is too long to send"), line
        assert "tools/call: tools/call" not in line

    def test_the_connection_id_is_minted_once_and_is_one_the_server_accepts(self):
        from browserplug_shared import mcp_era
        first, second = make_modern_relay(connection_id=None), make_modern_relay(connection_id=None)
        for relay in (first, second):
            assert mcp_era.is_connection_id(relay.connection_id)
        assert first.connection_id != second.connection_id

    def test_a_handshake_request_in_the_same_relay_keeps_its_session_and_gets_no_new_header(
            self, backend):
        server = backend()
        relay = make_modern_relay(server.port)
        relay._session_id, relay._protocol_version = "S1", "2025-06-18"
        relay.handle_frame('{"jsonrpc":"2.0","id":2,"method":"tools/list"}')
        _, headers, _ = server.requests[0]
        assert headers["mcp-session-id"] == "S1"
        assert headers["mcp-protocol-version"] == "2025-06-18"
        assert "x-browserplug-connection-id" not in headers and "mcp-method" not in headers

    @pytest.mark.parametrize("era", [None, "handshake", "direct"])
    def test_without_era_all_an_enveloped_frame_takes_the_handshake_path(self, era):
        """The relay is then what it was: the server's gate answers the frame and a probing
        client falls back to initialize."""
        relay = make_modern_relay(era=era)
        relay.post_modern = lambda *a, **k: pytest.fail("a modern post under era " + str(era))
        relay.post = FakeBackend(relay, (400, hdrs(Content_Type="application/json"), json.dumps(
            {"jsonrpc": "2.0", "id": 4, "error": {"code": -32022, "message": "Unsupported"}})))
        relay.handle_frame(json.dumps(modern_frame("server/discover", 4)))
        assert len(relay.post.calls) == 1
        assert written(relay)[0]["error"]["code"] == -32022

    def test_a_modern_404_is_a_missing_method_and_never_a_lost_session(self, backend):
        """The handshake path reads every 404 as "session gone" and rebuilds: an initialize
        replay, a new session on the server. A modern 404 is METHOD_NOT_FOUND (the era has no
        ping), and the client is told so."""
        error = {"jsonrpc": "2.0", "id": 8, "error": {"code": -32601, "message": "Method not found"}}
        server = backend(lambda message, headers, handler: (404, error, None))
        relay = make_modern_relay(server.port)
        relay._session_id, relay._initialize_frame = "S1", b'{"method":"initialize"}'
        generation = relay._session_gen
        relay.handle_frame(json.dumps(modern_frame("ping", 8)))
        assert len(server.requests) == 1, "a rebuild would have sent an initialize"
        assert relay._session_id == "S1" and relay._session_gen == generation
        assert written(relay) == [error]

    def test_a_refusal_naming_the_handshake_versions_reaches_the_client_whole(self, backend):
        """A server on `handshake` refuses the modern header; the SDK client then falls back
        to initialize, which is the version-skew case (relay all, server handshake)."""
        refusal = {"jsonrpc": "2.0", "id": 4, "error": {
            "code": -32022, "message": "Unsupported protocol version",
            "data": {"supported": ["2025-11-25"], "requested": VERSION}}}
        server = backend(lambda message, headers, handler: (400, refusal, None))
        relay = make_modern_relay(server.port)
        relay.handle_frame(json.dumps(modern_frame("server/discover", 4)))
        assert written(relay) == [refusal]
        assert relay._session_id is None
        assert any("does not serve 2026-07-28" in line for line in relay.logged), relay.logged

    def test_a_slow_modern_call_is_bounded_by_the_deadline_like_any_other(self, backend):
        """The server pings a quiet stream, so only a deadline that cuts the connection ends it."""
        def pinging(message, headers, handler):
            handler.send_response(200)
            handler.send_header("Content-Type", "text/event-stream")
            handler.end_headers()
            end = time.monotonic() + 15
            try:
                while time.monotonic() < end and not server.release.is_set():
                    handler.wfile.write(b": ping\n\n")
                    handler.wfile.flush()
                    time.sleep(0.05)
            except OSError:
                pass
            return 200, "", None

        server = backend(pinging)
        relay = make_modern_relay(server.port)
        relay.tool_deadline_seconds = 0.6
        started = time.monotonic()
        relay.handle_frame(json.dumps(modern_frame("tools/call", 9, name="page_read")))
        assert time.monotonic() - started < 4
        reply = written(relay)
        assert len(reply) == 1 and reply[0]["id"] == 9 and reply[0]["error"]["code"] == -32001

    # -- recovery -------------------------------------------------------------------------

    @staticmethod
    def scripted(relay, *script):
        """post_modern plays `script`: an Exception is raised, a tuple returned."""
        steps = list(script)
        relay.modern_calls = []

        def post_modern(message, version, body, exchange):
            relay.modern_calls.append(message["method"])
            step = steps.pop(0)
            if isinstance(step, Exception):
                raise step
            return step

        relay.post_modern = post_modern

    @staticmethod
    def ok(msg_id):
        return (200, hdrs(Content_Type="application/json"),
                json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": {}}))

    @pytest.mark.parametrize("method", ["server/discover", "tools/list",
                                        "resources/templates/list"])
    def test_a_read_only_request_is_replayed_once_after_the_server_comes_back(self, method):
        relay = make_modern_relay()
        relay.recovery_backoff_base = 0.0
        recovered = []
        relay.recover = lambda: recovered.append(1) or 2833
        self.scripted(relay, ConnectionResetError(10054, "reset"), self.ok(5))
        relay.handle_frame(json.dumps(modern_frame(method, 5)))
        assert recovered == [1] and relay.modern_calls == [method, method]
        assert [f["id"] for f in written(relay)] == [5] and "error" not in written(relay)[0]
        assert relay.port == 2833

    def test_a_modern_tool_call_is_not_replayed_and_says_what_is_known(self):
        relay = make_modern_relay()
        relay.recovery_backoff_base = 0.0
        relay.recover = lambda: 2833
        self.scripted(relay, ConnectionResetError(10054, "reset"))
        relay.handle_frame(json.dumps(modern_frame("tools/call", 6, name="page_act")))
        assert relay.modern_calls == ["tools/call"], "it may already have run"
        error = written(relay)[0]["error"]["message"]
        assert "was not re-sent" in error and "may have already run" in error

    def test_replay_after_recovery_needs_no_session_rebuild(self):
        """The handshake path replays the client's initialize on the new server; a modern
        request is self-contained, so nothing but the request itself is sent."""
        relay = make_modern_relay()
        relay.recovery_backoff_base = 0.0
        relay.recover = lambda: 2833
        relay._initialize_frame = b'{"jsonrpc":"2.0","id":1,"method":"initialize"}'
        relay.post = FakeBackend(relay)      # must never be called
        self.scripted(relay, ConnectionResetError(10054, "reset"), self.ok(7))
        relay.handle_frame(json.dumps(modern_frame("tools/list", 7)))
        assert relay.post.calls == [] and relay.modern_calls == ["tools/list", "tools/list"]

    # -- cancel ---------------------------------------------------------------------------

    @staticmethod
    def slow_tool_backend(backend):
        def respond(message, headers, handler):
            if message.get("method") == "tools/call":
                server.hold(handler)
            return Backend.ok(message, headers, handler)

        server = backend(respond)
        return server

    @staticmethod
    def run_script(relay, script):
        """relay.run() on a stdin that yields what `script` yields, one frame per line, and
        may wait between them. The thread ends when the script does."""
        def stdin():
            for frame in script():
                yield (json.dumps(frame) + "\n").encode()

        thread = threading.Thread(target=relay.run, args=(stdin(),), daemon=True)
        thread.start()
        return thread

    def test_a_cancel_ends_the_request_and_nothing_is_written_for_it(self, backend):
        server = self.slow_tool_backend(backend)
        relay = make_modern_relay(server.port)
        seen = {}

        def script():
            yield modern_frame("tools/call", 5, name="slow", arguments={})
            seen["started"] = server.started.wait(5)
            yield cancel_frame(5)
            seen["disconnected"] = server.disconnected.wait(5)
            yield modern_frame("tools/list", 6)
            end = time.monotonic() + 5
            while time.monotonic() < end and not any(f.get("id") == 6 for f in written(relay)):
                time.sleep(0.02)

        thread = self.run_script(relay, script)
        thread.join(20)
        assert not thread.is_alive()
        assert seen == {"started": True, "disconnected": True}, "the server never saw the abort"
        assert [f["id"] for f in written(relay)] == [6], "no reply, not even an error, for 5"
        methods = [json.loads(body)["method"] for _, _, body in server.requests]
        assert methods == ["tools/call", "tools/list"], "the cancel itself is never POSTed"
        assert relay.reported == [], "a cancelled call reports no timing"
        assert any("cancelled" in line for line in relay.logged), relay.logged

    def test_a_cancel_that_lands_as_the_answer_arrives_sends_no_reply(self):
        """The abort can come after the response was read and before it is written. The
        client withdrew the request, so nothing is written for it."""
        relay = make_modern_relay()

        def answers_while_the_cancel_arrives(message, version, body, exchange):
            exchange.abort()
            return self.ok(5)

        relay.post_modern = answers_while_the_cancel_arrives
        relay.handle_frame(json.dumps(modern_frame("tools/list", 5)))
        assert written(relay) == []

    def test_a_cancel_that_overtakes_its_worker_stops_the_request_before_it_is_sent(
            self, backend):
        """The relay takes frames from stdin in order but a worker runs the request later.
        The cancel is applied on the reader's thread, so it cannot miss a request that was
        read before it, whatever the workers are doing."""
        server = backend()
        relay = make_modern_relay(server.port)
        pool, control = Lanes(), Lanes()
        relay.dispatch(json.dumps(modern_frame("tools/call", 5, name="slow")), pool, control)
        relay.dispatch(json.dumps(cancel_frame(5)), pool, control)
        assert control.queued == [], "the cancel is settled on the reader's thread"
        pool.run_all()
        assert server.requests == [] and written(relay) == []

    def test_a_cancel_naming_the_id_as_text_still_finds_the_request(self, backend):
        server = backend()
        relay = make_modern_relay(server.port)
        pool, control = Lanes(), Lanes()
        relay.dispatch(json.dumps(modern_frame("tools/call", 12, name="slow")), pool, control)
        relay.dispatch(json.dumps(cancel_frame("12")), pool, control)
        pool.run_all()
        assert server.requests == [] and written(relay) == []

    def test_a_cancel_for_a_request_that_already_answered_is_dropped(self, backend):
        server = backend()
        relay = make_modern_relay(server.port)
        pool, control = Lanes(), Lanes()
        relay.dispatch(json.dumps(modern_frame("tools/list", 5)), pool, control)
        control.run_all()
        relay.dispatch(json.dumps(cancel_frame(5)), pool, control)
        control.run_all()
        assert len(server.requests) == 1, "the late cancel was not POSTed"
        assert [f["id"] for f in written(relay)] == [5]

    def test_a_cancel_for_a_handshake_call_is_still_forwarded(self):
        relay = make_modern_relay()
        relay._initialize_frame = b'{"jsonrpc":"2.0","id":1,"method":"initialize"}'
        relay._session_id = "S1"
        relay.post = FakeBackend(relay, (202, hdrs(Content_Type="application/json"), ""))
        pool, control = Lanes(), Lanes()
        relay.dispatch(json.dumps(cancel_frame(3)), pool, control)
        control.run_all()
        assert len(relay.post.calls) == 1
        assert json.loads(relay.post.calls[0]["body"])["method"] == "notifications/cancelled"

    def test_under_other_eras_a_cancel_is_forwarded_as_it_always_was(self):
        relay = make_modern_relay(era="direct")
        relay.post = FakeBackend(relay, (202, hdrs(Content_Type="application/json"), ""))
        pool, control = Lanes(), Lanes()
        relay.dispatch(json.dumps(cancel_frame(3)), pool, control)
        control.run_all()
        assert len(relay.post.calls) == 1

    # -- listen ---------------------------------------------------------------------------

    @staticmethod
    def watch(relay, health):
        """Drive watch_tools over a scripted /health sequence, then stop it."""
        seq = list(health)

        def next_health():
            if not seq:
                relay._stopping.set()
                raise RuntimeError("scripted sequence exhausted")
            return seq.pop(0)

        relay.health = next_health
        relay.watch_tools()

    @staticmethod
    def listen_line(listen_id="listen-1", **wanted):
        return json.dumps(modern_frame("subscriptions/listen", listen_id,
                                       notifications=wanted or {"toolsListChanged": True}))

    def test_listen_is_answered_by_the_relay_and_never_reaches_the_server(self, backend):
        server = backend()
        relay = make_modern_relay(server.port)
        pool, control = Lanes(), Lanes()
        relay.dispatch(self.listen_line("listen-1"), pool, control)
        assert pool.queued == [] and control.queued == [], "no worker is held by a stream"
        assert server.requests == []
        ack = written(relay)
        assert [f["method"] for f in ack] == ["notifications/subscriptions/acknowledged"]
        assert ack[0]["params"]["_meta"] == {"io.modelcontextprotocol/subscriptionId": "listen-1"}

    def test_a_change_in_the_servers_surface_is_stamped_onto_the_stream(self):
        relay = make_modern_relay()
        relay.dispatch(self.listen_line("listen-1"), Lanes(), Lanes())
        self.watch(relay, [{"server_pid": 1, "tools_signature": "a"},
                           {"server_pid": 1, "tools_signature": "a"},       # no change: silent
                           {"server_pid": 1, "tools_signature": "b"},       # the surface moved
                           {"server_pid": 2, "tools_signature": "b"}])      # a restart
        frames = written(relay)
        assert [f["method"] for f in frames] == (
            ["notifications/subscriptions/acknowledged"] + ["notifications/tools/list_changed"] * 2)
        for notice in frames[1:]:
            assert notice["params"] == {"_meta": {"io.modelcontextprotocol/subscriptionId":
                                                  "listen-1"}}
            assert "id" not in notice
        assert any("told 1 listen stream" in line for line in relay.logged), relay.logged

    def test_the_first_observation_is_a_baseline_and_tells_the_stream_nothing(self):
        relay = make_modern_relay()
        relay.dispatch(self.listen_line(), Lanes(), Lanes())
        self.watch(relay, [{"server_pid": 1, "tools_signature": "a"}])
        assert [f["method"] for f in written(relay)] == [
            "notifications/subscriptions/acknowledged"]

    def test_a_client_that_never_asked_is_sent_no_unsolicited_notice(self):
        """2026-07-28 forbids a change notification nobody requested, and a client that has
        opened no session and no stream asked for nothing."""
        relay = make_modern_relay()
        self.watch(relay, [{"server_pid": 1, "tools_signature": "a"},
                           {"server_pid": 2, "tools_signature": "b"}])
        assert written(relay) == []

    def test_a_handshake_client_still_gets_the_unsolicited_notice(self):
        relay = make_modern_relay()
        relay._initialize_frame = b'{"jsonrpc":"2.0","id":1,"method":"initialize"}'
        self.watch(relay, [{"server_pid": 1, "tools_signature": "a"},
                           {"server_pid": 2, "tools_signature": "a"}])
        assert relay._out.getvalue() == (
            b'{"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}\n')

    def test_a_client_with_both_is_told_on_each(self):
        relay = make_modern_relay()
        relay._initialize_frame = b'{"jsonrpc":"2.0","id":1,"method":"initialize"}'
        relay.dispatch(self.listen_line("listen-1"), Lanes(), Lanes())
        self.watch(relay, [{"server_pid": 1, "tools_signature": "a"},
                           {"server_pid": 1, "tools_signature": "b"}])
        stamps = [f.get("params", {}).get("_meta", {}).get(
            "io.modelcontextprotocol/subscriptionId", "none") for f in written(relay)[1:]]
        assert sorted(stamps) == ["listen-1", "none"], "one stamped, one unsolicited"

    def test_a_stream_that_asked_for_something_else_is_not_told_of_tools(self):
        relay = make_modern_relay()
        relay.dispatch(self.listen_line("p", promptsListChanged=True), Lanes(), Lanes())
        self.watch(relay, [{"server_pid": 1, "tools_signature": "a"},
                           {"server_pid": 2, "tools_signature": "a"}])
        assert [f["method"] for f in written(relay)] == [
            "notifications/subscriptions/acknowledged"]

    def test_a_cancel_closes_the_stream_and_it_hears_no_more(self):
        relay = make_modern_relay()
        pool, control = Lanes(), Lanes()
        relay.dispatch(self.listen_line("listen-1"), pool, control)
        relay.dispatch(json.dumps(cancel_frame("listen-1")), pool, control)
        assert control.queued == [] and pool.queued == []
        self.watch(relay, [{"server_pid": 1, "tools_signature": "a"},
                           {"server_pid": 2, "tools_signature": "a"}])
        assert [f["method"] for f in written(relay)] == [
            "notifications/subscriptions/acknowledged"], "no reply to the cancel, no notice"

    def test_under_other_eras_the_notice_is_unsolicited_as_it_always_was(self):
        for era in (None, "handshake", "direct"):
            relay = make_modern_relay(era=era)
            self.watch(relay, [{"server_pid": 1, "tools_signature": "a"},
                               {"server_pid": 2, "tools_signature": "a"}])
            assert relay._out.getvalue() == (
                b'{"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}\n'), era

    def test_a_listen_under_another_era_is_not_answered_by_the_relay(self):
        relay = make_modern_relay(era="direct")
        relay.post = FakeBackend(relay, (404, hdrs(Content_Type="application/json"), "{}"))
        pool, control = Lanes(), Lanes()
        relay.dispatch(self.listen_line("listen-1"), pool, control)
        assert written(relay) == [] and len(control.queued) == 1

    # -- the reader -----------------------------------------------------------------------

    @pytest.mark.parametrize("hook", ["intercept", "enter"])
    def test_an_error_while_settling_a_frame_locally_does_not_end_the_session(
            self, backend, hook):
        """The reader is the client's whole session: a bug in the carriage's local handling
        must cost that frame its fast path, not every frame after it."""
        server = backend()
        relay = make_modern_relay(server.port)
        real = getattr(relay.carriage, hook)
        calls = []

        def fails_once(message):
            calls.append(message.get("id"))
            if len(calls) == 1:
                raise RuntimeError("carriage bug")
            return real(message)

        setattr(relay.carriage, hook, fails_once)

        def script():
            yield modern_frame("tools/list", 1)
            yield modern_frame("tools/list", 2)
            end = time.monotonic() + 5
            while time.monotonic() < end and len(written(relay)) < 2:
                time.sleep(0.02)

        thread = self.run_script(relay, script)
        thread.join(20)
        assert not thread.is_alive()
        assert sorted(f["id"] for f in written(relay)) == [1, 2], "the frame after it is served"
        assert any("could not settle a frame locally" in line for line in relay.logged)

    # -- plumbing -------------------------------------------------------------------------

    def test_end_of_input_leaves_no_request_behind(self, backend):
        server = backend()
        relay = make_modern_relay(server.port)
        def script():
            yield modern_frame("tools/list", 1)
            end = time.monotonic() + 5
            while time.monotonic() < end and not written(relay):
                time.sleep(0.02)

        thread = self.run_script(relay, script)
        thread.join(20)
        assert not thread.is_alive()
        assert [f["id"] for f in written(relay)] == [1]
        assert relay.carriage.exchanges._by_id == {}

    def test_run_relay_passes_the_era_to_the_relay(self, monkeypatch):
        from launcher import stdio_relay
        monkeypatch.setattr(stdio_relay.StdioRelay, "run", lambda self, stdin=None: self)
        for era, carries in (("all", True), ("direct", False), ("handshake", False), (None, False)):
            relay = stdio_relay.run_relay(2830, log=lambda *a: None, era=era)
            assert relay.era == era and relay.carriage.enabled is carries, era


class TestModernSetting:
    """launcher.relay_era_setting(): the era the launcher hands its relay."""

    @pytest.fixture(autouse=True)
    def isolated(self, monkeypatch, tmp_path):
        from browserplug_shared import config_store, mcp_era
        monkeypatch.setenv(config_store.STATE_DIR_ENV, str(tmp_path))
        monkeypatch.delenv(mcp_era.ERA_ENV, raising=False)

    def test_the_default_is_all(self, capsys):
        assert launcher.relay_era_setting() == "all"
        assert "MCP era all (default)" in capsys.readouterr().err

    @pytest.mark.parametrize("value", ["handshake", "direct", "all"])
    def test_the_environment_decides(self, monkeypatch, value, capsys):
        monkeypatch.setenv("BROWSERPLUG_MCP_ERA", value)
        assert launcher.relay_era_setting() == value
        assert f"MCP era {value} (env)" in capsys.readouterr().err

    def test_config_json_decides_when_the_environment_is_silent(self, capsys):
        from browserplug_shared import config_store
        config_store.write_section("mcp", {"era": "direct"})
        assert launcher.relay_era_setting() == "direct"
        assert "MCP era direct (config)" in capsys.readouterr().err

    def test_a_value_that_is_not_an_era_is_named_and_skipped(self, monkeypatch, capsys):
        monkeypatch.setenv("BROWSERPLUG_MCP_ERA", "modern")
        assert launcher.relay_era_setting() == "all"
        err = capsys.readouterr().err
        assert "ignoring BROWSERPLUG_MCP_ERA=modern" in err and "handshake, direct, all" in err

    def test_a_setting_that_cannot_be_read_leaves_the_relay_on_the_handshake_era(
            self, monkeypatch, capsys):
        from browserplug_shared import mcp_era

        def broken(*a, **k):
            raise OSError("state directory unreadable")

        monkeypatch.setattr(mcp_era, "resolve", broken)
        assert launcher.relay_era_setting() == "handshake"
        assert "could not read the MCP era setting" in capsys.readouterr().err

    def test_serve_via_relay_hands_the_era_to_the_relay(self, monkeypatch):
        import launcher.stdio_relay as sr
        seen = {}
        monkeypatch.setattr(sr, "run_relay", lambda port, log, **kw: seen.update(kw))
        monkeypatch.setenv("BROWSERPLUG_MCP_ERA", "direct")
        launcher.serve_via_relay(2831)
        assert seen["era"] == "direct"
