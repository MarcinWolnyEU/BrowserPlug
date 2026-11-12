"""browserplug_shared.darwin_procs: process and socket facts on macOS from libproc and sysctl.

The parser tests build their buffers from the module's own offset constants, so they check the
decoding and the edge cases on any OS but say nothing about where the kernel puts a field. Only
the live tests, which run on a Mac and compare every reading with psutil on real processes and
sockets, prove the layout and would catch one that a future macOS moves.
"""

import errno
import os
import signal
import socket
import struct
import subprocess
import sys
import time

import pytest

from browserplug_shared import darwin_procs, proc_facts
from wait_helper import wait_until

DARWIN = sys.platform == "darwin"
REAL_PYTHON = getattr(sys, "_base_executable", None) or sys.executable


# ---- the parsers, over fixture bytes ---------------------------------------------------------

def procargs(argc, exe, *rest, padding=3, tail=b"HOME=/x\0"):
    """A KERN_PROCARGS2 buffer: argc, the executable path, NUL padding, the arguments."""
    body = struct.pack("=i", argc) + exe + b"\0" * padding
    return body + b"".join(arg + b"\0" for arg in rest) + tail


class TestProcargs:

    def test_the_executable_and_the_arguments_are_split(self):
        raw = procargs(3, b"/usr/bin/python3", b"python3", b"-c", b"print(1)")
        assert darwin_procs.parse_procargs(raw) == (
            "/usr/bin/python3", ["python3", "-c", "print(1)"])

    def test_the_environment_after_the_arguments_is_not_argv(self):
        raw = procargs(1, b"/bin/sh", b"sh", tail=b"A=1\0B=2\0")
        assert darwin_procs.parse_procargs(raw)[1] == ["sh"]

    def test_an_empty_argument_stays_an_empty_string(self):
        raw = procargs(4, b"/bin/x", b"x", b"", b"two words", b"")
        assert darwin_procs.parse_procargs(raw)[1] == ["x", "", "two words", ""]

    def test_no_arguments_is_an_empty_argv(self):
        assert darwin_procs.parse_procargs(procargs(0, b"/bin/x")) == ("/bin/x", [])

    def test_the_executable_may_differ_from_argv0(self):
        raw = procargs(1, b"/env/bin/python", b"python")
        assert darwin_procs.parse_procargs(raw) == ("/env/bin/python", ["python"])

    def test_non_ascii_bytes_decode_as_the_filesystem_does(self):
        raw = procargs(2, b"/bin/x", b"x", "café".encode())
        assert darwin_procs.parse_procargs(raw)[1] == ["x", "café"]

    def test_a_buffer_cut_short_gives_the_arguments_it_has(self):
        raw = struct.pack("=i", 3) + b"/bin/x\0\0" + b"x\0half"
        assert darwin_procs.parse_procargs(raw)[1] == ["x", "half"]

    @pytest.mark.parametrize("raw", [b"", b"\0\0\0", struct.pack("=i", -1) + b"/x\0",
                                     struct.pack("=i", 1) + b"no terminator"])
    def test_garbage_is_an_error(self, raw):
        with pytest.raises(OSError):
            darwin_procs.parse_procargs(raw)


def bsdinfo(pid=4242, ppid=1, uid=501, ruid=501, status=3, comm=b"python3", name=b"",
            started=(1_700_000_000, 250_000)):
    raw = struct.pack("=12I16s32s6I2Q", 0, status, 0, pid, ppid, uid, 20, ruid, 20, uid, 20, 0,
                      comm, name, 9, 4242, 0, 0, 4242, 0, *started)
    assert len(raw) == darwin_procs._BSDINFO_SIZE
    return raw


class TestBsdInfo:

    def test_the_real_uid_not_the_effective_one_is_the_owner(self):
        info = darwin_procs.parse_bsdinfo(bsdinfo(uid=0, ruid=501))
        assert (info.uid, info.ruid) == (0, 501)

    def test_pid_parent_status_and_name(self):
        info = darwin_procs.parse_bsdinfo(bsdinfo(pid=7, ppid=3, status=5, name=b"a long name"))
        assert (info.pid, info.ppid, info.status, info.name) == (7, 3, 5, "a long name")

    def test_the_short_command_name_is_used_when_there_is_no_long_one(self):
        assert darwin_procs.parse_bsdinfo(bsdinfo(comm=b"zsh")).name == "zsh"

    def test_the_start_time_is_epoch_seconds_with_microseconds(self):
        info = darwin_procs.parse_bsdinfo(bsdinfo(started=(1_700_000_000, 250_000)))
        assert info.started == pytest.approx(1_700_000_000.25)

    def test_a_short_buffer_is_an_error(self):
        with pytest.raises(OSError):
            darwin_procs.parse_bsdinfo(bsdinfo()[:100])


