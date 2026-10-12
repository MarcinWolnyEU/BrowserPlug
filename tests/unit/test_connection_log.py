"""Unit tests for server/ws/connection_log.py and the three places server.py hooks it in.

The whole server-side record of the 2026-09-21 wedge was two lines: `connection closed`,
and later `ERROR ... Error handling extension connection: no close frame received or
sent`. No browser named, no close code, no duration, and nothing at all about the two
reconnect attempts that opened a socket and then failed a five-second hello window. The
investigation could not get past "connection closed".

So the tests here are about what a reader of the log gets afterwards:

  * a departing browser leaves ONE line naming itself, how long it lasted, the close code
    and what that code means;
  * the drop is kept on the RECORD too, and survives the reconnect that replaces it — by
    the time anyone asks "why did it go?", the socket that knew is gone;
  * a slow hello is distinguishable from an absent one, because the remedies are
    opposite: wait, versus go and look at the browser;
  * a hello TIMEOUT closes with a reason the extension can tell from a token problem, or
    a browser already short of memory goes off to refetch a token it already has;
  * the extension's own journal is logged and kept, since its console dies with the
    browser and a force-kill leaves nothing else of its side of the story.
"""

import asyncio
import logging
import time
from datetime import datetime
from unittest.mock import patch

import pytest

import test_imports  # noqa: F401  (path setup)
from server.ws import connection_log
from server.ws import ws_auth
from server.server import BrowserPlugServer
from server.ws.ws_auth import WS_CLOSE_REASON_MAX_BYTES
from test_browser_registry import FakeWS, hello
from test_browser_routing import add_browser
from test_ws_token import FakeWS as TokenFakeWS
from test_ws_token import connect as token_connect
from test_ws_token import make_server as make_token_server
from test_ws_token import mint


@pytest.fixture
def server():
    return BrowserPlugServer(host="localhost", port=8772, start_mcp=False)


# =============================================================================
# close_phrase / closed_from_exception: the vocabulary
# =============================================================================

def test_1006_is_glossed_as_the_peer_vanishing():
    """The code the wedge actually produced. "1006" alone is a lookup nobody does
    mid-incident, and the gloss is the entire difference between "Firefox hung up" and
    "Firefox was killed"."""
    phrase = connection_log.close_phrase(1006)
    assert "1006" in phrase
    assert "vanished" in phrase


def test_a_missing_close_code_says_there_was_no_close_frame():
    phrase = connection_log.close_phrase(None)
    assert "no close code" in phrase
    assert "without a close frame" in phrase


def test_the_peers_own_reason_is_quoted_when_it_sent_one():
    phrase = connection_log.close_phrase(1001, "browser shutting down")
    assert "1001" in phrase and "going away" in phrase
    assert '"browser shutting down"' in phrase


def test_a_mock_shaped_code_or_reason_never_raises():
    """These come off a socket object. A test double answers close_reason with a Mock,
    and a diagnostic that raises on the teardown path takes the teardown with it."""
    assert connection_log.close_phrase(object(), object())
    assert connection_log.close_phrase(True, b"bytes")   # bool is not an int here


def test_closed_from_exception_prefers_what_the_PEER_sent():
    class Close:
        def __init__(self, code, reason):
            self.code, self.reason = code, reason

    class Closed(Exception):
        rcvd = Close(1001, "going away")
        sent = Close(1000, "")

    assert connection_log.closed_from_exception(Closed()) == (1001, "going away")


def test_closed_from_exception_falls_back_to_what_WE_sent():
    class Closed(Exception):
        rcvd = None
        sent = type("C", (), {"code": 1000, "reason": "server restarting"})()

    assert connection_log.closed_from_exception(Closed()) == (1000, "server restarting")


def test_closed_from_exception_on_an_abnormal_close_reports_nothing_rather_than_guessing():
    class Closed(Exception):
        rcvd = None
        sent = None

    assert connection_log.closed_from_exception(Closed()) == (None, None)


# =============================================================================
# The hello window
# =============================================================================

def test_a_prompt_hello_logs_nothing():
    """A line that prints on every connect is a line nobody reads."""
    assert connection_log.hello_latency_line("[1] Firefox", 40) == ""


def test_a_slow_hello_says_slow_not_absent():
    line = connection_log.hello_latency_line("[1] Firefox pid 23176", 7300,
                                             window_seconds=15)
    assert "7.3s" in line
    assert "slow, not absent" in line
    assert "15s" in line


def test_the_hello_timeout_close_reason_is_recognisable_and_fits_the_protocol_cap():
    """The extension refetches its token on ANY 4401. After a timeout that is wasted work
    on a browser that is already struggling, so the reason has to be tellable apart —
    by a PREFIX, which is all the extension can match on."""
    reason = connection_log.hello_timeout_close_reason(15, WS_CLOSE_REASON_MAX_BYTES)
    assert reason.startswith("hello timeout:")
    assert "15s" in reason
    assert len(reason.encode("utf-8")) <= WS_CLOSE_REASON_MAX_BYTES


