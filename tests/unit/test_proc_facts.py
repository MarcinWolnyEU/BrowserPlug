"""browserplug_shared.proc_facts: the owner, argv and cwd of a pid, from the standard library.

mcp_port.server_pid_is_ours reads these three facts to decide whether the process answering
/health is this checkout's server, on whatever interpreter a client registered the launcher
with: usually uv's bare Python, where psutil is not installed. So every reading here is
compared with psutil on live processes (psutil is in the runner's environment). That
comparison is what catches a PEB offset that a future Windows moves, and a /proc layout
assumption that a kernel changes.
"""

import os
import socket
import subprocess
import sys

import pytest

from browserplug_shared import proc_facts

WINDOWS = os.name == "nt"
LINUX = sys.platform.startswith("linux")
NATIVE = WINDOWS or LINUX

# A real interpreter, never a venv's redirector: on Windows `sys.executable` inside a uv
# environment is a trampoline that moves its own cwd to %TEMP% and starts the real python as
# a child, so a pid taken from it would answer for the wrong process.
REAL_PYTHON = getattr(sys, "_base_executable", None) or sys.executable
# Prints once it runs Python code: a process still being created has no PEB parameters yet,
# so a cwd read before then is legitimately empty.
SLEEP = "import time; print('up', flush=True); time.sleep(120)"


def _norm(path):
    return os.path.normcase(os.path.realpath(path))


@pytest.fixture
def sleeper(tmp_path):
    """Start a child that runs from `tmp_path` with a command line of known awkward shape."""
    started = []

    def start(*extra, cwd=None):
        child = subprocess.Popen([REAL_PYTHON, "-c", SLEEP, *extra], cwd=str(cwd or tmp_path),
                                 stdin=subprocess.DEVNULL, stdout=subprocess.PIPE)
        started.append(child)
        assert child.stdout.readline().strip() == b"up"
        return child

    yield start
    for child in started:
        child.kill()
        child.wait()
        child.stdout.close()


# ---- the running platform ------------------------------------------------------------------

@pytest.mark.skipif(not NATIVE, reason="the native backends are Windows and Linux")
class TestNativeBackend:

    def test_a_process_of_this_user_reports_its_argv_and_cwd(self, sleeper, tmp_path):
        child = sleeper("plain", "two words", 'has "quotes"', "back\\slash", "")
        same_user, argv, cwd = proc_facts.facts(child.pid)
        assert same_user is True
        assert argv[1:] == ["-c", SLEEP, "plain", "two words", 'has "quotes"', "back\\slash", ""]
        assert _norm(cwd) == _norm(str(tmp_path))

    def test_this_process_is_its_own_user(self):
        same_user, argv, cwd = proc_facts.facts(os.getpid())
        assert same_user is True
        assert argv and all(isinstance(arg, str) for arg in argv)
        assert _norm(cwd) == _norm(os.getcwd())

    def test_the_cwd_follows_the_process_not_the_reader(self, sleeper, tmp_path):
        """A relative server.py is placed by the target's cwd, so a reader that answered with
        its own would adopt any process started that way anywhere."""
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        child = sleeper(cwd=elsewhere)
        assert _norm(proc_facts.facts(child.pid)[2]) == _norm(str(elsewhere))
        assert _norm(proc_facts.facts(child.pid)[2]) != _norm(os.getcwd())

    @pytest.mark.parametrize("pid", [999999996, 0, -1, True, "12", None, 1.5])
    def test_a_pid_that_is_not_a_process_is_an_error(self, pid):
        with pytest.raises(OSError):
            proc_facts.facts(pid)

    def test_a_pid_that_has_exited_is_an_error(self, sleeper):
        child = sleeper()
        child.kill()
        child.wait()
        with pytest.raises(OSError):
            proc_facts.facts(child.pid)

    @pytest.mark.skipif(not WINDOWS, reason="pid 4 is the Windows System process")
    def test_a_process_of_another_user_is_reported_without_reading_its_command_line(
            self, monkeypatch):
        """The System process (pid 4) answers differently per box: unreadable on a
        non-elevated one, readable and another user's on an elevated one. So the other-user
        branch is driven by a probe that says so, and asserted on every box."""
        def argv_would_leak(self, pid):
            raise AssertionError("the command line of another user's process was read")

        monkeypatch.setattr(proc_facts.WindowsProbe, "same_user", lambda self, pid: False)
        monkeypatch.setattr(proc_facts.WindowsProbe, "argv", argv_would_leak)
        monkeypatch.setattr(proc_facts.WindowsProbe, "cwd", argv_would_leak)
        assert proc_facts.facts(4) == (False, [], None)

    @pytest.mark.skipif(not WINDOWS, reason="pid 4 is the Windows System process")
    def test_the_system_process_is_never_taken_for_a_server_of_this_checkout(self):
        """Whatever the box lets the reader see of pid 4, the launcher's verdict is no, and
        when it can be read at all it is read as another user's."""
        from browserplug_shared import mcp_port
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        assert mcp_port.server_pid_is_ours(4, root) is False
        try:
            same_user, argv, _cwd = proc_facts.facts(4)
        except OSError:
            pass        # unreadable here; the verdict above is what matters
        else:
            assert same_user is False and argv == []

    def test_the_same_facts_decide_a_server_entry(self, sleeper):
        from browserplug_shared import mcp_port
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        entry = os.path.join(root, "server", "server.py")
        ours = sleeper(entry)
        stranger = sleeper("not_a_server.py")
        assert mcp_port.server_pid_is_ours(ours.pid, root) is True
        assert mcp_port.server_pid_is_ours(stranger.pid, root) is False