def sockinfo(*, kind=2, family=2, vflag=1, lport=2830, fport=0, laddr=b"\x7f\0\0\x01",
             faddr=b"\0\0\0\0", state=1, size=800):
    """A PROC_PIDFDSOCKETINFO buffer holding one socket. An IPv4 address sits in the last
    four bytes of its 16-byte union field."""
    buf = bytearray(size)
    struct.pack_into("=i", buf, darwin_procs._SI_FAMILY, family)
    struct.pack_into("=i", buf, darwin_procs._SI_KIND, kind)
    struct.pack_into("=ii", buf, darwin_procs._IN_FPORT,
                     socket.htons(fport) if fport else 0, socket.htons(lport))
    buf[darwin_procs._IN_VFLAG] = vflag
    for offset, addr in ((darwin_procs._IN_LADDR, laddr), (darwin_procs._IN_FADDR, faddr)):
        if len(addr) == 4:
            buf[offset + 12:offset + 16] = addr
        else:
            buf[offset:offset + 16] = addr
    struct.pack_into("=i", buf, darwin_procs._TCP_STATE, state)
    return bytes(buf)


class TestSocketInfo:

    def test_an_ipv4_listener(self):
        conn = darwin_procs.parse_socket_fdinfo(sockinfo())
        assert conn.laddr == ("127.0.0.1", 2830) and conn.raddr == ()
        assert conn.status == "LISTEN" and conn.pid is None

    def test_an_established_ipv4_connection_has_both_ends(self):
        conn = darwin_procs.parse_socket_fdinfo(sockinfo(
            lport=51000, fport=2830, faddr=b"\x7f\0\0\x01", state=4))
        assert conn.laddr == ("127.0.0.1", 51000)
        assert conn.raddr == ("127.0.0.1", 2830)
        assert conn.status == "ESTABLISHED"

    def test_an_ipv6_loopback_listener(self):
        loopback = b"\0" * 15 + b"\x01"
        conn = darwin_procs.parse_socket_fdinfo(sockinfo(
            family=30, vflag=2, laddr=loopback, faddr=b"\0" * 16))
        assert conn.laddr == ("::1", 2830) and conn.family == socket.AF_INET6

    def test_a_dual_stack_wildcard_reads_as_the_ipv6_wildcard(self):
        conn = darwin_procs.parse_socket_fdinfo(sockinfo(
            family=30, vflag=3, laddr=b"\0" * 16, faddr=b"\0" * 16))
        assert conn.laddr == ("::", 2830)

    def test_an_ipv4_peer_of_a_dual_stack_socket_reads_as_ipv4(self):
        conn = darwin_procs.parse_socket_fdinfo(sockinfo(
            family=30, vflag=1, lport=2830, fport=50000, state=4,
            laddr=b"\x7f\0\0\x01", faddr=b"\x7f\0\0\x01"))
        assert conn.laddr == ("127.0.0.1", 2830) and conn.raddr == ("127.0.0.1", 50000)

    @pytest.mark.parametrize("state, name", [(0, "CLOSE"), (1, "LISTEN"), (2, "SYN_SENT"),
                                             (4, "ESTABLISHED"), (5, "CLOSE_WAIT"),
                                             (10, "TIME_WAIT"), (99, "NONE")])
    def test_tcp_states_use_psutils_spelling(self, state, name):
        assert darwin_procs.parse_socket_fdinfo(sockinfo(state=state)).status == name

    @pytest.mark.parametrize("kind", [1, 3, 4, 5, 6])      # UDP, Unix domain, ndrv, kernel...
    def test_a_socket_that_is_not_tcp_is_skipped(self, kind):
        assert darwin_procs.parse_socket_fdinfo(sockinfo(kind=kind)) is None

    def test_a_family_that_is_not_inet_is_skipped(self):
        assert darwin_procs.parse_socket_fdinfo(sockinfo(family=1)) is None      # AF_UNIX

    def test_a_short_buffer_is_skipped(self):
        assert darwin_procs.parse_socket_fdinfo(sockinfo()[:200]) is None
        assert darwin_procs.parse_socket_fdinfo(b"") is None

    def test_ports_follow_network_byte_order(self):
        assert darwin_procs.parse_socket_fdinfo(sockinfo(lport=1)).laddr.port == 1
        assert darwin_procs.parse_socket_fdinfo(sockinfo(lport=65535)).laddr.port == 65535
        assert darwin_procs.parse_socket_fdinfo(sockinfo(lport=0x0B0E)).laddr.port == 0x0B0E