def test_the_close_reason_is_cut_to_the_cap_not_allowed_to_exceed_it():
    reason = connection_log.hello_timeout_close_reason(15, 20)
    assert len(reason.encode("utf-8")) <= 20
    assert reason.startswith("hello timeout:")


# =============================================================================
# The journal
# =============================================================================

def test_the_journal_renders_as_one_line_of_events_in_order():
    at = datetime(2026, 9, 21, 23, 54, 58).timestamp() * 1000
    line = connection_log.journal_line("[1] Firefox pid 23176", [
        {"t": at, "e": "close", "code": 1006},
        {"t": at + 70_000, "e": "open"},
        {"t": at + 75_000, "e": "rejected", "code": 4401,
         "reason": "hello timeout: no extension_hello within 15s"},
    ])
    assert line.startswith("[1] Firefox pid 23176 reports since its last hello: ")
    assert "close 23:54:58 code 1006" in line
    assert "open 23:56:08" in line
    assert 'rejected 23:56:13 code 4401 "hello timeout' in line


def test_an_empty_journal_produces_no_line_at_all():
    assert connection_log.journal_line("[1] Firefox", []) == ""
    assert connection_log.journal_line("[1] Firefox", None) == ""


def test_a_long_journal_loses_whole_entries_and_says_how_many():
    """An entry cut in half reads as data. Dropping it and counting does not."""
    entries = [{"e": "retry", "attempt": n, "reason": "x" * 40} for n in range(20)]
    line = connection_log.journal_line("[1] Firefox", entries, max_chars=300)
    assert len(line) <= 360          # the head plus one entry's worth of overshoot
    assert "more)" in line


def test_a_malformed_entry_is_dropped_rather_than_raising():
    kept = connection_log.clean_journal([
        {"e": "open", "t": 1_700_000_000_000},
        "not a dict",
        {"t": 1_700_000_000_000},                 # no event name
        {"e": "", "t": 1},                        # blank event name
        {"e": "close", "code": "1006"},           # code must be an int
        {"e": "retry", "attempt": True},          # a bool is not an attempt count
    ])
    assert [e["e"] for e in kept] == ["open", "close", "retry"]
    assert "code" not in kept[1] and "attempt" not in kept[2]


def test_the_journal_is_capped_and_unknown_keys_are_dropped():
    kept = connection_log.clean_journal(
        [{"e": "retry", "attempt": n, "smuggled": "x" * 5000} for n in range(50)])
    assert len(kept) == connection_log.MAX_JOURNAL_ENTRIES
    assert all(set(e) <= {"e", "t", "code", "attempt", "reason"} for e in kept)


def test_a_journal_that_is_not_a_list_reads_as_nothing():
    assert connection_log.clean_journal({"e": "open"}) == []
    assert connection_log.clean_journal(None) == []


# =============================================================================
# The hooks in server.py — the half that no helper test can prove
# =============================================================================

@pytest.mark.asyncio
async def test_a_departing_browser_leaves_one_line_naming_itself_and_the_close_code(
        server, caplog):
    ws, task = await add_browser(server, "ff-a", 101, "Firefox")
    with caplog.at_level(logging.INFO, logger="server.server"):
        ws.drop(code=1006)                    # the browser vanished
        await asyncio.wait_for(task, timeout=2.0)

    lines = [r.getMessage() for r in caplog.records if "disconnected" in r.getMessage()]
    assert len(lines) == 1, f"expected exactly one disconnect line, got {lines}"
    assert "[1] Firefox pid 101" in lines[0]
    assert "close code 1006" in lines[0] and "vanished" in lines[0]
    assert "disconnected after" in lines[0]


@pytest.mark.asyncio
async def test_the_drop_survives_on_the_record_and_reaches_server_info(server):
    """After a reconnect the question is always "why did it go LAST time?" — and by then
    the socket that could answer is closed."""
    ws, task = await add_browser(server, "ff-a", 101, "Firefox")
    ws.drop(code=1006)
    await asyncio.wait_for(task, timeout=2.0)

    ws2, task2 = await add_browser(server, "ff-a", 101, "Firefox")
    row = server.get_connections_info()["extension_instances"][0]
    assert row["last_close_code"] == 1006
    assert row["last_disconnect_hms"]
    assert row["currently_connected"] is True, "it is back; the PREVIOUS drop is the news"

    ws2.drop()
    await asyncio.wait_for(task2, timeout=2.0)


