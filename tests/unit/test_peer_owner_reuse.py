"""S3 follow-up: the owner verdict is cached per peer port, and a port can change hands.

A short-lived local client (a launcher, curl) connects from port P and exits. Within the
cache window another account's process is given the same ephemeral port. The cached
"not foreign" verdict must not carry over: it belongs to the process, so it is dropped when
that process is gone or is a different one (pid reuse, seen as a new create_time).
The process table is faked, as in test_peer_owner_gate.py.
"""

import sys
import types

import pytest

from server.reporting import client_identity
from server.web import peer_owner

ME = "desktop-marcin"
OTHER = "desktop-guest"


class Table:
    """port -> pid, and pid -> (owner, create_time); a pid missing from `procs` is dead."""

    def __init__(self):
        self.ports = {}
        self.procs = {}
        self.lookups = []


@pytest.fixture
def table(monkeypatch):
    t = Table()

    class NoSuchProcess(Exception):
        pass

    class FakeProc:
        def __init__(self, pid=None):
            self.pid = pid
            if pid is not None and pid not in t.procs:
                raise NoSuchProcess(pid)

        def username(self):
            return ME if self.pid is None else t.procs[self.pid][0]

        def create_time(self):
            return t.procs[self.pid][1]

    def pid_for_port(port):
        t.lookups.append(port)
        return t.ports.get(port)

    monkeypatch.setattr(client_identity, "pid_for_local_port", pid_for_port)
    monkeypatch.setattr(peer_owner, "_server_owner", None)
    monkeypatch.setitem(sys.modules, "psutil", types.SimpleNamespace(
        Process=FakeProc, NoSuchProcess=NoSuchProcess))
    return t


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.mark.asyncio
async def test_a_port_taken_over_by_another_process_is_looked_up_again(table):
    clock = Clock()
    gate = peer_owner.PeerOwnerGate(clock=clock)
    table.ports[5000] = 11
    table.procs[11] = (ME, 100.0)
    assert await gate.is_foreign(5000) is False

    # The first process exits; another account's process now holds the same port.
    del table.procs[11]
    table.ports[5000] = 22
    table.procs[22] = (OTHER, 200.0)
    clock.now += 1.0                      # well inside the cache window
    assert await gate.is_foreign(5000) is True


@pytest.mark.asyncio
async def test_a_reused_pid_with_a_new_start_time_is_looked_up_again(table):
    clock = Clock()
    gate = peer_owner.PeerOwnerGate(clock=clock)
    table.ports[5000] = 11
    table.procs[11] = (ME, 100.0)
    assert await gate.is_foreign(5000) is False

    table.procs[11] = (OTHER, 300.0)      # same pid, a different process
    clock.now += 1.0
    assert await gate.is_foreign(5000) is True


@pytest.mark.asyncio
async def test_a_live_unchanged_peer_is_not_looked_up_again(table):
    clock = Clock()
    gate = peer_owner.PeerOwnerGate(clock=clock)
    table.ports[5000] = 11
    table.procs[11] = (ME, 100.0)
    for _ in range(5):
        assert await gate.is_foreign(5000) is False
        clock.now += 0.5
    assert table.lookups == [5000]


@pytest.mark.asyncio
async def test_the_cache_still_expires(table):
    clock = Clock()
    gate = peer_owner.PeerOwnerGate(clock=clock)
    table.ports[5000] = 11
    table.procs[11] = (ME, 100.0)
    await gate.is_foreign(5000)
    clock.now += peer_owner.CACHE_SECONDS + 0.1
    await gate.is_foreign(5000)
    assert table.lookups == [5000, 5000]