# ---- the socket layout self-check, with its I/O faked -------------------------------------------

class _FakeSocket:
    """Stands in for the probe's listener: it "binds" to a fixed port."""

    def __init__(self, *args):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def bind(self, addr):
        pass

    def listen(self, backlog):
        pass

    def getsockname(self):
        return ("127.0.0.1", 4321)


def _listener(port):
    return darwin_procs.Conn(socket.AF_INET, darwin_procs.Addr("127.0.0.1", port), (),
                             "LISTEN", os.getpid())


@pytest.fixture
def unprobed(monkeypatch):
    """A probe built without libproc (so on any OS), its first socket check still to run."""
    probe = object.__new__(darwin_procs.DarwinProbe)
    probe._sockets_checked = None
    monkeypatch.setattr(darwin_procs, "socket", type("S", (), {"socket": _FakeSocket}))
    monkeypatch.setattr(probe, "all_pids", lambda: [])
    return probe


class TestSocketSelfCheckCaching:

    def test_a_transient_failure_is_raised_but_not_cached(self, unprobed, monkeypatch):
        """EMFILE opening the probe socket, or a descriptor list that kept growing, says
        nothing about the layout: the check ran again on the next call, and one failure
        used to disable the socket table for the life of the server."""
        failures = [OSError(errno.EMFILE, "too many open files"),
                    OSError(errno.EIO, "the descriptor list kept growing")]

        def process_connections(pid):
            if failures:
                raise failures.pop(0)
            return [_listener(4321)]

        monkeypatch.setattr(unprobed, "process_connections", process_connections)
        for _ in range(2):
            with pytest.raises(OSError):
                unprobed.connections()
            assert unprobed._sockets_checked is None
        assert unprobed.connections() == []          # no pids to walk, but the check passed
        assert unprobed._sockets_checked is True
        assert unprobed.connections() == []          # verdict cached: no second probe

    def test_a_failure_opening_the_probe_socket_is_not_cached(self, unprobed, monkeypatch):
        def no_descriptors(*args):
            raise OSError(errno.EMFILE, "too many open files")

        monkeypatch.setattr(darwin_procs, "socket",
                            type("S", (), {"socket": no_descriptors}))
        with pytest.raises(OSError) as raised:
            unprobed.listeners(2830)
        assert raised.value.errno == errno.EMFILE
        assert unprobed._sockets_checked is None

    def test_a_definite_layout_mismatch_is_cached_as_unavailable(self, unprobed, monkeypatch):
        calls = []

        def wrong_port(pid):
            calls.append(pid)
            return [_listener(9999)]                 # the probe listens on 4321

        monkeypatch.setattr(unprobed, "process_connections", wrong_port)
        for _ in range(2):
            with pytest.raises(OSError) as raised:
                unprobed.connections()
            assert raised.value.errno == errno.ENOTSUP
        assert unprobed._sockets_checked is False
        assert len(calls) == 1, "a mismatch is a verdict: it is not probed again"


# ---- the reader on a Mac -----------------------------------------------------------------------

pytestmark_live = pytest.mark.skipif(not DARWIN, reason="libproc exists on macOS only")


@pytest.fixture
def probe():
    found = proc_facts.darwin_probe()
    assert found is not None, "libproc could not be used or its self-check failed on this Mac"
    return found


def spawn(code, *args, **kw):
    child = subprocess.Popen([REAL_PYTHON, "-c", code, *args], stdout=subprocess.PIPE,
                             stdin=subprocess.DEVNULL, **kw)
    assert child.stdout.readline().strip() == b"up"
    return child


def reap(child):
    child.kill()
    child.wait()
    child.stdout.close()


