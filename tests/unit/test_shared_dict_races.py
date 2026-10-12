"""Cross-thread shared tables (review A-F3 leftovers, chain X3).

`http_clients` is filled on the uvicorn thread (track_http_client, per MCP request) and
read on report-building threads and the WebSocket thread (/health, get_connections). The
health report used to walk its live `.values()` view inside a comprehension, so an insert
on the other thread during the walk raised "dictionary changed size during iteration" and
failed the whole report.
"""
import threading
import time
from datetime import datetime
from unittest.mock import patch

import pytest

from server.reporting import client_identity
from server.server import BrowserPlugServer


@pytest.fixture
def server():
    return BrowserPlugServer(host="localhost", port=8772, start_mcp=False)


class _SlowStamp:
    """A last_activity whose timestamp() yields the GIL, so the thread inserting sessions
    runs in the middle of a walk of the table, as it does on a busy server."""

    def __init__(self):
        self._real = datetime.now()

    def timestamp(self):
        time.sleep(0.0005)
        return self._real.timestamp()


def _entry(pid):
    now = datetime.now()
    return {"pid": pid, "friendly": "x", "last_activity": _SlowStamp(), "first_seen": now,
            "history": []}


def test_health_survives_sessions_being_added_meanwhile(server):
    for i in range(40):
        server.http_clients[f"seed-{i}"] = _entry(1000 + i)
    # The connection roster is a separate, slow report; only the pid list is under test.
    server.get_connections_info = lambda *a, **k: {}
    stop = threading.Event()
    failures = []

    def writer():
        for n in range(150):
            if stop.is_set():
                break
            server.http_clients[f"new-{n}"] = _entry(50000 + n)
            time.sleep(0.0003)

    t = threading.Thread(target=writer)
    t.start()
    try:
        with patch("psutil.net_connections", return_value=[]):
            for _ in range(5):
                try:
                    server.get_health_info()
                except RuntimeError as e:  # dictionary changed size during iteration
                    failures.append(str(e))
                    break
    finally:
        stop.set()
        t.join()
    assert failures == []


def test_two_first_requests_of_one_session_keep_one_entry(server):
    """The describe step is slow (psutil walks), so two concurrent first requests both saw
    'no entry'; the second overwrote the first's entry and dropped its history."""
    gate = threading.Barrier(2)

    def slow_chain(_port):
        try:
            gate.wait(timeout=2)
        except threading.BrokenBarrierError:
            pass
        return 4242, None

    def run(entry_sink):
        client_identity.track_http_client(server.http_clients, "sid-1", ("127.0.0.1", 5000))
        entry_sink.append(server.http_clients["sid-1"])

    seen = []
    with patch.object(client_identity, "resolve_client_chain", slow_chain), \
            patch.object(client_identity, "describe_pid", lambda pid: {"pid": pid}):
        threads = [threading.Thread(target=run, args=(seen,)) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    assert len(seen) == 2 and seen[0] is seen[1]


def test_launcher_version_pruning_survives_concurrent_health_polls():
    """/health runs on worker threads, one per polling launcher, and each call records its
    launcher's version and prunes the stale ones by walking the same table."""
    table = {}
    errors = []

    def poll(base):
        try:
            for i in range(600):
                client_identity.record_launcher_version(table, base + i, "1.0")
        except RuntimeError as e:
            errors.append(str(e))

    threads = [threading.Thread(target=poll, args=(k * 100000,)) for k in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []


def test_pruning_a_selection_another_call_just_pruned_does_not_raise():
    from server.browsers import routing

    class Racy(dict):
        def items(self):
            snapshot = list(super().items())
            self.clear()  # the other call prunes the same stale key first
            return snapshot

    routing.prune_selection(Racy({"pid:1@2": "gone-browser"}), {}, [])
