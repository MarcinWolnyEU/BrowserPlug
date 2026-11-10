"""browserplug_shared.socket_table: the one TCP-table call behind the server's port-to-pid lookups.

psutil's table is used wherever psutil can read it; on macOS, where it raises AccessDenied
for a non-root user, the table of the user's own processes (darwin_procs) answers instead.
The probe is a fake here, so the policy is tested on every OS; the real macOS table is
compared with psutil in test_darwin_procs.py.
"""

import sys

import psutil
import pytest

from browserplug_shared import darwin_procs, proc_facts, socket_table


class FakeProbe:
    def __init__(self, rows):
        self.rows = rows
        self.asked = []

    def connections(self, pids=None):
        self.asked.append(pids)
        return self.rows


ROW = darwin_procs.Conn(2, darwin_procs.Addr("127.0.0.1", 2830), (), "LISTEN", 42)


def denied(kind="inet"):
    raise psutil.AccessDenied()


def test_the_status_names_are_psutils_so_callers_need_no_psutil_constants():
    assert socket_table.CONN_LISTEN == psutil.CONN_LISTEN
    assert socket_table.CONN_ESTABLISHED == psutil.CONN_ESTABLISHED


def test_psutils_table_is_used_when_psutil_can_read_it(monkeypatch):
    seen = []
    monkeypatch.setattr(psutil, "net_connections", lambda kind="inet": seen.append(kind) or ["p"])
    monkeypatch.setattr(proc_facts, "darwin_probe", lambda: pytest.fail("the native table"))
    assert socket_table.tcp_connections() == ["p"]
    assert seen == ["inet"]


def test_a_refused_psutil_falls_back_to_the_native_table(monkeypatch):
    probe = FakeProbe([ROW])
    monkeypatch.setattr(psutil, "net_connections", denied)
    monkeypatch.setattr(proc_facts, "darwin_probe", lambda: probe)
    assert socket_table.tcp_connections() == [ROW]
    assert probe.asked == [None]


def test_one_pid_limits_the_native_walk_and_only_the_native_one(monkeypatch):
    probe = FakeProbe([ROW])
    monkeypatch.setattr(psutil, "net_connections", denied)
    monkeypatch.setattr(proc_facts, "darwin_probe", lambda: probe)
    socket_table.tcp_connections(only_pid=77)
    assert probe.asked == [[77]]


def test_a_refusal_with_no_native_table_is_the_original_error(monkeypatch):
    monkeypatch.setattr(psutil, "net_connections", denied)
    monkeypatch.setattr(proc_facts, "darwin_probe", lambda: None)
    with pytest.raises(psutil.AccessDenied):
        socket_table.tcp_connections()


def test_any_other_psutil_error_is_not_swallowed_into_the_native_table(monkeypatch):
    def broken(kind="inet"):
        raise OSError("table unreadable")
    monkeypatch.setattr(psutil, "net_connections", broken)
    monkeypatch.setattr(proc_facts, "darwin_probe", lambda: pytest.fail("the native table"))
    with pytest.raises(OSError, match="table unreadable"):
        socket_table.tcp_connections()


def test_without_psutil_the_native_table_answers_where_there_is_one(monkeypatch):
    probe = FakeProbe([ROW])
    monkeypatch.setitem(sys.modules, "psutil", None)         # import psutil -> ImportError
    monkeypatch.setattr(proc_facts, "darwin_probe", lambda: probe)
    assert socket_table.tcp_connections() == [ROW]


def test_without_psutil_and_without_a_native_table_it_is_an_import_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "psutil", None)
    monkeypatch.setattr(proc_facts, "darwin_probe", lambda: None)
    with pytest.raises(ImportError):
        socket_table.tcp_connections()


def test_the_probe_is_only_ever_built_on_macOS(monkeypatch):
    monkeypatch.setattr(proc_facts.sys, "platform", "linux")
    monkeypatch.setattr(proc_facts, "_DARWIN_PROBE_CACHE", None)
    assert proc_facts.darwin_probe() is None


@pytest.mark.parametrize("platform,euid,expected", [
    ("win32", None, True), ("linux", 1000, True), ("darwin", 0, True), ("darwin", 501, False)])
def test_only_a_non_root_mac_table_is_blind_to_other_users(monkeypatch, platform, euid, expected):
    monkeypatch.setattr(socket_table.sys, "platform", platform)
    monkeypatch.setattr(socket_table.os, "geteuid", lambda: euid, raising=False)
    assert socket_table.sees_other_users() is expected