@pytestmark_live
class TestLiveProcess:

    def test_this_process_reads_back_as_itself(self, probe):
        info = probe.bsd_info(os.getpid())
        assert (info.pid, info.ruid, info.ppid) == (os.getpid(), os.getuid(), os.getppid())
        assert probe.same_user(os.getpid()) is True
        assert probe.alive(os.getpid()) is True

    def test_the_readings_agree_with_psutil(self, probe):
        import psutil
        child = spawn("import time; print('up', flush=True); time.sleep(60)", "x y", "")
        try:
            theirs = psutil.Process(child.pid)
            info = probe.bsd_info(child.pid)
            assert info.ppid == theirs.ppid() == os.getpid()
            assert os.path.realpath(probe.image_path(child.pid)) == os.path.realpath(theirs.exe())
            assert probe.argv(child.pid) == theirs.cmdline()
            assert os.path.realpath(probe.cwd(child.pid)) == os.path.realpath(theirs.cwd())
        finally:
            reap(child)

    def test_the_exec_path_keeps_a_symlink_that_the_image_path_resolves(self, probe, tmp_path):
        """A venv's bin/python is a symlink out of the environment: the name it was started
        with stays under the environment, the file behind it does not."""
        env = tmp_path / "env"
        (env / "bin").mkdir(parents=True)
        link = env / "bin" / "python"
        os.symlink(REAL_PYTHON, link)
        child = subprocess.Popen([str(link), "-c", "print('up', flush=True); import time; "
                                  "time.sleep(60)"], stdout=subprocess.PIPE,
                                 stdin=subprocess.DEVNULL)
        try:
            assert child.stdout.readline().strip() == b"up"
            exe, argv = probe.procargs(child.pid)
            assert exe == str(link) and argv[0] == str(link)
            assert not os.path.realpath(probe.image_path(child.pid)).startswith(
                os.path.realpath(env) + os.sep)
            assert probe.holder(str(env)) == child.pid
            assert proc_facts.psutil_holder(str(env)) == child.pid
        finally:
            reap(child)
        assert probe.holder(str(env)) is None

    def test_an_exited_process_is_not_alive_even_before_it_is_reaped(self, probe):
        child = spawn("print('up', flush=True)")

        def exited_but_not_reaped():
            try:
                os.kill(child.pid, 0)
            except ProcessLookupError:
                pytest.fail("the child was reaped")
            return not probe.alive(child.pid)

        wait_until(exited_but_not_reaped, "the child exited and was no longer alive", interval=0.05)
        assert probe.alive(child.pid) is False
        os.kill(child.pid, 0)               # kill(0) still finds the zombie: the point of this test
        with pytest.raises(OSError):
            probe.facts(child.pid)
        child.wait()
        child.stdout.close()

    def test_a_pid_that_does_not_exist_is_an_error_and_not_alive(self, probe):
        with pytest.raises(OSError):
            probe.bsd_info(999999)
        assert probe.alive(999999) is False
        # Larger than a 32-bit pid: an OSError, never the ArgumentError ctypes would raise.
        for huge in (2 ** 31, 10 ** 12):
            for read in (probe.bsd_info, probe.facts, probe.argv, probe.cwd, probe.image_path,
                         probe.process_connections):
                with pytest.raises(OSError):
                    read(huge)
            assert probe.alive(huge) is False

    def test_a_process_of_another_user_cannot_be_described_and_is_foreign_to_the_caller(
            self, probe):
        """The kernel refuses proc_pidinfo for a process of another user (launchd is root's),
        so facts() raises where Linux and Windows would answer (False, [], None). Callers
        count either as foreign (mcp_port.server_pid_is_ours refuses on any OSError)."""
        if os.getuid() == 0:
            pytest.skip("root can read everything")
        with pytest.raises(PermissionError):
            probe.facts(1)
        assert probe.alive(1) is True                       # it exists; it is not ours to open
        from browserplug_shared import mcp_port
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        assert mcp_port.server_pid_is_ours(1, root) is False

    def test_parent_and_start_time_agree_with_psutil(self, probe):
        import psutil
        child = spawn("import time; print('up', flush=True); time.sleep(60)")
        try:
            ppid, started = probe.parent_and_start(child.pid)
            assert ppid == os.getpid()
            assert started == pytest.approx(psutil.Process(child.pid).create_time(), abs=0.05)
            assert abs(started - time.time()) < 120
            assert proc_facts.parent_and_start(child.pid) == (ppid, started)
            # A later process starts later: the ordering client_pid.py compares.
            assert probe.parent_and_start(os.getpid())[1] <= started
        finally:
            reap(child)

    def test_the_process_list_holds_this_process_and_its_parent(self, probe):
        pids = probe.all_pids()
        assert os.getpid() in pids and os.getppid() in pids and 1 in pids
        assert all(isinstance(pid, int) and pid > 0 for pid in pids)


