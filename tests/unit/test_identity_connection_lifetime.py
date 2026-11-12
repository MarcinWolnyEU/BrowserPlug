"""A client whose connection closes with its response is still found in the TCP table.

The server names the process behind an MCP session by the source port of a request: it
reads the machine's TCP table for the socket with that local port and walks up from its
owner. The stdio proxy and the relay both open a fresh connection for every request and
drop it as soon as the response is in, so the connection of a session's first request is
gone a few milliseconds after the server answers it. The lookup runs on a worker thread
and reads a table that takes a few milliseconds on Windows and longer on a loaded Mac, so
it used to read a table with no such socket in it. The miss was then filed as an answer:
the header already named the client, so the session was marked identified with no
launcher, and no later request looked again. test_proxy_e2e failed on a GitHub macOS
runner because the connection map never drew the launcher, and failed in most runs of
the old code on Windows too.

Here the table is modelled: a peer's socket is in it only while its request is being
answered, a read takes as long as the test says, and it returns the table as it is when
the read ends. HttpEndpoints, client_identity and the process walk are the production
code, over stand-in processes; only the machine's tables are faked.

The tests assert on what a request waited for (the recorded waits below) and on the order
of events, never on how long anything took: a hosted 3-vCPU runner can stall a thread hop
for longer than any bound that would also catch a regression. Where a lookup has to be
slow, a gate (threading.Event) holds it until the test opens it. The wait budget is set
high wherever the awaited event is meant to arrive in time, so a slow scheduler cannot
turn it into a timeout; the tests of a bounded wait set it low and rely on a gate that
stays shut.
"""

import asyncio
import sys
import threading
import time
import types
from concurrent.futures import ThreadPoolExecutor

import pytest

from browserplug_shared import socket_table
from browserplug_shared.version import CLIENT_PID_HEADER
from server.reporting import client_identity
from server.web import http_endpoints

from test_client_identity import _proxy_chain
from test_http_endpoints import CALL, _echo_app, _mcp_post, _server
from wait_helper import wait_until_async

PROXY_PID = 5300        # the proxy child: the process that owns the peer's socket
LAUNCHER_PID = 5100     # what the connection map must draw as the launcher hop
CLIENT_PID = 5000       # the client the launcher forwards in X-BrowserPlug-Client-Pid
GATE_CAP = 30.0         # a gate nobody opens frees its thread after this, so a failed test ends
GENEROUS_WAIT_SECONDS = 30.0


@pytest.fixture(autouse=True)
def _own_identity_pool(monkeypatch):
    """A pool of the test's own, as in test_http_endpoints: a lookup that outlives its test
    must not run on after the stand-ins are undone, nor hold a worker of the shared pool.
    The teardown joins every worker, so a lookup is finished before the next test starts."""
    pool = ThreadPoolExecutor(max_workers=http_endpoints.IDENTITY_WORKERS)
    monkeypatch.setattr(http_endpoints, "_identity_pool", pool)
    yield
    pool.shutdown(wait=True, cancel_futures=True)


class ShortLivedConnections:
    """A TCP table in which a peer's socket exists only while its request is answered.

    `behaviour` lists how each read goes, one entry per read, the last repeated: a number
    of seconds the read takes (the thread hop plus the walk on a slow or loaded machine), or
    a threading.Event gate the read waits for. The rows are those of the moment the read
    ENDS, as with a real table that is read while connections come and go.
    """

    def __init__(self, behaviour, visible=True):
        self.behaviour = list(behaviour)
        self.visible = visible      # False: no row is ever readable (another account's peer)
        self.open_ports = set()
        self.reads = 0

    def connect(self, port):
        self.open_ports.add(port)

    def close(self, port):
        self.open_ports.discard(port)

    def release(self):
        """Open every gate, so no read is left waiting."""
        for item in self.behaviour:
            if isinstance(item, threading.Event):
                item.set()

    def __call__(self, only_pid=None):
        item = self.behaviour[min(self.reads, len(self.behaviour) - 1)]
        self.reads += 1
        if isinstance(item, threading.Event):
            item.wait(GATE_CAP)
        else:
            time.sleep(item)
        if not self.visible:
            return []
        return [types.SimpleNamespace(
            laddr=types.SimpleNamespace(ip="127.0.0.1", port=port),
            raddr=types.SimpleNamespace(ip="127.0.0.1", port=2830),
            status=socket_table.CONN_ESTABLISHED, pid=PROXY_PID)
            for port in sorted(self.open_ports)]