# ---- the cross-check with psutil ------------------------------------------------------------

@pytest.mark.skipif(not NATIVE, reason="the native backends are Windows and Linux")
def test_every_reading_agrees_with_psutil_on_the_live_processes(sleeper, tmp_path):
    """The native readers against psutil, on every process psutil lists.

    Owner, argv and cwd must be equal wherever both can read them. A pid that vanishes
    while it is being compared is skipped, never counted as a disagreement. The processes
    started here guarantee the comparison is not empty."""
    import psutil
    sleeper("alpha", "beta gamma")
    sleeper(cwd=tmp_path.parent)
    me = psutil.Process().username()
    compared = 0
    problems = []
    for pid in psutil.pids():
        try:
            process = psutil.Process(pid)
            theirs_is_me = process.username() == me
            their_argv = process.cmdline()
        except psutil.Error:
            continue                       # protected or gone: psutil cannot say either
        try:
            their_cwd = process.cwd()
        except psutil.Error:
            their_cwd = None
        try:
            same_user, argv, cwd = proc_facts.facts(pid)
        except OSError:
            if theirs_is_me and process.is_running():
                problems.append(f"pid {pid} {process.name()}: psutil reads it, facts() cannot")
            continue
        compared += 1
        if same_user != theirs_is_me:
            problems.append(f"pid {pid} {process.name()}: owner {same_user} vs psutil "
                            f"{theirs_is_me}")
        elif same_user:
            if argv != their_argv and process.is_running():
                problems.append(f"pid {pid} {process.name()}: argv {argv!r} vs {their_argv!r}")
            if cwd is not None and their_cwd is not None and _norm(cwd) != _norm(their_cwd):
                problems.append(f"pid {pid} {process.name()}: cwd {cwd!r} vs {their_cwd!r}")
            if cwd is None and their_cwd is not None and process.is_running():
                problems.append(f"pid {pid} {process.name()}: psutil reads the cwd "
                                f"{their_cwd!r}, facts() gets none")
    assert compared >= 3, f"only {compared} processes were comparable"
    assert not problems,"native readings differ from psutil:\n" + "\n".join(problems)


# ---- the Linux reader, over fixture text ------------------------------------------------------