@pytest.mark.asyncio
async def test_the_departed_record_carries_when_and_how_the_browser_went(server):
    ws, task = await add_browser(server, "ff-a", 101, "Firefox")
    ws.drop(code=1006)
    await asyncio.wait_for(task, timeout=2.0)

    gone = server._departed["ff-a"]
    assert str(gone) == "[1] Firefox pid 101"      # still just a string
    assert gone.close_code == 1006
    assert gone.at_hms


@pytest.mark.asyncio
async def test_a_slow_hello_is_measured_and_reported(server, caplog):
    ws = FakeWS(peer=("127.0.0.1", 10101))
    with patch.object(server, "_probe_browser_identity",
                      return_value={"pid": 101, "brand": "Firefox",
                                    "friendly": "Firefox", "is_firefox": True}):
        task = asyncio.create_task(server.handle_extension_connection(ws))
        # The peer probe runs on an executor (finding C7): wait for the registration.
        for _ in range(400):
            if server.registry.browser_for_ws(ws) is not None or task.done():
                break
            await asyncio.sleep(0.005)
    # Backdate the accept so the hello below looks like one sent by a browser under load;
    # the alternative is a test that sleeps for seven seconds.
    server.browsers["pending-1"].accepted_at = time.monotonic() - 7.3

    with caplog.at_level(logging.INFO, logger="server.server"):
        await hello(server, ws, instance_id="ff-a")

    assert 7000 <= server.browsers["ff-a"].hello_latency_ms <= 9000
    assert any("slow, not absent" in r.getMessage() for r in caplog.records)
    assert server.get_connections_info()["extension_instances"][0]["hello_latency_ms"]

    ws.drop()
    await asyncio.wait_for(task, timeout=2.0)


@pytest.mark.asyncio
async def test_a_prompt_hello_is_measured_but_not_logged(server, caplog):
    with caplog.at_level(logging.INFO, logger="server.server"):
        ws, task = await add_browser(server, "ff-a", 101, "Firefox")
    assert server.browsers["ff-a"].hello_latency_ms is not None
    assert not any("slow, not absent" in r.getMessage() for r in caplog.records)
    ws.drop()
    await asyncio.wait_for(task, timeout=2.0)


@pytest.mark.asyncio
async def test_the_periodic_hello_does_not_re_measure_the_latency(server):
    """The extension re-sends its hello every ~30s for the life of the connection. A
    second measurement there would report the age of the CONNECTION as the age of a
    hello, and every long-lived browser would look like a sick one."""
    ws, task = await add_browser(server, "ff-a", 101, "Firefox")
    first = server.browsers["ff-a"].hello_latency_ms
    await asyncio.sleep(0.02)
    await hello(server, ws, instance_id="ff-a", version="1.8.8")
    assert server.browsers["ff-a"].hello_latency_ms == first
    ws.drop()
    await asyncio.wait_for(task, timeout=2.0)


@pytest.mark.asyncio
async def test_the_extensions_journal_is_logged_once_and_kept_for_server_info(
        server, caplog):
    at = int(datetime(2026, 9, 21, 23, 54, 58).timestamp() * 1000)
    journal = [{"t": at, "e": "close", "code": 1006},
               {"t": at + 70_000, "e": "open"},
               {"t": at + 75_000, "e": "rejected", "code": 4401,
                "reason": "hello timeout: no extension_hello within 15s"}]

    ws = FakeWS(peer=("127.0.0.1", 10102))
    with patch.object(server, "_probe_browser_identity",
                      return_value={"pid": 101, "brand": "Firefox",
                                    "friendly": "Firefox", "is_firefox": True}):
        task = asyncio.create_task(server.handle_extension_connection(ws))
        await asyncio.sleep(0.01)
    with caplog.at_level(logging.INFO, logger="server.server"):
        await hello(server, ws, instance_id="ff-a", journal=journal)

    reported = [r.getMessage() for r in caplog.records
                if "reports since its last hello" in r.getMessage()]
    assert len(reported) == 1
    assert "close 23:54:58 code 1006" in reported[0]

    row = server.get_connections_info()["extension_instances"][0]
    assert [e["e"] for e in row["reported_journal"]] == ["close", "open", "rejected"]

    ws.drop()
    await asyncio.wait_for(task, timeout=2.0)


@pytest.mark.asyncio
async def test_a_hello_without_a_journal_logs_nothing_and_keeps_the_previous_one(server):
    """The journal covers the gap since the last hello; a heartbeat hello has no gap to
    report, and must not erase the account of the last one."""
    ws, task = await add_browser(server, "ff-a", 101, "Firefox",
                                 journal=[{"e": "open", "t": 1_700_000_000_000}])
    await hello(server, ws, instance_id="ff-a", version="1.8.8")
    assert [e["e"] for e in server.browsers["ff-a"].reported_journal] == ["open"]
    ws.drop()
    await asyncio.wait_for(task, timeout=2.0)