@pytest.fixture
def machine(monkeypatch):
    """A server over a modelled TCP table and a stand-in process tree (the proxy
    transport's chain), as the macOS runner has it: the owner gate does no table read there
    (sees_other_users is False), so only the identity lookup reads the table."""
    server = _server()
    procs = {}
    _proxy_chain(procs)
    psutil = types.ModuleType("psutil")
    psutil.Process = lambda pid: procs[pid]
    monkeypatch.setitem(sys.modules, "psutil", psutil)
    monkeypatch.setattr(socket_table, "sees_other_users", lambda: False)
    monkeypatch.setattr(client_identity, "describe_pid",
                        lambda pid: {"pid": pid, "friendly": f"process {pid}",
                                     "start_time": f"started-{pid}"})
    monkeypatch.setattr(http_endpoints, "IDENTITY_WAIT_SECONDS", GENEROUS_WAIT_SECONDS)
    box = types.SimpleNamespace(server=server, table=None,
                                app=server._wrap_client_tracking(_echo_app([])))

    def use(table):
        monkeypatch.setattr(socket_table, "tcp_connections", table)
        box.table = table
        return box

    box.use = use
    yield box
    if box.table is not None:
        box.table.release()      # a test that failed before it opened its gates


class RecordedWaits:
    """http_endpoints' `asyncio` with wait_for recorded: when each wait began, the timeout it
    was given, and whether it ran out. What a request was held for, without a clock."""

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        return getattr(asyncio, name)

    async def wait_for(self, awaitable, timeout):
        call = types.SimpleNamespace(start=asyncio.get_running_loop().time(),
                                     timeout=timeout, timed_out=False)
        self.calls.append(call)
        try:
            return await asyncio.wait_for(awaitable, timeout)
        except TimeoutError:
            call.timed_out = True
            raise


@pytest.fixture
def waits(monkeypatch):
    recorded = RecordedWaits()
    monkeypatch.setattr(http_endpoints, "asyncio", recorded)
    return recorded


async def _request(box, sid, port, body=None):
    """One request on a connection of its own, which the client closes right after the
    response, as the proxy's httpx client and the relay's urllib do."""
    box.table.connect(port)
    forwarded = [(CLIENT_PID_HEADER.lower().encode(), str(CLIENT_PID).encode())]
    options = {} if body is None else {"body": body}
    scope, receive, send, _ = _mcp_post(sid, peer=("127.0.0.1", port), extra=forwarded,
                                        **options)

    async def send_then_hang_up(message):
        await send(message)
        if message["type"] == "http.response.body" and not message.get("more_body"):
            box.table.close(port)

    await box.app(scope, receive, send_then_hang_up)
    box.table.close(port)


async def _lookup_ended(box, sid, timeout=GATE_CAP):
    def ended():
        return not box.server.http_clients[sid].get("identity_pending")

    await wait_until_async(ended, f"the identity lookup for {sid} ended", timeout=timeout)
    return box.server.http_clients[sid]


async def _finish(box, *gates):
    """Open the gates and wait for every lookup to end, so a test leaves nothing running."""
    box.table.release()
    for gate in gates:
        gate.set()
    for sid in list(box.server.http_clients):
        await _lookup_ended(box, sid)


@pytest.mark.asyncio
async def test_the_launcher_is_found_when_the_table_is_read_after_the_connection_closed(machine):
    """The case the macOS runner is in: the read is slower than the connection lives. The
    request that starts the lookup is held until the read is over, so the socket is there."""
    box = machine.use(ShortLivedConnections([0.2]))
    await _request(box, "s1", 7001)
    entry = await _lookup_ended(box, "s1")
    assert (entry["pid"], entry["launcher_pid"]) == (CLIENT_PID, LAUNCHER_PID), (
        "the session was filed with no launcher: the table was read after its socket closed")
    assert entry["launcher_start_time"] == f"started-{LAUNCHER_PID}"
    assert entry["identified"] is True
    assert box.table.reads == 1


@pytest.mark.asyncio
async def test_only_the_table_read_is_waited_for_not_the_process_walk(machine, waits,
                                                                      monkeypatch):
    """The walk needs pids, not the connection. Holding the first request of every session
    for it would add its whole cost to every request of the proxy transport. The walk is
    gated and stays shut while the request is served: the request's one wait must end
    because the read finished, not because it ran out."""
    walk = threading.Event()

    def gated_describe(pid):
        walk.wait(GATE_CAP)
        return {"pid": pid, "friendly": "slow", "start_time": f"started-{pid}"}

    monkeypatch.setattr(client_identity, "describe_pid", gated_describe)
    box = machine.use(ShortLivedConnections([0.05]))
    try:
        await _request(box, "s1", 7001)
        assert len(waits.calls) == 1 and waits.calls[0].timed_out is False, (
            "the request waited for the walk: its wait ran out")
        assert box.server.http_clients["s1"]["identity_pending"] is True
    finally:
        await _finish(box, walk)
    assert box.server.http_clients["s1"]["launcher_pid"] == LAUNCHER_PID