class TestLinuxReader:

    def test_the_real_uid_is_the_first_number_on_the_uid_line(self):
        text = "Name:\tpython\nUmask:\t0022\nUid:\t1000\t1001\t1000\t1000\nGid:\t1000\t1000\n"
        assert proc_facts.parse_status_uid(text) == 1000

    @pytest.mark.parametrize("text", ["", "Name:\tpython\n", "Uid:\n", "Uid:\tnope\t1\n"])
    def test_a_status_without_a_uid_is_unreadable(self, text):
        with pytest.raises(OSError):
            proc_facts.parse_status_uid(text)

    @pytest.mark.parametrize("raw,expected", [
        (b"", []),
        (b"python3\0server/server.py\0", ["python3", "server/server.py"]),
        (b"python3\0server/server.py", ["python3", "server/server.py"]),
        (b"python3\0\0x\0", ["python3", "", "x"]),
        ("pythón\0café\0".encode(), ["pythón", "café"]),
    ])
    def test_cmdline_is_split_on_nul(self, raw, expected):
        assert proc_facts.parse_cmdline(raw) == expected

    def _proc(self, tmp_path, uid, cmdline=b"python3\0server/server.py\0"):
        base = tmp_path / "4242"
        base.mkdir()
        (base / "status").write_text(f"Name:\tpython3\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n")
        (base / "cmdline").write_bytes(cmdline)
        return tmp_path

    def test_a_process_of_the_same_uid_reads_fully(self, tmp_path, monkeypatch):
        proc = self._proc(tmp_path, 1000)
        monkeypatch.setattr(os, "readlink", lambda path: "/srv/checkout")
        assert proc_facts.linux_facts(4242, str(proc), uid=1000) == (
            True, ["python3", "server/server.py"], "/srv/checkout")

    def test_another_uid_is_foreign_and_its_argv_is_never_read(self, tmp_path):
        proc = self._proc(tmp_path, 1001)
        os.remove(proc / "4242" / "cmdline")            # reading it would raise
        assert proc_facts.linux_facts(4242, str(proc), uid=1000) == (False, [], None)

    def test_an_unreadable_cwd_is_none_not_an_error(self, tmp_path, monkeypatch):
        """Another user's cwd link raises PermissionError; a process's own is read, but a
        directory deleted under it reads as a dangling link."""
        proc = self._proc(tmp_path, 1000)

        def refuse(path):
            raise PermissionError(path)
        monkeypatch.setattr(os, "readlink", refuse)
        assert proc_facts.linux_facts(4242, str(proc), uid=1000)[2] is None

    def test_a_pid_with_no_proc_entry_is_an_error(self, tmp_path):
        with pytest.raises(OSError):
            proc_facts.linux_facts(4242, str(tmp_path), uid=1000)


# ---- the fallback and the dispatch ------------------------------------------------------------

class TestFallback:

    def _no_native_backend(self, monkeypatch):
        monkeypatch.setattr(proc_facts, "_IS_WINDOWS", False)
        monkeypatch.setattr(proc_facts.sys, "platform", "darwin")

    def test_without_a_native_backend_or_psutil_the_check_is_unavailable(self, monkeypatch):
        self._no_native_backend(monkeypatch)
        monkeypatch.setitem(sys.modules, "psutil", None)          # import psutil -> ImportError
        with pytest.raises(proc_facts.ProbeUnavailable):
            proc_facts.facts(os.getpid())

    def test_without_a_native_backend_psutil_answers_when_it_can(self, monkeypatch, tmp_path):
        import psutil  # noqa: F401  (declared for the suite: a missing one must fail, not skip)
        self._no_native_backend(monkeypatch)
        same_user, argv, cwd = proc_facts.facts(os.getpid())
        assert same_user is True and argv
        assert _norm(cwd) == _norm(os.getcwd())

    def test_psutil_errors_are_oserrors_and_another_users_process_is_foreign(self,
                                                                             monkeypatch):
        import psutil
        self._no_native_backend(monkeypatch)
        with pytest.raises(OSError):
            proc_facts.facts(999999996)

        class Stranger:
            def __init__(self, pid=None):
                self.pid = pid

            def username(self):
                return "someone-else" if self.pid else "me"

            def cmdline(self):
                raise AssertionError("another user's argv must not be read")
        monkeypatch.setattr(psutil, "Process", Stranger)
        assert proc_facts.facts(7) == (False, [], None)


