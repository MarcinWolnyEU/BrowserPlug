"""launcher/relay_era.py: how the stdio relay carries MCP 2026-07-28.

The relay gives a stdio client no HTTP headers of its own, and the server routes a modern
request on them, so what the relay synthesizes has to be exact: after connect, a header that
disagrees with the body is a hard error in Claude Code, not a fallback. These tests hold the
pieces to the SDK's own rules (mcp.shared.inbound) rather than to a second reading of them:

  * the headers are fed to the SDK's validation ladder, and the Mcp-Name sentinel is
    compared with the SDK's encoder;
  * the listen frames are compared with the SDK's wire models;
  * a request that is cancelled, or runs past its deadline, is cut at the socket, which is
    only visible against a real HTTP server, so the fake backend here is one.

The relay-level behaviour (what StdioRelay does with all of this) is test_stdio_relay.py's
TestModern, and the whole path through the real launcher is test_e2e_modern_relay.py.
"""

import gc
import http.client
import io
import json
import select
import socket
import subprocess
import sys
import textwrap
import threading
import time
import urllib.request
import weakref
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from mcp.shared.inbound import (InboundModernRoute, classify_inbound_request,
                                decode_header_value)
from mcp.shared.inbound import encode_header_value as sdk_encode_header_value
from mcp.shared.subscriptions import SUBSCRIPTION_ID_META_KEY, event_to_notification
from mcp.shared.subscriptions import ToolsListChanged
from mcp_types import (SubscriptionFilter, SubscriptionsAcknowledgedNotification,
                       SubscriptionsAcknowledgedNotificationParams)

from browserplug_shared import mcp_era
from browserplug_shared.version import CLIENT_PID_HEADER
from launcher import relay_era
from launcher.stdio_relay import StdioRelay

VERSION = "2026-07-28"
CAPABILITIES_KEY = "io.modelcontextprotocol/clientCapabilities"
_REPO_ROOT = str(__import__("pathlib").Path(relay_era.__file__).resolve().parent.parent)
_SHARED_PY = _REPO_ROOT + "/shared-py"


def modern_frame(method, msg_id=1, **params):
    """A 2026-07-28 frame as an SDK client writes it: the envelope in params._meta."""
    meta = {relay_era.ENVELOPE_VERSION_KEY: VERSION, CAPABILITIES_KEY: {}}
    frame = {"jsonrpc": "2.0", "method": method, "params": {"_meta": meta, **params}}
    if msg_id is not None:
        frame["id"] = msg_id
    return frame


def lowercase(headers):
    return {name.lower(): value for name, value in headers.items()}


# --- a fake /mcp endpoint on a real socket ---------------------------------------------

class Backend:
    """A loopback HTTP server that records every POST and answers from `respond`.

    `respond(message, headers, handler)` returns (status, body, extra headers); `body` is a
    dict (sent as JSON) or text. `hold(handler)` is for a handler that must stay in flight:
    it sets `started`, then waits until the test sets `release` or the client's end of the
    connection goes away, which sets `disconnected`.
    """

    def __init__(self, respond=None):
        self.requests = []      # (path, {lowercase header: value}, body text)
        self.started = threading.Event()
        self.release = threading.Event()
        self.disconnected = threading.Event()
        self.respond = respond or self.ok
        backend = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"])).decode("utf-8")
                headers = lowercase(dict(self.headers.items()))
                backend.requests.append((self.path, headers, body))
                status, payload, extra = backend.respond(json.loads(body), headers, self)
                data = (json.dumps(payload) if isinstance(payload, dict) else payload).encode()
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    for name, value in (extra or {}).items():
                        self.send_header(name, value)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except OSError:
                    backend.disconnected.set()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @staticmethod
    def ok(message, headers, handler):
        return 200, {"jsonrpc": "2.0", "id": message.get("id"), "result": {}}, None

    def hold(self, handler, seconds=15.0):
        """Stay in flight until released, or until the client hangs up (then say so)."""
        self.started.set()
        end = time.monotonic() + seconds
        while time.monotonic() < end and not self.release.is_set():
            ready, _, _ = select.select([handler.connection], [], [], 0.05)
            if ready:
                try:
                    gone = handler.connection.recv(1, socket.MSG_PEEK) == b""
                except OSError:
                    gone = True
                if gone:
                    self.disconnected.set()
                    return

    def close(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def backend():
    made = []

    def make(respond=None):
        made.append(Backend(respond))
        return made[-1]

    yield make
    for b in made:
        b.close()


def post_in_thread(backend_, exchange, deadline_seconds=10.0, body=b"{}", headers=None):
    """relay_era.post on its own thread. Returns (thread, result), where result gets
    `value` or `error` when the post ends."""
    result = {}

    def run():
        try:
            result["value"] = relay_era.post(
                "127.0.0.1", backend_.port, body, headers or {"Content-Type": "application/json"},
                exchange, deadline_seconds, time.monotonic() + deadline_seconds)
        except Exception as e:
            result["error"] = e

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, result


# --- which frames are modern ---------------------------------------------------------------

class TestClassification:
    def test_an_enveloped_frame_is_modern_and_names_its_version(self):
        assert relay_era.modern_version(modern_frame("tools/list")) == VERSION
        assert relay_era.modern_version(modern_frame("notifications/x", msg_id=None)) == VERSION

    @pytest.mark.parametrize("frame", [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"_meta": {}}},
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"_meta": {"progressToken": 5}}},
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"_meta": "x"}},
        {"jsonrpc": "2.0", "id": 1, "result": {}},
    ], ids=["no params", "empty params", "empty meta", "progress token only", "meta not a map",
            "a response"])
    def test_a_frame_without_the_envelope_is_not(self, frame):
        assert relay_era.modern_version(frame) is None

    def test_initialize_is_never_modern(self):
        """The SDK serves a request that carries the envelope on the modern path unless it
        is `initialize`, and initialize on a modern connection is -32022."""
        assert relay_era.modern_version(modern_frame("initialize")) is None

    @pytest.mark.parametrize("version", [20260728, None, "", " 2026-07-28", "2026-07-28 ",
                                         "2026-07-28\n", "2026-07-ä", ["2026-07-28"]])
    def test_a_version_that_cannot_travel_as_a_header_is_not(self, version):
        """The server compares the header with the envelope for equality, so a version it
        could never match is left for the server's gate to answer."""
        frame = modern_frame("tools/list")
        frame["params"]["_meta"][relay_era.ENVELOPE_VERSION_KEY] = version
        assert relay_era.modern_version(frame) is None

    @pytest.mark.parametrize("method", ["tools/lïst", "tools/list\r\nX: y", "", None, 7])
    def test_a_method_that_cannot_travel_as_a_header_is_not(self, method):
        frame = modern_frame("tools/list")
        frame["method"] = method
        assert relay_era.modern_version(frame) is None