@pytest.mark.asyncio
async def test_a_read_that_outlasts_the_wait_is_repeated_by_the_sessions_next_request(
        machine, monkeypatch):
    """The wait is bounded (a table that takes seconds must not hold a client for seconds).
    The first lookup then reads a closed connection's table and finds nothing; the session's
    next request has a socket of its own, open while it is served, and looks again. The first
    read is gated, so it ends after the request has returned however fast the machine is."""
    monkeypatch.setattr(http_endpoints, "IDENTITY_WAIT_SECONDS", 0.05)
    first_read = threading.Event()
    box = machine.use(ShortLivedConnections([first_read, 0.0]))
    await _request(box, "s1", 7001)
    first_read.set()
    entry = await _lookup_ended(box, "s1")
    assert entry["launcher_pid"] is None and entry["identified"] is False, (
        "a miss must stay open: the header named the client but not the launcher")
    assert entry["pid"] == CLIENT_PID
    # The repeat's read is instant; the short budget above is not needed for it, and a
    # starved thread hop must not turn it into another miss.
    monkeypatch.setattr(http_endpoints, "IDENTITY_WAIT_SECONDS", GENEROUS_WAIT_SECONDS)
    await _request(box, "s1", 7002)
    entry = await _lookup_ended(box, "s1")
    assert entry["launcher_pid"] == LAUNCHER_PID and entry["identified"] is True
    assert box.table.reads == 2


@pytest.mark.asyncio
async def test_a_peer_that_never_shows_in_the_table_costs_a_bounded_number_of_reads(machine):
    """Another account's process on macOS, for one. The session keeps the forwarded pid
    and stops asking after the retries; its requests are not held once it has settled."""
    box = machine.use(ShortLivedConnections([0.0], visible=False))
    for n in range(1 + client_identity.IDENTITY_MISS_RETRIES + 3):
        await _request(box, "s1", 7001 + n)
        await _lookup_ended(box, "s1")
    entry = box.server.http_clients["s1"]
    assert box.table.reads == 1 + client_identity.IDENTITY_MISS_RETRIES
    assert entry["pid"] == CLIENT_PID and entry["launcher_pid"] is None
    assert entry["identified"] is True


@pytest.mark.asyncio
async def test_a_session_already_identified_is_never_held(machine, waits):
    box = machine.use(ShortLivedConnections([0.05]))
    await _request(box, "s1", 7001)
    await _lookup_ended(box, "s1")
    waited = len(waits.calls)
    assert waited == 1
    await _request(box, "s1", 7002)
    assert len(waits.calls) == waited, "the second request waited for nothing"
    assert box.table.reads == 1


@pytest.mark.asyncio
async def test_the_wait_for_the_table_read_is_bounded(machine, waits, monkeypatch):
    """The read is gated and stays shut: the request can only return because its wait ran
    out, and the lookup is still running when it does."""
    monkeypatch.setattr(http_endpoints, "IDENTITY_WAIT_SECONDS", 0.2)
    box = machine.use(ShortLivedConnections([threading.Event()]))
    try:
        await _request(box, "s1", 7001)
        assert len(waits.calls) == 1 and waits.calls[0].timed_out is True
        # The timeout is the deadline minus the loop clock, which float rounding can leave a
        # few picoseconds over the budget.
        assert waits.calls[0].timeout <= http_endpoints.IDENTITY_WAIT_SECONDS + 1e-3
        assert box.server.http_clients["s1"]["identity_pending"] is True
    finally:
        await _finish(box)


@pytest.mark.asyncio
async def test_a_request_is_held_for_one_budget_however_many_waits_it_makes(
        machine, waits, monkeypatch):
    """The first request of a session, when it is a tools/call, waits for the table read
    and then for the identity. Both arrive late here: the read is gated and opened a moment
    into the budget, the walk stays shut. Every wait must end by the time the first one
    began plus the budget, so the request is held IDENTITY_WAIT_SECONDS in total and not
    twice that. This compares the waits' own start times and timeouts, so a slow scheduler
    can delay a wait but cannot move its deadline. With a budget per wait, the second would
    end at least the read's delay (0.1 s) past the first one's deadline."""
    budget = 0.4
    monkeypatch.setattr(http_endpoints, "IDENTITY_WAIT_SECONDS", budget)
    read, walk = threading.Event(), threading.Event()

    def gated_describe(pid):
        walk.wait(GATE_CAP)
        return {"pid": pid, "friendly": "slow", "start_time": f"started-{pid}"}

    monkeypatch.setattr(client_identity, "describe_pid", gated_describe)
    box = machine.use(ShortLivedConnections([read]))
    asyncio.get_running_loop().call_later(0.1, read.set)
    try:
        await _request(box, "s1", 7001, body=CALL)
        assert waits.calls, "the request made no wait on its lookup"
        first = waits.calls[0].start
        for call in waits.calls:
            assert call.start + call.timeout <= first + budget + 1e-3, (
                f"a wait ran past the request's one budget: {waits.calls}")
        assert box.server.http_clients["s1"]["identity_pending"] is True
    finally:
        await _finish(box, walk)