@pytest.mark.asyncio
async def test_a_hostile_journal_cannot_grow_the_log_or_the_roster(server):
    ws, task = await add_browser(
        server, "ff-a", 101, "Firefox",
        journal=[{"e": "retry" * 50, "reason": "x" * 5000} for _ in range(200)])
    kept = server.browsers["ff-a"].reported_journal
    assert len(kept) == connection_log.MAX_JOURNAL_ENTRIES
    assert len(kept[0]["e"]) <= connection_log.MAX_JOURNAL_EVENT
    assert len(kept[0]["reason"]) <= connection_log.MAX_JOURNAL_REASON
    ws.drop()
    await asyncio.wait_for(task, timeout=2.0)


# =============================================================================
# The hello timeout, end to end
# =============================================================================

@pytest.mark.asyncio
async def test_a_socket_that_never_says_hello_is_closed_with_a_hello_timeout_reason():
    """The rejection the wedge produced twice. It came back "token required: GET …",
    which sent a browser that was already struggling off to refetch a token it had."""
    srv = make_token_server(ws_token=mint())
    srv.ws_auth_timeout = 0.05
    ws = TokenFakeWS()

    task = await token_connect(srv, ws)
    await asyncio.wait_for(task, timeout=2.0)

    assert srv.browsers == {}
    assert ws.close_code == 4401, "the code stays 4401; the REASON is what changed"
    assert ws.close_reason.startswith("hello timeout:")
    assert "token" not in ws.close_reason.lower()


@pytest.mark.asyncio
async def test_a_token_rejection_still_says_token_required():
    """The other half of the same discrimination: an extension whose handshake proves a
    token this server did not issue must still be told to go and fetch the right one."""
    from secure_ext import failed_handshake
    from browserplug_shared import device_tokens
    srv = make_token_server(ws_token=mint())
    ws = TokenFakeWS()

    task = await token_connect(srv, ws)
    await failed_handshake(ws, device_tokens.issue(mint()))
    await asyncio.wait_for(task, timeout=5.0)

    assert ws.close_code == 4401
    assert ws.close_reason.startswith("token required")


@pytest.mark.asyncio
async def test_a_plaintext_token_is_refused_without_directions_to_fetch_one():
    """An extension older than 1.9 sends its token in a plaintext hello. The refusal
    names no endpoint: fetching a token would only put it on the wire in the clear again."""
    srv = make_token_server(ws_token=mint())
    ws = TokenFakeWS()

    task = await token_connect(srv, ws)
    ws.feed({"type": "extension_hello", "data": {"token": srv.ws_token, "instanceId": "ff-a"}})
    await asyncio.wait_for(task, timeout=2.0)

    assert ws.close_code == 4401
    assert ws.close_reason.startswith("encryption required")
    assert "http" not in ws.close_reason
    assert srv.browsers == {}


def test_the_default_hello_window_is_fifteen_seconds():
    """Five seconds was too short for a browser under memory pressure — twice, eight
    minutes apart, while a cold start passed the same handshake in three."""
    assert ws_auth.WS_AUTH_TIMEOUT_SECONDS == 15.0
    assert ws_auth.ws_auth_timeout_seconds({}) == 15.0


def test_the_hello_window_is_overridable_and_clamped():
    env = ws_auth.WS_AUTH_TIMEOUT_ENV
    assert ws_auth.ws_auth_timeout_seconds({env: "30"}) == 30.0
    assert ws_auth.ws_auth_timeout_seconds({env: "0.1"}) == ws_auth.WS_AUTH_TIMEOUT_MIN
    assert ws_auth.ws_auth_timeout_seconds({env: "600"}) == ws_auth.WS_AUTH_TIMEOUT_MAX


def test_a_garbage_hello_window_reads_as_the_default_rather_than_refusing_to_start():
    """A typo in an env var must never be the reason a browser cannot connect."""
    env = ws_auth.WS_AUTH_TIMEOUT_ENV
    assert ws_auth.ws_auth_timeout_seconds({env: "soon"}) == 15.0
    assert ws_auth.ws_auth_timeout_seconds({env: ""}) == 15.0


@pytest.mark.asyncio
async def test_the_timeout_rejection_names_the_knob_that_answers_it(caplog):
    srv = make_token_server(ws_token=mint())
    srv.ws_auth_timeout = 0.05
    ws = TokenFakeWS()

    with caplog.at_level(logging.WARNING, logger="server.server"):
        task = await token_connect(srv, ws)
        await asyncio.wait_for(task, timeout=2.0)

    rejected = [r.getMessage() for r in caplog.records if "Rejected" in r.getMessage()]
    assert rejected and "BROWSERPLUG_WS_AUTH_TIMEOUT" in rejected[0]
    assert "memory or CPU pressure" in rejected[0]
    assert "requires a token" not in rejected[0], "this one is not about the token"