class TestRequestIds:
    @pytest.mark.parametrize("a, b", [(7, "7"), ("7", 7), ("abc", "abc"), (7, 7)])
    def test_a_numeric_string_and_the_number_are_one_id(self, a, b):
        """As in the SDK's coerce_request_id: a cancel that echoes an id as text must find it."""
        assert relay_era.request_id_key(a) == relay_era.request_id_key(b)

    @pytest.mark.parametrize("value", [True, None, [1], {"a": 1}])
    def test_what_is_not_an_id_cannot_be_tracked(self, value):
        assert relay_era.request_id_key(value) is None

    def test_a_connection_id_is_what_the_server_accepts(self):
        ids = {relay_era.new_connection_id() for _ in range(50)}
        assert len(ids) == 50, "one id per relay: they must not repeat"
        assert all(mcp_era.is_connection_id(i) for i in ids)


# --- the headers ---------------------------------------------------------------------------

class TestHeaderValue:
    @pytest.mark.parametrize("value", [
        "server_info", "", "a b", " leading", "trailing ", "café", "日本語", "tab\there",
        "line\nbreak", "=?base64?AAAA?=", "x" * 300, "emoji \U0001f642", "\x7f", "a\x00b"])
    def test_it_encodes_exactly_as_the_sdk_does(self, value):
        encoded = relay_era.encode_header_value(value)
        assert encoded == sdk_encode_header_value(value)
        assert decode_header_value(encoded) == value, "the server must recover the exact value"

    def test_plain_text_goes_through_unchanged_and_the_rest_is_ascii(self):
        assert relay_era.encode_header_value("page_read") == "page_read"
        assert relay_era.encode_header_value("café").isascii()