LISTENER = ("import socket,sys,time\n"
            "fam = socket.AF_INET6 if ':' in sys.argv[1] else socket.AF_INET\n"
            "s = socket.socket(fam)\n"
            "s.bind((sys.argv[1], 0)); s.listen(4)\n"
            "target = {'0.0.0.0': '127.0.0.1', '::': '::1'}.get(sys.argv[1], sys.argv[1])\n"
            "c = socket.create_connection((target, s.getsockname()[1]))\n"
            "a, _ = s.accept()\n"
            "print('up', s.getsockname()[1], c.getsockname()[1], flush=True)\n"
            "time.sleep(60)\n")


def spawn_listener(address):
    child = subprocess.Popen([REAL_PYTHON, "-c", LISTENER, address], stdout=subprocess.PIPE,
                             stdin=subprocess.DEVNULL)
    word, listen_port, client_port = child.stdout.readline().split()
    assert word == b"up"
    return child, int(listen_port), int(client_port)


def rows_of(probe, pid):
    return {(c.laddr.ip, c.laddr.port, c.raddr.ip if c.raddr else None,
             c.raddr.port if c.raddr else None, c.status) for c in probe.process_connections(pid)}


def unmapped(row):
    """`row` with an IPv4 address that sits in an IPv6 socket written as the plain IPv4 one.
    psutil prints such a peer as `::a.b.c.d` on macOS (it decodes by family, from the bare
    union); the standard spelling is `::ffff:a.b.c.d`. The native reader gives `a.b.c.d`,
    which is what the vflag says it is."""
    def plain(ip):
        low = (ip or "").lower()
        for prefix in ("::ffff:", "::"):
            if low.startswith(prefix) and "." in low:
                return low[len(prefix):]
        return ip
    return (plain(row[0]), row[1], plain(row[2]), row[3], row[4])


def psutil_rows(pid):
    import psutil
    return {(c.laddr.ip, c.laddr.port, c.raddr.ip if c.raddr else None,
             c.raddr.port if c.raddr else None, c.status)
            for c in psutil.Process(pid).net_connections("tcp")}