# ---- what launcher/client_pid.py walks the chain with -----------------------------------------

@pytest.mark.skipif(not WINDOWS, reason="WindowsProbe is the Windows reader")
class TestWindowsProbe:

    def test_it_answers_image_creation_time_and_parent(self, sleeper):
        probe = proc_facts.WindowsProbe()
        child = sleeper()
        assert os.path.normcase(probe.image_path(child.pid)) == os.path.normcase(REAL_PYTHON) \
            or os.path.basename(probe.image_path(child.pid)).lower() == "python.exe"
        assert probe.parent(child.pid) == os.getpid()
        assert probe.created(child.pid) >= probe.created(os.getpid())
        assert "time.sleep(120)" in probe.command_line(child.pid)

    def test_an_unopenable_pid_raises_oserror_for_every_reading(self):
        probe = proc_facts.WindowsProbe()
        for reading in (probe.image_path, probe.command_line, probe.created, probe.parent,
                        probe.same_user):
            with pytest.raises(OSError):
                reading(999999996)

    def test_the_probe_is_shared_with_the_client_pid_walk(self):
        from launcher import client_pid
        assert client_pid._WindowsProbe is proc_facts.WindowsProbe


@pytest.mark.skipif(not WINDOWS, reason="the SID compare is the Windows reader's")
class TestWindowsOwner:
    """No second user account exists to start a process under, so the owner compare is driven
    with token buffers built from known SIDs: the same SID twice is the same user, two
    different ones are not."""

    @staticmethod
    def _token_user(sid_string):
        """A TOKEN_USER-shaped buffer: a pointer to the SID, which sits right behind it."""
        import ctypes
        from ctypes import wintypes
        adv = ctypes.WinDLL("advapi32", use_last_error=True)
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        adv.ConvertStringSidToSidW.argtypes = (wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p))
        adv.GetLengthSid.argtypes = (ctypes.c_void_p,)
        adv.GetLengthSid.restype = wintypes.DWORD
        k32.LocalFree.argtypes = (ctypes.c_void_p,)
        psid = ctypes.c_void_p()
        assert adv.ConvertStringSidToSidW(sid_string, ctypes.byref(psid))
        try:
            size = adv.GetLengthSid(psid)
            buf = ctypes.create_string_buffer(8 + size)
            ctypes.memmove(ctypes.addressof(buf) + 8, psid, size)
            ctypes.c_void_p.from_buffer(buf).value = ctypes.addressof(buf) + 8
            return buf
        finally:
            k32.LocalFree(psid)

    def _owner_check(self, monkeypatch, theirs, mine):
        probe = proc_facts.WindowsProbe()
        answers = iter([self._token_user(theirs), self._token_user(mine)])
        monkeypatch.setattr(probe, "_user_sid", lambda handle: next(answers))
        return probe.same_user(os.getpid())

    def test_the_same_sid_is_the_same_user(self, monkeypatch):
        assert self._owner_check(monkeypatch, "S-1-5-21-1-2-3-1001", "S-1-5-21-1-2-3-1001") is True

    def test_another_sid_is_another_user(self, monkeypatch):
        assert self._owner_check(monkeypatch, "S-1-5-18", "S-1-5-21-1-2-3-1001") is False
        assert self._owner_check(monkeypatch, "S-1-5-21-1-2-3-1002",
                                 "S-1-5-21-1-2-3-1001") is False

    def test_facts_of_another_user_carry_no_argv_and_no_cwd(self, monkeypatch):
        probe = proc_facts.WindowsProbe()
        monkeypatch.setattr(probe, "same_user", lambda pid: False)
        monkeypatch.setattr(probe, "argv", lambda pid: pytest.fail("another user's argv read"))
        assert probe.facts(os.getpid()) == (False, [], None)