class TestRequestHeaders:
    @staticmethod
    def headers_for(frame, pid=4242, connection_id="ab" * 16):
        version = relay_era.modern_version(frame)
        return relay_era.request_headers(frame, version, pid, connection_id)

    def test_a_tool_call_carries_the_routing_headers_and_who_it_is(self):
        frame = modern_frame("tools/call", 3, name="server_info", arguments={})
        headers = self.headers_for(frame, pid=777, connection_id="cd" * 16)
        assert headers == {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            CLIENT_PID_HEADER: "777",
            "X-BrowserPlug-Connection-Id": "cd" * 16,
            "MCP-Protocol-Version": VERSION,
            "Mcp-Method": "tools/call",
            "Mcp-Name": "server_info",
        }

    def test_no_session_header_is_ever_sent(self):
        for frame in (modern_frame("tools/list"), modern_frame("tools/call", name="x"),
                      modern_frame("server/discover")):
            assert "mcp-session-id" not in lowercase(self.headers_for(frame))

    def test_the_connection_id_header_is_the_one_the_server_reads(self):
        assert relay_era.CONNECTION_ID_HEADER == mcp_era.CONNECTION_ID_HEADER

    @pytest.mark.parametrize("method, params", [
        ("tools/call", {"name": "navigate"}),
        ("tools/call", {"name": "naïve outil", "arguments": {}}),
        ("tools/call", {"name": " padded "}),
        ("prompts/get", {"name": "a-prompt"}),
        ("resources/read", {"uri": "file:///C:/dir with space/é.txt"}),
        ("tools/list", {}),
        ("server/discover", {}),
        ("resources/templates/list", {}),
    ])
    def test_the_sdks_validation_ladder_accepts_what_the_relay_sends(self, method, params):
        """The SDK's own check of header against body (classify_inbound_request): the
        version, the method and, for a name-bearing method, the name, sentinel and all."""
        frame = modern_frame(method, 9, **params)
        headers = lowercase(self.headers_for(frame))
        route = classify_inbound_request(frame, headers=headers)
        assert isinstance(route, InboundModernRoute), route
        assert route.protocol_version == VERSION

    def test_the_ladder_does_reject_a_missing_name_so_the_test_above_can_fail(self):
        frame = modern_frame("tools/call", 9, name="server_info")
        headers = lowercase(self.headers_for(frame))
        del headers["mcp-name"]
        assert not isinstance(classify_inbound_request(frame, headers=headers),
                              InboundModernRoute)

    def test_a_non_ascii_name_travels_as_the_sentinel_and_every_header_is_ascii(self):
        headers = self.headers_for(modern_frame("tools/call", name="café"))
        assert headers["Mcp-Name"] == sdk_encode_header_value("café")
        assert headers["Mcp-Name"].startswith("=?base64?")
        assert all(v.isascii() for v in headers.values())

    def test_no_name_is_sent_for_a_method_that_does_not_name_one(self):
        assert "Mcp-Name" not in self.headers_for(modern_frame("tools/list"))
        # nor when the body names nothing the header could mirror
        assert "Mcp-Name" not in self.headers_for(modern_frame("tools/call"))
        assert "Mcp-Name" not in self.headers_for(modern_frame("tools/call", name=7))

    @pytest.mark.parametrize("method, key", [("tools/call", "name"), ("prompts/get", "name"),
                                             ("resources/read", "uri")])
    def test_a_lone_surrogate_is_refused_with_a_text_that_says_so(self, method, key):
        """JSON can carry "\\ud800"; it has no UTF-8 form, so it has no header form either.
        The refusal must name the cause, not surface as a UnicodeEncodeError that the relay
        reports as an unreachable server."""
        frame = modern_frame(method, 9, **{key: "tool\ud800"})
        with pytest.raises(relay_era.HeaderRefused) as raised:
            self.headers_for(frame)
        assert "unpaired surrogate" in str(raised.value) and key in str(raised.value)
        assert "request was not sent" in str(raised.value)

    def test_a_name_at_the_limit_is_sent_and_one_byte_over_is_refused(self):
        limit = relay_era.MAX_NAME_HEADER
        headers = self.headers_for(modern_frame("tools/call", name="n" * limit))
        assert len(headers["Mcp-Name"]) == limit
        with pytest.raises(relay_era.HeaderRefused) as raised:
            self.headers_for(modern_frame("tools/call", name="n" * (limit + 1)))
        assert str(limit) in str(raised.value) and "too long" in str(raised.value)

    def test_the_limit_counts_the_encoded_value(self):
        """Non-ASCII text grows by a third under base64, and it is the header that counts."""
        name = "\u00e9" * (relay_era.MAX_NAME_HEADER // 2)
        assert len(name) < relay_era.MAX_NAME_HEADER
        with pytest.raises(relay_era.HeaderRefused):
            self.headers_for(modern_frame("tools/call", name=name))


# --- exchanges -------------------------------------------------------------------------------

class FakeSocket:
    def __init__(self):
        self.shutdowns = []

    def shutdown(self, how):
        self.shutdowns.append(how)


class TestExchanges:
    def test_a_cancel_marks_the_request_and_shuts_its_socket_down(self):
        table = relay_era.Exchanges()
        exchange = table.begin(5)
        sock = FakeSocket()
        exchange.attach(sock)
        assert table.cancel(5) is True
        assert exchange.cancelled and sock.shutdowns == [socket.SHUT_RDWR]

    def test_a_cancel_before_the_socket_exists_stops_the_post_before_it_connects(self):
        table = relay_era.Exchanges()
        exchange = table.begin("job-1")
        assert table.cancel("job-1") is True
        with pytest.raises(relay_era.Cancelled):
            exchange.attach(FakeSocket())

    def test_a_client_that_reuses_an_id_in_flight_is_cancelled_by_its_newest_request(self):
        """The table holds one request per id. A client that sends an id again before the
        first is answered replaces the entry, and a cancel (which names an id, nothing
        more) reaches the newer request."""
        table = relay_era.Exchanges()
        older, newer = table.begin(5), table.begin("5")
        assert older is not newer
        assert table.cancel(5) is True
        assert newer.cancelled and not older.cancelled

    def test_a_cancel_for_what_is_not_in_flight_is_not_one(self):
        table = relay_era.Exchanges()
        assert table.cancel(5) is False
        exchange = table.begin(5)
        table.end(5, exchange)
        assert table.cancel(5) is False, "an answered request is no longer in flight"

    def test_ending_a_request_does_not_remove_another_with_the_same_id(self):
        table = relay_era.Exchanges()
        old = table.begin(5)
        table.end(5, old)
        newer = table.begin(5)
        table.end(5, old)       # a late end of the first must not unregister the second
        assert table.cancel(5) is True and newer.cancelled

    def test_a_request_with_no_id_is_never_cancellable(self):
        table = relay_era.Exchanges()
        assert table.cancel(None) is False
        assert table.begin(None) is not table.begin(None)

    def test_an_abort_after_the_socket_was_released_touches_nothing(self):
        """The socket handle may already belong to someone else by then."""
        exchange = relay_era.Exchange()
        sock = FakeSocket()
        exchange.attach(sock)
        exchange.detach()
        exchange.abort()
        assert sock.shutdowns == [] and exchange.cancelled


# --- the exchange on a real connection -------------------------------------------------------

class TestPost:
    def test_the_request_reaches_the_server_with_the_headers_and_the_body(self, backend):
        server = backend()
        frame = modern_frame("tools/call", 4, name="server_info", arguments={})
        body = json.dumps(frame).encode()
        headers = relay_era.request_headers(frame, VERSION, 4242, "ef" * 16)
        status, response_headers, text = relay_era.post(
            "127.0.0.1", server.port, body, headers, relay_era.Exchange(), 5, time.monotonic() + 5)
        assert status == 200 and json.loads(text)["id"] == 4
        assert response_headers.get("Content-Type") == "application/json"
        path, seen, seen_body = server.requests[0]
        assert path == "/mcp" and seen_body == body.decode()
        assert seen["mcp-protocol-version"] == VERSION
        assert seen["mcp-method"] == "tools/call" and seen["mcp-name"] == "server_info"
        assert seen[CLIENT_PID_HEADER.lower()] == "4242"
        assert seen["x-browserplug-connection-id"] == "ef" * 16
        assert "mcp-session-id" not in seen

    def test_an_error_status_is_a_result_not_an_exception(self, backend):
        error = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": "nope"}}
        server = backend(lambda message, headers, handler: (404, error, None))
        status, _, text = relay_era.post("127.0.0.1", server.port, b"{}", {}, relay_era.Exchange(),
                                         5, time.monotonic() + 5)
        assert status == 404 and json.loads(text) == error

    def test_aborting_the_exchange_ends_the_connection_and_raises_cancelled(self, backend):
        server = backend(lambda message, headers, handler: (server.hold(handler),
                                                            (200, {}, None))[1])
        exchange = relay_era.Exchange()
        thread, result = post_in_thread(server, exchange)
        assert server.started.wait(5), "the request never reached the server"
        exchange.abort()
        thread.join(5)
        assert not thread.is_alive()
        assert isinstance(result.get("error"), relay_era.Cancelled), result
        assert server.disconnected.wait(5), "the server never saw its client hang up"

    def test_a_request_cancelled_before_it_is_sent_never_connects(self, backend):
        server = backend()
        exchange = relay_era.Exchange()
        exchange.abort()
        with pytest.raises(relay_era.Cancelled):
            relay_era.post("127.0.0.1", server.port, b"{}", {}, exchange, 5, time.monotonic() + 5)
        assert server.requests == []

    def test_a_server_that_only_pings_hits_the_overall_deadline(self, backend):
        """A socket timeout restarts on every byte, so a stream that pings forever never
        trips it. The deadline timer, armed at connect, cuts the connection."""
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
                server.disconnected.set()
            return 200, "", None

        server = backend(pinging)
        started = time.monotonic()
        thread, result = post_in_thread(server, relay_era.Exchange(), deadline_seconds=0.6)
        thread.join(6)
        assert not thread.is_alive(), "the post is still reading a stream that only pings"
        assert isinstance(result.get("error"), relay_era.DeadlineCut), result
        assert time.monotonic() - started < 4
        assert server.disconnected.wait(5)

    def test_a_server_that_never_answers_hits_the_deadline_too(self, backend):
        server = backend(lambda message, headers, handler: (server.hold(handler),
                                                            (200, {}, None))[1])
        thread, result = post_in_thread(server, relay_era.Exchange(), deadline_seconds=0.6)
        thread.join(6)
        assert isinstance(result.get("error"), relay_era.DeadlineCut), result

    def test_a_refused_connection_stays_a_connection_failure(self):
        """A dead server is what recovery is for: it must not read as a cancel or a deadline."""
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        with pytest.raises(OSError) as raised:
            relay_era.post("127.0.0.1", port, b"{}", {}, relay_era.Exchange(), 5,
                           time.monotonic() + 5)
        assert not isinstance(raised.value, (relay_era.Cancelled, relay_era.DeadlineCut))

    def test_a_connect_that_times_out_is_a_lost_server_and_not_a_deadline(self, monkeypatch):
        """The urllib path reads a connect timeout as a lost server: the relay recovers and
        replays. The deadline is for a request the server accepted, so a modern request that
        never connected must take the same road, even when the timeout is the deadline."""
        def never_connects(self):
            raise TimeoutError("timed out")

        monkeypatch.setattr(http.client.HTTPConnection, "connect", never_connects)
        with pytest.raises(TimeoutError) as raised:
            relay_era.post("127.0.0.1", 9, b"{}", {}, relay_era.Exchange(), 5,
                           time.monotonic() - 1)
        assert not isinstance(raised.value, (relay_era.Cancelled, relay_era.DeadlineCut))

    def test_a_cancel_that_lands_while_connecting_is_a_cancel(self, monkeypatch):
        exchange = relay_era.Exchange()

        def refused_after_the_cancel(self):
            exchange.abort()
            raise ConnectionRefusedError(10061, "refused")

        monkeypatch.setattr(http.client.HTTPConnection, "connect", refused_after_the_cancel)
        with pytest.raises(relay_era.Cancelled):
            relay_era.post("127.0.0.1", 9, b"{}", {}, exchange, 5, time.monotonic() + 5)


class StubResponse:
    """A response whose body read fails, as a socket cut mid-body can."""

    status = 200
    headers = {}

    def __init__(self, error):
        self.error = error
        self.closed = False

    def read(self):
        if self.error is not None:
            raise self.error
        return b"{}"

    def close(self):
        self.closed = True


class TestPostCleanup:
    @pytest.mark.parametrize("error", [None, ConnectionResetError(10054, "reset")])
    def test_the_response_is_closed_however_the_read_ends(self, backend, monkeypatch, error):
        """A response that will close owns the socket (the connection hands it over), so
        closing the connection alone leaves it open until the garbage collector runs."""
        response = StubResponse(error)
        monkeypatch.setattr(http.client.HTTPConnection, "getresponse", lambda self: response)
        server = backend()
        try:
            relay_era.post("127.0.0.1", server.port, b"{}", {}, relay_era.Exchange(), 5,
                           time.monotonic() + 5)
        except ConnectionResetError:
            assert error is not None
        assert response.closed is True

    def test_a_stopped_deadline_watch_is_freed_by_reference_counting(self):
        """The timer holds the watch's bound method, so the watch must let the timer go in
        stop(). Otherwise each request leaves a cycle, and its dead timer thread's kernel
        handles stay open until the cyclic collector gets to them."""
        was_enabled = gc.isenabled()
        gc.collect()
        gc.disable()
        try:
            watch = relay_era._DeadlineWatch(FakeSocket(), time.monotonic() + 60)
            alive = weakref.ref(watch)
            watch.stop()
            del watch
            end = time.monotonic() + 5      # the cancelled timer thread exits on its own
            while alive() is not None and time.monotonic() < end:
                time.sleep(0.01)
            assert alive() is None
        finally:
            if was_enabled:
                gc.enable()

    def test_a_stopped_watch_never_fires_and_stop_may_be_repeated(self):
        sock = FakeSocket()
        watch = relay_era._DeadlineWatch(sock, time.monotonic() + 0.2)
        watch.stop()
        watch.stop()
        time.sleep(0.4)
        assert sock.shutdowns == [] and not watch.fired.is_set()

    def test_a_thousand_requests_leave_no_garbage_cycles_and_flat_handles(self, backend):
        server = backend()
        frame = modern_frame("tools/list", 1)
        body = json.dumps(frame).encode()
        headers = relay_era.request_headers(frame, VERSION, 1, "ab" * 16)

        def handle_count():
            if sys.platform != "win32":
                return 0
            import ctypes
            kernel = ctypes.WinDLL("kernel32")
            kernel.GetCurrentProcess.restype = ctypes.c_void_p
            kernel.GetProcessHandleCount.argtypes = [ctypes.c_void_p,
                                                     ctypes.POINTER(ctypes.c_ulong)]
            count = ctypes.c_ulong()
            assert kernel.GetProcessHandleCount(kernel.GetCurrentProcess(), ctypes.byref(count))
            return count.value

        was_enabled = gc.isenabled()
        gc.collect()
        gc.disable()
        try:
            before = handle_count()
            for _ in range(1000):
                relay_era.post("127.0.0.1", server.port, body, headers, relay_era.Exchange(),
                               10, time.monotonic() + 10)
            time.sleep(0.3)         # timer and server threads finish
            grown = handle_count() - before
            unreachable = gc.collect()
        finally:
            if was_enabled:
                gc.enable()
        assert len(server.requests) == 1000
        # Measured with the cycle: 32 objects and 5 kernel handles per request; without: 0, 0.
        assert grown < 50, f"{grown} kernel handles left open by 1000 requests"
        assert unreachable < 500, f"{unreachable} objects left for the cyclic collector"


# --- subscriptions/listen --------------------------------------------------------------------

class Collected:
    """A relay's stdout as parsed frames, and the errors it was told to write."""

    def __init__(self):
        self.frames = []
        self.errors = []

    def emit(self, raw):
        self.frames.append(json.loads(raw))

    def emit_error(self, msg_id, message, code):
        self.errors.append((msg_id, code, message))


def listen_frame(listen_id="listen-1", **wanted):
    return modern_frame("subscriptions/listen", listen_id, notifications=wanted)


class TestSubscriptions:
    @staticmethod
    def make():
        out = Collected()
        return out, relay_era.Subscriptions(out.emit, out.emit_error)

    def test_the_acknowledgment_is_what_the_sdks_wire_model_says(self):
        out, subs = self.make()
        subs.listen(listen_frame("listen-1", toolsListChanged=True))
        expected = SubscriptionsAcknowledgedNotification(
            params=SubscriptionsAcknowledgedNotificationParams(
                notifications=SubscriptionFilter(tools_list_changed=True),
                _meta={SUBSCRIPTION_ID_META_KEY: "listen-1"})
        ).model_dump(by_alias=True, mode="json", exclude_none=True)
        assert len(out.frames) == 1
        ack = out.frames[0]
        assert ack["jsonrpc"] == "2.0" and "id" not in ack
        assert ack["method"] == expected["method"] == "notifications/subscriptions/acknowledged"
        assert ack["params"] == expected["params"]

    def test_the_change_notice_is_the_sdks_stamped_notification(self):
        out, subs = self.make()
        subs.listen(listen_frame("listen-1", toolsListChanged=True))
        assert subs.tools_changed() == 1
        notice = out.frames[1]
        expected = event_to_notification(
            ToolsListChanged(), {SUBSCRIPTION_ID_META_KEY: "listen-1"}).model_dump(
                by_alias=True, mode="json", exclude_none=True)
        assert notice["method"] == expected["method"] == "notifications/tools/list_changed"
        assert notice["params"] == expected["params"]
        assert "id" not in notice

    def test_the_ack_echoes_the_honored_filter_as_the_sdk_does(self):
        out, subs = self.make()
        subs.listen(listen_frame(1, toolsListChanged=True, promptsListChanged=False,
                                 resourcesListChanged=True, resourceSubscriptions=["a://b"]))
        assert out.frames[0]["params"]["notifications"] == {
            "toolsListChanged": True, "resourcesListChanged": True,
            "resourceSubscriptions": ["a://b"]}
        assert out.frames[0]["params"]["_meta"] == {SUBSCRIPTION_ID_META_KEY: 1}

    def test_a_stream_is_told_only_about_what_it_asked_for(self):
        out, subs = self.make()
        subs.listen(listen_frame("prompts-only", promptsListChanged=True))
        subs.listen(listen_frame("tools", toolsListChanged=True))
        assert subs.tools_changed() == 1
        notices = [f for f in out.frames if f["method"] == "notifications/tools/list_changed"]
        assert [n["params"]["_meta"][SUBSCRIPTION_ID_META_KEY] for n in notices] == ["tools"]

    def test_nothing_is_sent_when_nobody_listens(self):
        out, subs = self.make()
        assert subs.tools_changed() == 0 and out.frames == []

    def test_a_closed_stream_hears_no_more(self):
        out, subs = self.make()
        subs.listen(listen_frame("a", toolsListChanged=True))
        assert subs.close("a") is True
        assert subs.close("a") is False
        assert subs.tools_changed() == 0
        assert len(out.frames) == 1, "only the acknowledgment"

    def test_a_listen_with_the_same_id_replaces_the_old_one(self):
        out, subs = self.make()
        subs.listen(listen_frame("a", toolsListChanged=True))
        subs.listen(listen_frame("a", toolsListChanged=True))
        assert len(subs) == 1 and subs.tools_changed() == 1

    def test_the_acknowledgment_comes_before_any_notice(self):
        """Both run under one lock, so a poll that fires while a listen is being answered
        cannot put a notice ahead of the acknowledgment on its stream."""
        out = Collected()
        subs = relay_era.Subscriptions(out.emit, out.emit_error)
        original = out.emit

        def slow_emit(raw):
            time.sleep(0.05)       # the window in which a watcher would slip in
            original(raw)

        subs._emit = slow_emit
        listener = threading.Thread(
            target=lambda: subs.listen(listen_frame("a", toolsListChanged=True)))
        listener.start()
        deadline = time.monotonic() + 2
        while not len(subs) and time.monotonic() < deadline:
            time.sleep(0.001)
        subs.tools_changed()
        listener.join(5)
        assert [f["method"] for f in out.frames] == [
            "notifications/subscriptions/acknowledged", "notifications/tools/list_changed"]

    def test_a_bad_listen_is_answered_with_an_error_under_its_id(self):
        out, subs = self.make()
        subs.listen({"jsonrpc": "2.0", "id": "x", "method": "subscriptions/listen",
                     "params": {"_meta": {}}})
        subs.listen({"jsonrpc": "2.0", "id": [1], "method": "subscriptions/listen",
                     "params": {"notifications": {}}})
        subs.listen({"jsonrpc": "2.0", "method": "subscriptions/listen",
                     "params": {"notifications": {}}})
        assert [(i, c) for i, c, _ in out.errors] == [("x", -32602), ([1], -32600)]
        assert out.frames == [] and len(subs) == 0

    def test_a_stream_at_the_cap_can_still_be_replaced(self):
        """The limit stops new streams, not a client re-listening under an id it already
        holds (it does that after every reconnect of its own)."""
        out, subs = self.make()
        for n in range(relay_era.MAX_SUBSCRIPTIONS):
            subs.listen(listen_frame(f"s{n}", toolsListChanged=True))
        acknowledged = len(out.frames)
        subs.listen(listen_frame("s3", toolsListChanged=True))
        assert out.errors == [] and len(subs) == relay_era.MAX_SUBSCRIPTIONS
        assert len(out.frames) == acknowledged + 1, "the replacement is acknowledged"
        # a new id past the cap is still refused, and the replaced id still works
        subs.listen(listen_frame(7, toolsListChanged=True))
        assert out.errors and out.errors[0][0] == 7, "17th id is refused"
        before = len(out.errors)
        subs.listen(listen_frame("s3", toolsListChanged=True))
        assert len(out.errors) == before

    def test_the_number_of_streams_is_bounded(self):
        out, subs = self.make()
        for n in range(relay_era.MAX_SUBSCRIPTIONS):
            subs.listen(listen_frame(f"s{n}", toolsListChanged=True))
        subs.listen(listen_frame("one-too-many", toolsListChanged=True))
        assert out.errors and out.errors[0][0] == "one-too-many"
        assert len(subs) == relay_era.MAX_SUBSCRIPTIONS


# --- the carriage ----------------------------------------------------------------------------

def make_carriage(era="all", legacy=False, pid=4242):
    out = Collected()
    logged = []
    carriage = relay_era.Carriage(era, out.emit, out.emit_error,
                                  lambda *a: logged.append(" ".join(str(x) for x in a)),
                                  pid, legacy_session=lambda: legacy)
    return carriage, out, logged


class TestCarriage:
    @pytest.mark.parametrize("era", [None, mcp_era.HANDSHAKE, mcp_era.DIRECT])
    def test_without_era_all_nothing_is_recognised_or_intercepted(self, era):
        carriage, out, _ = make_carriage(era)
        assert carriage.enabled is False
        for frame in (modern_frame("tools/list"), listen_frame(),
                      {"jsonrpc": "2.0", "method": "notifications/cancelled",
                       "params": {"requestId": 1}}):
            assert carriage.version_of(frame) is None
            assert carriage.intercept(frame) is False
        assert carriage.tools_changed() == 0 and out.frames == [] and out.errors == []

    def test_under_all_a_modern_frame_is_recognised(self):
        carriage, _, _ = make_carriage("all")
        assert carriage.enabled is True
        assert carriage.version_of(modern_frame("tools/list")) == VERSION
        assert carriage.version_of({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) is None

    def test_a_listen_is_answered_and_consumed(self):
        carriage, out, _ = make_carriage()
        assert carriage.intercept(listen_frame("L", toolsListChanged=True)) is True
        assert out.frames[0]["method"] == "notifications/subscriptions/acknowledged"
        assert carriage.tools_changed() == 1

    def test_a_listen_without_the_envelope_is_not_ours(self):
        carriage, out, _ = make_carriage()
        frame = {"jsonrpc": "2.0", "id": 1, "method": "subscriptions/listen",
                 "params": {"notifications": {"toolsListChanged": True}}}
        assert carriage.intercept(frame) is False and out.frames == []

    def test_a_modern_request_is_entered_so_a_later_cancel_finds_it(self):
        carriage, _, logged = make_carriage()
        frame = modern_frame("tools/call", 12, name="x")
        assert carriage.intercept(frame) is False, "a request goes on to a worker"
        exchange = carriage.enter(frame)
        cancel = {"jsonrpc": "2.0", "method": "notifications/cancelled",
                  "params": {"requestId": 12, "reason": "user"}}
        assert carriage.intercept(cancel) is True
        assert exchange.cancelled
        assert any("cancelled request 12" in line for line in logged), logged

    def test_a_cancel_may_name_the_id_as_text(self):
        carriage, _, _ = make_carriage()
        exchange = carriage.enter(modern_frame("tools/call", 12, name="x"))
        assert carriage.intercept({"jsonrpc": "2.0", "method": "notifications/cancelled",
                                   "params": {"requestId": "12"}}) is True
        assert exchange.cancelled

    @pytest.mark.parametrize("era", [None, mcp_era.HANDSHAKE, mcp_era.DIRECT])
    def test_without_era_all_no_request_is_entered(self, era):
        carriage, _, _ = make_carriage(era)
        assert carriage.enter(modern_frame("tools/call", 12, name="x")) is None

    def test_only_a_modern_request_is_entered(self):
        carriage, _, _ = make_carriage()
        assert carriage.enter({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) is None
        assert carriage.enter(modern_frame("notifications/x", msg_id=None)) is None, \
            "a notification has no id to cancel it by"
        assert carriage.enter(modern_frame("tools/list", 1)) is not None

    def test_a_cancel_closes_the_listen_stream_it_names(self):
        carriage, out, _ = make_carriage()
        carriage.intercept(listen_frame("L", toolsListChanged=True))
        assert carriage.intercept({"jsonrpc": "2.0", "method": "notifications/cancelled",
                                   "params": {"requestId": "L"}}) is True
        assert carriage.tools_changed() == 0

    def test_a_cancel_for_nothing_in_flight_is_dropped_without_a_handshake_session(self):
        """Nothing on the server could use it, and sent on it draws an error from a server
        that has no session to look it up in."""
        carriage, _, _ = make_carriage(legacy=False)
        assert carriage.intercept({"jsonrpc": "2.0", "method": "notifications/cancelled",
                                   "params": {"requestId": 99}}) is True

    def test_a_cancel_for_nothing_in_flight_goes_on_when_a_handshake_session_exists(self):
        """It may name the handshake-era call that session is still serving."""
        carriage, _, _ = make_carriage(legacy=True)
        assert carriage.intercept({"jsonrpc": "2.0", "method": "notifications/cancelled",
                                   "params": {"requestId": 99}}) is False

    def test_a_malformed_cancel_does_not_raise(self):
        carriage, _, _ = make_carriage()
        for params in (None, [], {"requestId": [1]}, {}):
            frame = {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": params}
            assert carriage.intercept(frame) is True

    def test_what_a_reply_says_about_the_servers_era_is_logged_once(self):
        carriage, _, logged = make_carriage()
        refusal = json.dumps({"jsonrpc": "2.0", "id": 1, "error": {
            "code": -32022, "message": "Unsupported protocol version",
            "data": {"supported": ["2025-11-25"], "requested": VERSION}}})
        carriage.note_reply(400, [refusal])
        carriage.note_reply(400, [refusal])
        assert len(logged) == 1 and "does not serve 2026-07-28" in logged[0]
        # after the server served it, the same refusal is a changed setting, not a fallback
        carriage, _, logged = make_carriage()
        carriage.note_reply(200, ['{"jsonrpc":"2.0","id":1,"result":{}}'])
        carriage.note_reply(400, [refusal])
        assert len(logged) == 1 and "stopped serving 2026-07-28" in logged[0]
        assert "reconnect" in logged[0]

    @staticmethod
    def refusal(supported, requested="2027-01-01", **extra):
        return json.dumps({"jsonrpc": "2.0", "id": 1, "error": {
            "code": -32022, "message": "Unsupported protocol version",
            "data": {"supported": supported, "requested": requested, **extra}}})

    def test_a_refusal_that_still_lists_2026_07_28_is_not_blamed_on_the_era(self):
        """The server serves the era; this one request named a version it does not (a client
        newer than the server). The log must not send anyone chasing an mcp.era change."""
        carriage, _, logged = make_carriage()
        carriage.note_reply(200, ['{"jsonrpc":"2.0","id":1,"result":{}}'])
        carriage.note_reply(400, [self.refusal([VERSION])])
        assert len(logged) == 1
        assert "mcp.era" not in logged[0] and "stopped serving" not in logged[0]
        assert "2027-01-01" in logged[0] and VERSION in logged[0]

    def test_the_same_refusal_repeated_is_logged_once(self):
        carriage, _, logged = make_carriage()
        carriage.note_reply(200, ['{"jsonrpc":"2.0","id":1,"result":{}}'])
        for _ in range(3):
            carriage.note_reply(400, [self.refusal([VERSION])])
        assert len(logged) == 1
        # and the era-changed line is once as well, however often the client retries
        carriage, _, logged = make_carriage()
        carriage.note_reply(200, ['{"jsonrpc":"2.0","id":1,"result":{}}'])
        for _ in range(3):
            carriage.note_reply(400, [self.refusal(["2025-11-25"], VERSION)])
        assert len(logged) == 1 and "stopped serving" in logged[0]

    def test_an_era_change_is_still_reported_after_a_complaint_about_one_version(self):
        carriage, _, logged = make_carriage()
        carriage.note_reply(200, ['{"jsonrpc":"2.0","id":1,"result":{}}'])
        carriage.note_reply(400, [self.refusal([VERSION])])
        carriage.note_reply(400, [self.refusal(["2025-11-25"], VERSION)])
        assert len(logged) == 2 and "stopped serving" in logged[1]

    def test_a_second_era_change_after_the_server_served_again_is_logged_again(self):
        """serve, refuse, serve, refuse: the server came back on 2026-07-28 and moved off it a
        second time. The first line's de-duplication must not swallow the second."""
        carriage, _, logged = make_carriage()
        served = '{"jsonrpc":"2.0","id":1,"result":{}}'
        refused = self.refusal(["2025-11-25"], VERSION)
        carriage.note_reply(200, [served])
        carriage.note_reply(400, [refused])
        carriage.note_reply(400, [refused])
        assert len(logged) == 1
        carriage.note_reply(200, [served])
        carriage.note_reply(400, [refused])
        carriage.note_reply(400, [refused])
        assert len(logged) == 2 and "stopped serving" in logged[1]

    def test_a_refusal_with_no_list_is_read_as_the_era_having_moved(self):
        carriage, _, logged = make_carriage()
        carriage.note_reply(200, ['{"jsonrpc":"2.0","id":1,"result":{}}'])
        carriage.note_reply(400, [json.dumps({"jsonrpc": "2.0", "id": 1, "error": {
            "code": -32022, "message": "x", "data": "not an object"}})])
        carriage.note_reply(400, [self.refusal("2026-07-28")])
        assert len(logged) == 1 and "stopped serving" in logged[0]

    def test_other_error_replies_are_not_worth_a_line(self):
        carriage, _, logged = make_carriage()
        carriage.note_reply(404, ['{"jsonrpc":"2.0","id":1,"error":{"code":-32601}}'])
        carriage.note_reply(500, ["plain text"])
        assert logged == []


# --- what an old relay sent is what a relay under any other era still sends ----------------

class RawServer:
    """A socket server that keeps the bytes of every request, for byte-for-byte comparison."""

    def __init__(self):
        self.raw = []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            data = b""
            while b"\r\n\r\n" not in data:
                data += conn.recv(65536)
            head, _, rest = data.partition(b"\r\n\r\n")
            length = int([line for line in head.split(b"\r\n")
                          if line.lower().startswith(b"content-length")][0].split(b":")[1])
            while len(rest) < length:
                rest += conn.recv(65536)
            self.raw.append(head + b"\r\n\r\n" + rest)
            reply = json.dumps({"jsonrpc": "2.0", "id": json.loads(rest).get("id"),
                                "result": {"protocolVersion": "2025-06-18"}}).encode()
            session = b"Mcp-Session-Id: S1\r\n" if b'"initialize"' in rest else b""
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n" + session
                         + b"Content-Length: %d\r\nConnection: close\r\n\r\n" % len(reply) + reply)
            conn.close()

    def close(self):
        self.sock.close()


def normalized(raw, port):
    """A request's bytes with the server's port blanked, so two runs can be compared."""
    return raw.replace(b"127.0.0.1:%d" % port, b"127.0.0.1:PORT")


def todays_request(body, session=False):
    """The bytes a POST had before relay_era existed, as the interpreter's urllib writes them.

    This is the old StdioRelay.post, kept here as a reference: one urllib request with
    Content-Type, Accept, the client pid and, once a session exists, Mcp-Session-Id and
    MCP-Protocol-Version, added in that order. Header order and spelling are urllib's, which
    differ between Python versions, so the expectation is produced rather than written down.
    (On 3.14 it was recorded from the relay itself: Accept-Encoding, Content-Length, Host,
    User-Agent, Content-type, Accept, X-browserplug-client-pid, Mcp-session-id,
    Mcp-protocol-version, Connection.)
    """
    server = RawServer()
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{server.port}/mcp", data=body,
                                     method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json, text/event-stream")
        req.add_header(CLIENT_PID_HEADER, "4242")
        if session:
            req.add_header("Mcp-Session-Id", "S1")
            req.add_header("MCP-Protocol-Version", "2025-06-18")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        opener.open(req, timeout=5).close()
        return normalized(server.raw[0], server.port)
    finally:
        server.close()


HANDSHAKE_CLIENT = [
    # (frame, whether it carries the session the initialize reply opened)
    ({"jsonrpc": "2.0", "id": 1, "method": "initialize",
      "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                 "clientInfo": {"name": "t", "version": "0"}}}, False),
    ({"jsonrpc": "2.0", "method": "notifications/initialized"}, True),
    ({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, True),
    ({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
      "params": {"name": "tabs", "arguments": {"action": "list"}}}, True),
    ({"jsonrpc": "2.0", "method": "notifications/cancelled",
      "params": {"requestId": 3, "reason": "x"}}, True),
]
# What a probing client sends first. A relay that does not carry 2026-07-28 forwards it as
# it always did: the server's gate answers it and the client falls back to initialize.
DISCOVER_PROBE = {"jsonrpc": "2.0", "id": 4, "method": "server/discover",
                  "params": {"_meta": {relay_era.ENVELOPE_VERSION_KEY: VERSION,
                                       CAPABILITIES_KEY: {}}}}


class TestHandshakeBytesAreUnchanged:
    """The pin: a relay that does not carry 2026-07-28 for a frame sends that frame as
    byte for byte what it always sent. The expectation is `todays_request`: the old POST,
    reproduced with urllib, so a header added to the handshake path fails here."""

    @staticmethod
    def run(era, frames):
        server = RawServer()
        try:
            relay = StdioRelay(server.port, log=lambda *a: None, out=io.BytesIO(),
                               poll_seconds=0, client_pid=4242, era=era)
            relay.report_timing = lambda *a: None
            for frame, _ in frames:
                relay.handle_frame(json.dumps(frame))
            return [normalized(raw, server.port) for raw in server.raw]
        finally:
            server.close()

    @pytest.mark.parametrize("era", [None, mcp_era.HANDSHAKE, mcp_era.DIRECT])
    def test_every_post_is_what_it_always_was(self, era):
        frames = HANDSHAKE_CLIENT + [(DISCOVER_PROBE, True)]
        raw = self.run(era, frames)
        assert len(raw) == len(frames)
        for (frame, session), got in zip(frames, raw):
            assert got == todays_request(json.dumps(frame).encode(), session), frame

    def test_a_handshake_client_is_served_the_same_bytes_under_all(self):
        """`all` changes nothing for a client that never sends the envelope."""
        raw = self.run(mcp_era.ALL, HANDSHAKE_CLIENT)
        assert len(raw) == len(HANDSHAKE_CLIENT)
        for (frame, session), got in zip(HANDSHAKE_CLIENT, raw):
            assert got == todays_request(json.dumps(frame).encode(), session), frame

    def test_the_connection_id_is_not_added_to_a_handshake_request_under_all(self):
        raw = self.run(mcp_era.ALL, HANDSHAKE_CLIENT)
        assert all(b"connection-id" not in r.lower() for r in raw)


# --- weight ----------------------------------------------------------------------------------

class TestWeight:
    """This module rides the relay's memory budget, a megabyte in all (the relay is one
    process per MCP client on the box)."""

    def test_the_module_pulls_in_nothing_heavy_and_not_uuid(self):
        probe = textwrap.dedent("""
            import sys
            sys.path[:0] = [%r, %r]
            import launcher.relay_era
            heavy = {"fastmcp", "mcp", "mcp_types", "httpx", "httpx2", "httpcore2", "pydantic",
                     "uvicorn", "starlette", "jsonschema", "watchfiles", "asyncio", "psutil",
                     "uuid", "socketserver"}
            print(",".join(sorted({m.split('.')[0] for m in sys.modules} & heavy)))
        """) % (_REPO_ROOT, _SHARED_PY)
        out = subprocess.run([sys.executable, "-I", "-c", probe], capture_output=True, text=True,
                             timeout=120)
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == "", (
            "launcher/relay_era.py pulled in " + out.stdout.strip() + ". uuid in particular "
            "loads a DLL that costs a megabyte of commit; relay_era.new_connection_id uses "
            "os.urandom for that reason.")