@pytestmark_live
class TestLiveSockets:

    @pytest.mark.parametrize("address", ["127.0.0.1", "0.0.0.0", "::1", "::"],
                             ids=["v4-loopback", "v4-wildcard", "v6-loopback", "v6-wildcard"])
    def test_a_listener_and_its_connection_read_as_psutil_reads_them(self, probe, address):
        if ":" in address and not socket.has_ipv6:
            pytest.skip("no IPv6 stack")
        child, listen_port, client_port = spawn_listener(address)
        try:
            ours = rows_of(probe, child.pid)
            assert ours == psutil_rows(child.pid), "native table differs from psutil"
            assert any(row[1] == listen_port and row[4] == "LISTEN" for row in ours)
            assert any(row[1] == client_port and row[3] == listen_port
                       and row[4] == "ESTABLISHED" for row in ours)
        finally:
            reap(child)

    def test_every_listener_of_this_user_agrees_with_psutil(self, probe):
        import psutil
        listening = socket.socket()          # so the table holds at least this process's own
        listening.bind(("127.0.0.1", 0))
        listening.listen()
        theirs = set()
        for proc in psutil.process_iter():
            try:
                for c in proc.net_connections("tcp"):
                    if c.status == psutil.CONN_LISTEN:
                        theirs.add((proc.pid, c.laddr.ip, c.laddr.port))
            except psutil.Error:
                continue
        ours = {(c.pid, c.laddr.ip, c.laddr.port) for c in probe.connections()
                if c.status == darwin_procs.CONN_LISTEN}
        listening.close()
        assert theirs, "this Mac has no listener of this user to compare"
        # A listener that opened or closed between the two reads is not a disagreement.
        assert len(theirs ^ ours) <= 2, sorted(theirs ^ ours)

    def test_the_table_walk_skips_processes_it_may_not_open(self, probe):
        rows = probe.connections()
        assert all(c.pid is not None for c in rows)
        assert probe.connections([1, 999999]) == []         # launchd is root's, 999999 is gone

    def test_one_process_limits_the_walk(self, probe):
        child, listen_port, _ = spawn_listener("127.0.0.1")
        try:
            rows = probe.connections([child.pid])
            assert {c.pid for c in rows} == {child.pid}
            assert any(c.laddr.port == listen_port for c in rows)
        finally:
            reap(child)

    def test_listeners_names_the_owner_and_stops_early_for_a_pid_that_does_not_listen(self, probe):
        child, listen_port, _ = spawn_listener("127.0.0.1")
        try:
            assert probe.listeners(listen_port, claimed=child.pid) == [("127.0.0.1", child.pid)]
            assert probe.listeners(listen_port, claimed=os.getpid()) == []
            assert probe.listeners(listen_port) == [("127.0.0.1", child.pid)]
        finally:
            reap(child)

    def test_the_socket_layout_self_check_passes(self, probe):
        assert probe._sockets_make_sense() is True

    def test_a_layout_that_reads_wrong_makes_the_socket_table_unavailable(self, probe, monkeypatch):
        monkeypatch.setattr(probe, "_sockets_checked", None)
        monkeypatch.setattr(probe, "_sockets_make_sense", lambda: False)
        with pytest.raises(OSError):
            probe.listeners(2830)
        with pytest.raises(OSError):
            probe.connections()
        with pytest.raises(OSError):
            probe.connections([os.getpid()])

    def test_listeners_of_a_claimed_pid_that_has_exited_is_empty_not_an_error(self, probe):
        """pid_owns_listener answers False for it; an OSError would read as 'table
        unreadable' and make mcp_port.server_pid_is_ours trust the port."""
        child = spawn("print('up', flush=True); import time; time.sleep(60)")
        os.kill(child.pid, signal.SIGKILL)
        child.wait()
        child.stdout.close()
        with pytest.raises(OSError) as raised:
            probe.process_connections(child.pid)
        assert raised.value.errno == errno.ESRCH
        assert probe.listeners(2830, claimed=child.pid) == []
        assert proc_facts.pid_owns_listener(child.pid, 2830) is False

    def test_a_process_with_more_descriptors_than_any_first_guess_is_read_whole(self, probe):
        code = ("import resource, socket, time\n"
                "soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)\n"
                "want = 4096 if hard == resource.RLIM_INFINITY else min(hard, 4096)\n"
                "resource.setrlimit(resource.RLIMIT_NOFILE, (want, hard))\n"
                "held = [socket.socket() for _ in range(want - 64)]\n"
                "print('up', flush=True)\n"
                "time.sleep(60)\n")
        child = spawn(code)
        try:
            assert len(probe._socket_fds(child.pid)) >= 2000
        finally:
            reap(child)

    def test_an_ipv4_peer_of_a_dual_stack_socket_reads_as_psutil_reads_it(self, probe):
        """The native reader decodes by the protocol-version flag (an IPv4 peer carries only
        the IPv4 flag), psutil by the socket family (it prints `::a.b.c.d`). The two
        spellings name one address, so the rows are compared with the prefix removed."""
        if not socket.has_ipv6:
            pytest.skip("no IPv6 stack")
        with socket.socket(socket.AF_INET6) as server:
            server.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            server.bind(("::", 0))
            server.listen(1)
            port = server.getsockname()[1]
            with socket.create_connection(("127.0.0.1", port)) as client:
                client_port = client.getsockname()[1]
                accepted, _ = server.accept()
                with accepted:
                    ours = {unmapped(row) for row in rows_of(probe, os.getpid())
                            if {port, client_port} & {row[1], row[3]}}
                    theirs = {unmapped(row) for row in psutil_rows(os.getpid())
                              if {port, client_port} & {row[1], row[3]}}
        assert ours == theirs
        assert ("127.0.0.1", port, "127.0.0.1", client_port, "ESTABLISHED") in ours


@pytestmark_live
def test_a_probe_that_cannot_describe_this_process_refuses_to_exist(monkeypatch):
    """The self-check is what turns a moved struct into 'unavailable' instead of 'wrong'."""
    monkeypatch.setattr(darwin_procs.DarwinProbe, "bsd_info",
                        lambda self, pid: darwin_procs.BsdInfo(1, 1, 0, 0, 3, "x", 0.0))
    with pytest.raises(OSError):
        darwin_procs.DarwinProbe()


@pytestmark_live
def test_sigkilled_children_do_not_leave_the_probe_confused(probe):
    child = spawn("print('up', flush=True); import time; time.sleep(60)")
    os.kill(child.pid, signal.SIGKILL)
    child.wait()
    child.stdout.close()
    assert probe.alive(child.pid) is False
    with pytest.raises(OSError):
        probe.facts(child.pid)