# ---- which process owns the socket that answers on a port ---------------------------------------

@pytest.fixture
def listening():
    """A listening 127.0.0.1 socket of this process; yields its port."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    yield sock.getsockname()[1]
    sock.close()


@pytest.mark.skipif(not NATIVE, reason="the native backends are Windows and Linux")
class TestListenerOwner:

    def test_the_process_that_listens_owns_the_listener(self, listening):
        assert proc_facts.pid_owns_listener(os.getpid(), listening) is True

    def test_another_process_does_not_own_it_even_when_it_is_a_real_one(self, listening):
        assert proc_facts.pid_owns_listener(os.getppid(), listening) is False

    def test_a_port_nobody_listens_on_has_no_owner(self, listening):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            free = probe.getsockname()[1]
        assert proc_facts.pid_owns_listener(os.getpid(), free) is False

    def test_it_agrees_with_psutil_about_who_listens(self, sleeper, tmp_path):
        import psutil
        code = ("import socket,time; s=socket.socket(); s.bind(('127.0.0.1',0)); s.listen(); "
                "print(s.getsockname()[1], flush=True); time.sleep(120)")
        child = subprocess.Popen([REAL_PYTHON, "-c", code], stdout=subprocess.PIPE,
                                 stdin=subprocess.DEVNULL)
        try:
            port = int(child.stdout.readline())
            ours = [c.pid for c in psutil.net_connections("tcp")
                    if c.status == "LISTEN" and c.laddr.port == port]
            assert ours == [child.pid]
            assert proc_facts.pid_owns_listener(child.pid, port) is True
            assert proc_facts.pid_owns_listener(os.getpid(), port) is False
        finally:
            child.kill()
            child.wait()
            child.stdout.close()

    @pytest.mark.skipif(not WINDOWS, reason="a bind beside a wildcard listener succeeds on "
                                            "Windows only")
    def test_a_loopback_listener_beside_a_wildcard_one_takes_the_connections(self):
        """On Windows a bind to 127.0.0.1 succeeds beside another process's 0.0.0.0 listener
        and wins every loopback connection. The wildcard process owns a listener on the port
        and is still not who answers."""
        code = ("import socket,time; s=socket.socket(); s.bind(('0.0.0.0',0)); s.listen(); "
                "print(s.getsockname()[1], flush=True); time.sleep(120)")
        wildcard = subprocess.Popen([REAL_PYTHON, "-c", code], stdout=subprocess.PIPE,
                                    stdin=subprocess.DEVNULL)
        try:
            port = int(wildcard.stdout.readline())
            assert proc_facts.pid_owns_listener(wildcard.pid, port) is True
            with socket.socket() as specific:
                specific.bind(("127.0.0.1", port))
                specific.listen()
                assert proc_facts.pid_owns_listener(os.getpid(), port) is True
                assert proc_facts.pid_owns_listener(wildcard.pid, port) is False
        finally:
            wildcard.kill()
            wildcard.wait()
            wildcard.stdout.close()

    @pytest.mark.parametrize("bind, dual_stack", [("::1", None), ("::", False)],
                             ids=["ipv6-loopback", "dual-stack-wildcard"])
    def test_an_ipv6_listener_is_read_from_the_ipv6_half_of_the_table(self, bind, dual_stack):
        """A ::1 or dual-stack :: listener lives only in the IPv6 table (Windows
        GetExtendedTcpTable(AF_INET6), /proc/net/tcp6). A reader that lost that half would
        answer "nobody listens" for a server bound that way."""
        if not socket.has_ipv6:
            pytest.skip("no IPv6 stack")
        sock = socket.socket(socket.AF_INET6)
        try:
            if dual_stack is not None:
                sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, int(dual_stack))
            try:
                sock.bind((bind, 0))
            except OSError as e:
                pytest.skip(f"cannot bind {bind}: {e}")
            sock.listen()
            port = sock.getsockname()[1]
            if WINDOWS:
                rows = proc_facts._windows().listeners(port)
                assert (bind, os.getpid()) in rows
            assert proc_facts.pid_owns_listener(os.getpid(), port) is True
            assert proc_facts.pid_owns_listener(os.getppid(), port) is False
        finally:
            sock.close()

    def test_reading_the_table_is_fast(self, listening):
        import time
        started = time.perf_counter()
        for _ in range(20):
            proc_facts.pid_owns_listener(os.getpid(), listening)
        assert (time.perf_counter() - started) / 20 < 0.25


@pytest.mark.skipif(not WINDOWS, reason="the Windows socket table")
def test_a_port_two_processes_listen_on_beside_each_other_has_no_single_owner(monkeypatch):
    """Windows lets two processes share a listener address when both ask for it. A
    connection then reaches either, so neither pid vouches for the answer."""
    class Table:
        def listeners(self, port):
            return [("127.0.0.1", 5), ("127.0.0.1", 6), ("0.0.0.0", 7)]
    monkeypatch.setattr(proc_facts, "_windows", lambda: Table())
    assert proc_facts.pid_owns_listener(5, 2830) is False
    assert proc_facts.pid_owns_listener(6, 2830) is False
    assert proc_facts.pid_owns_listener(7, 2830) is False


def test_the_reachable_owners_follow_the_most_specific_listener():
    reached = proc_facts.reached_owners
    assert reached([("0.0.0.0", 1), ("127.0.0.1", 2)]) == {2}
    assert reached([("0.0.0.0", 1)]) == {1}
    assert reached([("::1", 3), ("::", 4)]) == {4}
    assert reached([("::ffff:127.0.0.1", 5), ("0.0.0.0", 1)]) == {5}
    assert reached([("127.0.0.1", 2), ("127.0.0.1", 6)]) == {2, 6}      # shared: ambiguous
    assert reached([("192.168.1.5", 7), ("::1", 8)]) == {8}
    assert reached([("192.168.1.5", 7)]) == set()


PROC_NET_TCP = """\
  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 0100007F:0B1E 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 12345 1 0 100 0
   1: 00000000:0016 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 777 1 0 100 0
   2: 0100007F:0B1E 0100007F:D2C8 01 00000000:00000000 00:00000000 00000000  1000        0 99999 1 0 100 0
