"""A fake process table for tests whose client pids are made-up numbers.

routing.pid_client_key appends the OS start time ("pid:N@<epoch>") whenever
routing.process_started finds a running process. A made-up pid such as 777 is a real
process on some machines (a conhost.exe, say), so a test asserting
`browser_selection["pid:777"]` passes or fails depending on what happens to be running.

Import `fake_process_table` into a test module to shield every test in it: no pid is
running until the test registers a start time for it,

    from fake_process_table import fake_process_table  # noqa: F401

    def test_x(fake_process_table):          # request it by name to register a start
        fake_process_table[777] = 1_700_000_000
"""

import pytest

from server.browsers import routing


@pytest.fixture(autouse=True)
def fake_process_table(monkeypatch):
    """pid -> start epoch. Empty, so every pid reports None (not running)."""
    table = {}
    monkeypatch.setattr(routing, "process_started", lambda pid: table.get(int(pid)))
    return table