"""
PROC_NET_TCP6 = """\
  sl  local_address                         remote_address                        st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 00000000000000000000000000000000:0B1F 00000000000000000000000000000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 4242 1 0 100 0
   1: 00000000000000000000000001000000:0B1E 00000000000000000000000000000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 4243 1 0 100 0
   2: 0000000000000000FFFF00000100007F:0B20 00000000000000000000000000000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 4244 1 0 100 0
"""


@pytest.mark.skipif(sys.byteorder != "little", reason="the fixture rows are little-endian")
class TestProcNetTcp:

    def test_listening_rows_are_parsed_and_established_ones_are_not(self):
        assert proc_facts.parse_proc_net_tcp(PROC_NET_TCP) == [
            ("127.0.0.1", 0x0B1E, 12345), ("0.0.0.0", 22, 777)]
        assert proc_facts.parse_proc_net_tcp(PROC_NET_TCP6) == [
            ("::", 0x0B1F, 4242), ("::1", 0x0B1E, 4243), ("::ffff:127.0.0.1", 0x0B20, 4244)]

    def test_garbage_rows_are_skipped(self):
        assert proc_facts.parse_proc_net_tcp("header\nnot a row\n   0: ZZ:1 x 0A a b c d e f 5\n") == []

    @pytest.fixture
    def proc(self, tmp_path):
        (tmp_path / "net").mkdir()
        (tmp_path / "net" / "tcp").write_text(PROC_NET_TCP, encoding="utf-8")
        (tmp_path / "net" / "tcp6").write_text(PROC_NET_TCP6, encoding="utf-8")
        return str(tmp_path)

    def test_the_owner_is_the_pid_holding_the_listening_inode(self, proc, monkeypatch):
        held = {100: {12345}, 200: {777, 4242}}
        monkeypatch.setattr(proc_facts, "socket_inodes",
                            lambda pid, proc_root="/proc": held[pid])
        assert proc_facts.linux_owns_listener(100, 0x0B1E, proc) is True
        assert proc_facts.linux_owns_listener(200, 0x0B1E, proc) is False
        # Port 22 is the IPv4 wildcard only; 0x0B1F is the IPv6 wildcard only.
        assert proc_facts.linux_owns_listener(200, 22, proc) is True
        assert proc_facts.linux_owns_listener(100, 22, proc) is False
        assert proc_facts.linux_owns_listener(200, 0x0B1F, proc) is True
        assert proc_facts.linux_owns_listener(100, 9999, proc) is False

    def test_two_listeners_on_one_address_are_not_ours_when_we_hold_only_one(self, tmp_path,
                                                                            monkeypatch):
        (tmp_path / "net").mkdir()
        (tmp_path / "net" / "tcp").write_text(
            PROC_NET_TCP + "   3: 0100007F:0B1E 00000000:0000 0A 00000000:00000000 00:00000000 "
                           "00000000  1001        0 5555 1 0 100 0\n", encoding="utf-8")
        monkeypatch.setattr(proc_facts, "socket_inodes", lambda pid, proc_root="/proc": {12345})
        assert proc_facts.linux_owns_listener(100, 0x0B1E, str(tmp_path)) is False
        monkeypatch.setattr(proc_facts, "socket_inodes",
                            lambda pid, proc_root="/proc": {12345, 5555})
        assert proc_facts.linux_owns_listener(100, 0x0B1E, str(tmp_path)) is True

    def test_a_listener_someone_else_holds_beside_it_is_not_ours(self, proc, monkeypatch):
        """127.0.0.1:0x0B1E (inode 12345) and the IPv6 loopback (4243) are two listeners; a
        connection to 127.0.0.1 reaches the first, so holding only the second is not enough."""
        monkeypatch.setattr(proc_facts, "socket_inodes", lambda pid, proc_root="/proc": {4243})
        assert proc_facts.linux_owns_listener(100, 0x0B1E, proc) is False

    def test_the_sockets_a_pid_holds_come_from_its_fd_links(self, tmp_path, monkeypatch):
        fd = tmp_path / "7" / "fd"
        fd.mkdir(parents=True)
        links = {"0": "/dev/null", "3": "socket:[555]", "4": "anon_inode:[eventpoll]",
                 "5": "socket:[556]"}
        monkeypatch.setattr(proc_facts.os, "listdir", lambda p: list(links))
        monkeypatch.setattr(proc_facts.os, "readlink",
                            lambda p: links[os.path.basename(p)])
        assert proc_facts.socket_inodes(7, str(tmp_path)) == {555, 556}


def test_a_pid_is_alive_until_it_exits_even_while_its_handle_is_held(sleeper):
    child = sleeper()
    assert proc_facts.pid_alive(child.pid) is True
    child.kill()
    child.wait()            # Popen still holds the process object here
    assert proc_facts.pid_alive(child.pid) is False
    assert proc_facts.pid_alive(os.getpid()) is True
    for bad in (0, -1, None, True, "1"):
        assert proc_facts.pid_alive(bad) is False


@pytest.mark.skipif(not WINDOWS, reason="the process-image list is the Windows in-use test")
class TestImageList:

    def test_a_running_image_is_found_without_opening_the_file(self, sleeper):
        child = sleeper()
        assert child.pid in proc_facts.pids_running_image(REAL_PYTHON)

    def test_an_image_nothing_runs_has_no_pids(self, tmp_path):
        lonely = tmp_path / "python.exe"
        lonely.write_bytes(b"")
        assert proc_facts.pids_running_image(str(lonely)) == []
        assert proc_facts.pids_running_image(str(tmp_path / "gone" / "python.exe")) == []
