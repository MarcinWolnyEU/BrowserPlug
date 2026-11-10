"""Process and TCP socket facts on macOS, read with ctypes from libproc and sysctl.

macOS offers no /proc, and psutil is not on the bare interpreter a client registers the
launcher with. The one system-wide socket call psutil has (`net_connections()`) raises
AccessDenied for a non-root user, because it opens every process. The per-process calls work
for processes of the same user, which is all this module reads:

- `proc_listpids`, `proc_pidinfo` and `proc_pidfdinfo` (libproc, in libSystem): the pid list,
  a process's owner and state (PROC_PIDTBSDINFO), its current directory
  (PROC_PIDVNODEPATHINFO), its open file descriptors (PROC_PIDLISTFDS) and the TCP state of
  each socket (PROC_PIDFDSOCKETINFO).
- `sysctl(KERN_PROCARGS2)`: a process's executable path and argv.

The struct layouts are the ones in XNU's bsd/sys/proc_info.h, which psutil and lsof read
too. They are written as byte offsets into the buffers the kernel fills, and
tests/unit/test_proc_facts.py compares every reading with psutil on live processes, which is
what would catch a layout a future macOS changes. `DarwinProbe` also checks itself against
this process before it answers anything (see `_self_check`), so a layout that drifted makes
the probe unavailable instead of wrong.

The parsers (`parse_procargs`, `parse_bsdinfo`, `parse_socket_fdinfo`) take plain bytes and
run on any OS, so their tests do not need a Mac.

A process this user may not open (another user's, or a protected system one) is skipped by
the table walks and raises OSError from the single-process readers.

Stdlib-only: the launcher imports this through proc_facts.
"""

import errno
import os
import socket
import struct
from collections import namedtuple

# ---- constants from sys/proc_info.h, sys/sysctl.h and netinet/tcp_fsm.h -------------------

_PROC_ALL_PIDS = 1
_PROC_PIDTBSDINFO = 3
_PROC_PIDVNODEPATHINFO = 9
_PROC_PIDLISTFDS = 1                  # a separate namespace from the flavors above
_PROC_PIDFDSOCKETINFO = 3             # proc_pidfdinfo flavor
_PROX_FDTYPE_SOCKET = 2
_FD_SLACK = 8 * 64                    # 64 proc_fdinfo entries (int fd, uint32 type) of headroom
_SOCKINFO_TCP = 2
_CTL_KERN = 1
_KERN_ARGMAX = 8
_KERN_PROCARGS2 = 49
_SZOMB = 5                            # pbi_status of a process that exited and is not reaped

_DARWIN_AF_INET = 2                   # AF_* differ per OS; these are macOS's
_DARWIN_AF_INET6 = 30
_INI_IPV4 = 0x1
_INI_IPV6 = 0x2

# struct proc_bsdinfo is 136 bytes: twelve uint32 (flags, status, xstatus, pid, ppid, uid,
# gid, ruid, rgid, svuid, svgid, rfu), pbi_comm[16], pbi_name[32], six more 32-bit fields
# (nfiles, pgid, pjobc, e_tdev, e_tpgid, nice) and the start time as two uint64 (seconds and
# microseconds since the epoch).
_BSDINFO_SIZE = 136
_BSDINFO_FORMAT = "=12I16s32s6I2Q"
_BSD_STATUS, _BSD_PID, _BSD_PPID, _BSD_UID, _BSD_RUID = 1, 3, 4, 5, 7
_BSD_START_SEC, _BSD_START_USEC = 20, 21

# struct proc_vnodepathinfo: pvi_cdir (a 152-byte vnode_info, then a 1024-byte path), then
# pvi_rdir. Only the current directory is read.
_VNODE_CDIR_PATH = 152
_VNODE_PATH_SIZE = 1024

# struct socket_fdinfo = proc_fileinfo (24 bytes) + socket_info. In socket_info the kind sits
# at 232 and the protocol union at 240; for TCP the union is a tcp_sockinfo, which starts
# with an in_sockinfo (fport, lport, ..., vflag at 24, faddr at 32, laddr at 48; 80 bytes)
# and continues with tcpsi_state. The offsets below are from the start of socket_fdinfo.
_SOCKET_FDINFO_BUFFER = 2048
_SI = 24
_SI_TYPE, _SI_PROTOCOL, _SI_FAMILY = _SI + 152, _SI + 156, _SI + 160
_SI_KIND = _SI + 232
_IN = _SI + 240
_IN_FPORT, _IN_LPORT, _IN_VFLAG = _IN, _IN + 4, _IN + 24
_IN_FADDR, _IN_LADDR = _IN + 32, _IN + 48
_TCP_STATE = _IN + 80
_SOCKET_FDINFO_MIN = _TCP_STATE + 4

_TCP_STATES = {0: "CLOSE", 1: "LISTEN", 2: "SYN_SENT", 3: "SYN_RECV", 4: "ESTABLISHED",
               5: "CLOSE_WAIT", 6: "FIN_WAIT1", 7: "CLOSING", 8: "LAST_ACK",
               9: "FIN_WAIT2", 10: "TIME_WAIT"}

CONN_LISTEN = "LISTEN"
CONN_ESTABLISHED = "ESTABLISHED"

Addr = namedtuple("Addr", "ip port")
# The attributes of a psutil `sconn` that callers use; `raddr` is () for an unconnected
# socket, as in psutil. `status` is psutil's spelling of the state.
Conn = namedtuple("Conn", "family laddr raddr status pid")
BsdInfo = namedtuple("BsdInfo", "pid ppid uid ruid status name started")


# ---- parsers over plain bytes --------------------------------------------------------------

def parse_bsdinfo(raw):
    """BsdInfo from the PROC_PIDTBSDINFO buffer. `ruid` is the real uid, the one psutil and
    /proc's first Uid: field report."""
    if len(raw) < _BSDINFO_SIZE:
        raise OSError(errno.EINVAL, "short proc_bsdinfo")
    fields = struct.unpack_from(_BSDINFO_FORMAT, raw)
    name = fields[13].split(b"\0", 1)[0] or fields[12].split(b"\0", 1)[0]
    started = fields[_BSD_START_SEC] + fields[_BSD_START_USEC] / 1e6
    return BsdInfo(fields[_BSD_PID], fields[_BSD_PPID], fields[_BSD_UID], fields[_BSD_RUID],
                   fields[_BSD_STATUS], os.fsdecode(name), started)


def parse_procargs(raw):
    """(executable path, argv) from a KERN_PROCARGS2 buffer: an int argc, the executable
    path and its NUL padding, then argc NUL-terminated strings (the environment follows
    and is ignored). An empty argument is an empty string between two NULs."""
    if len(raw) < 5:
        raise OSError(errno.EINVAL, "short KERN_PROCARGS2 buffer")
    (argc,) = struct.unpack_from("=i", raw)
    if argc < 0:
        raise OSError(errno.EINVAL, "negative argc in KERN_PROCARGS2")
    end = raw.find(b"\0", 4)
    if end < 0:
        raise OSError(errno.EINVAL, "no executable path in KERN_PROCARGS2")
    exe = os.fsdecode(raw[4:end])
    pos = end
    while pos < len(raw) and raw[pos] == 0:
        pos += 1
    argv = []
    for _ in range(argc):
        stop = raw.find(b"\0", pos)
        if stop < 0:
            argv.append(os.fsdecode(raw[pos:]))
            break
        argv.append(os.fsdecode(raw[pos:stop]))
        pos = stop + 1
    return exe, argv


def _address(raw, offset, vflag):
    """The address at `offset`: IPv6 when the socket is one, else the IPv4 kept in the last
    four bytes of the 16-byte union. A dual-stack socket carries both flags and its wildcard
    reads as `::`; an IPv4 peer of a dual-stack socket carries only the IPv4 flag."""
    if vflag & _INI_IPV6:
        return socket.inet_ntop(socket.AF_INET6, raw[offset:offset + 16])
    return socket.inet_ntop(socket.AF_INET, raw[offset + 12:offset + 16])


def parse_socket_fdinfo(raw):
    """Conn (with pid None) for the TCP socket in a PROC_PIDFDSOCKETINFO buffer; None for
    anything else (a UDP, Unix-domain or system-control socket) or a short buffer."""
    if len(raw) < _SOCKET_FDINFO_MIN:
        return None
    family, kind = struct.unpack_from("=i", raw, _SI_FAMILY)[0], struct.unpack_from(
        "=i", raw, _SI_KIND)[0]
    if kind != _SOCKINFO_TCP or family not in (_DARWIN_AF_INET, _DARWIN_AF_INET6):
        return None
    fport, lport = struct.unpack_from("=ii", raw, _IN_FPORT)
    vflag = raw[_IN_VFLAG]
    (state,) = struct.unpack_from("=i", raw, _TCP_STATE)
    try:
        local = Addr(_address(raw, _IN_LADDR, vflag), socket.ntohs(lport & 0xFFFF))
        remote = (Addr(_address(raw, _IN_FADDR, vflag), socket.ntohs(fport & 0xFFFF))
                  if fport else ())
    except (OSError, ValueError):
        return None
    ip_family = socket.AF_INET6 if vflag & _INI_IPV6 else socket.AF_INET
    return Conn(ip_family, local, remote, _TCP_STATES.get(state, "NONE"), None)


# ---- the reader ----------------------------------------------------------------------------

def _oserror(err, what):
    return OSError(err or errno.EIO, f"{what}: {os.strerror(err or errno.EIO)}")


def _check_pid(pid):
    """pids are 32-bit ints in the kernel; ctypes would raise ArgumentError, which is not
    an OSError, for the huge value a hostile /health can quote."""
    if not isinstance(pid, int) or isinstance(pid, bool) or not 0 < pid <= 0x7FFFFFFF:
        raise _oserror(errno.ESRCH, f"not a pid: {pid!r}")


class DarwinProbe:
    """libproc and sysctl readers. Raises OSError (or AttributeError) from the constructor
    when the libraries cannot be used, or when the layouts do not match this process."""

    def __init__(self):
        import ctypes
        ct = ctypes
        self._ct = ct
        libc = ct.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        libc.proc_listpids.argtypes = (ct.c_uint32, ct.c_uint32, ct.c_void_p, ct.c_int)
        libc.proc_listpids.restype = ct.c_int
        libc.proc_pidinfo.argtypes = (ct.c_int, ct.c_int, ct.c_uint64, ct.c_void_p, ct.c_int)
        libc.proc_pidinfo.restype = ct.c_int
        libc.proc_pidfdinfo.argtypes = (ct.c_int, ct.c_int, ct.c_int, ct.c_void_p, ct.c_int)
        libc.proc_pidfdinfo.restype = ct.c_int
        libc.proc_pidpath.argtypes = (ct.c_int, ct.c_void_p, ct.c_uint32)
        libc.proc_pidpath.restype = ct.c_int
        libc.sysctl.argtypes = (ct.POINTER(ct.c_int), ct.c_uint, ct.c_void_p,
                                ct.POINTER(ct.c_size_t), ct.c_void_p, ct.c_size_t)
        libc.sysctl.restype = ct.c_int
        self._libc = libc
        self._argmax = None
        self._sockets_checked = None
        self._self_check()

    # ---- single-process readers --------------------------------------------------------

    def bsd_info(self, pid):
        _check_pid(pid)
        ct = self._ct
        buf = ct.create_string_buffer(_BSDINFO_SIZE)
        got = self._libc.proc_pidinfo(pid, _PROC_PIDTBSDINFO, 0, buf, _BSDINFO_SIZE)
        if got != _BSDINFO_SIZE:
            raise _oserror(ct.get_errno() or errno.ESRCH, f"proc_pidinfo({pid})")
        return parse_bsdinfo(buf.raw)

    def image_path(self, pid):
        """The executable file's resolved path (proc_pidpath), readable for most processes."""
        _check_pid(pid)
        ct = self._ct
        buf = ct.create_string_buffer(4096)
        got = self._libc.proc_pidpath(pid, buf, 4096)
        if got <= 0:
            raise _oserror(ct.get_errno() or errno.ESRCH, f"proc_pidpath({pid})")
        return os.fsdecode(buf.raw[:got])

    def _argmax_bytes(self):
        if self._argmax is None:
            ct = self._ct
            mib = (ct.c_int * 2)(_CTL_KERN, _KERN_ARGMAX)
            value = ct.c_int(0)
            size = ct.c_size_t(ct.sizeof(value))
            if self._libc.sysctl(mib, 2, ct.byref(value), ct.byref(size), None, 0) != 0 \
                    or value.value <= 0:
                self._argmax = 1 << 20
            else:
                self._argmax = value.value
        return self._argmax

    def procargs(self, pid, buf=None):
        """(executable path as exec'd, argv) of a process of this user. The executable path
        is the name the process was started with, a symlink such as a venv's bin/python
        included; `image_path` is the file behind it. A scan over many processes passes one
        `buf` (from `_procargs_buffer`) instead of allocating the argmax-sized area each
        time."""
        _check_pid(pid)
        ct = self._ct
        capacity = self._argmax_bytes()
        if buf is None:
            buf = self._procargs_buffer()
        size = ct.c_size_t(capacity)
        mib = (ct.c_int * 3)(_CTL_KERN, _KERN_PROCARGS2, pid)
        if self._libc.sysctl(mib, 3, buf, ct.byref(size), None, 0) != 0:
            raise _oserror(ct.get_errno(), f"sysctl(KERN_PROCARGS2, {pid})")
        return parse_procargs(ct.string_at(buf, size.value))

    def _procargs_buffer(self):
        return self._ct.create_string_buffer(self._argmax_bytes())

    def argv(self, pid):
        return self.procargs(pid)[1]

    def cwd(self, pid):
        """The current directory, or None when it cannot be read (not this user's)."""
        _check_pid(pid)
        ct = self._ct
        capacity = 4096
        buf = ct.create_string_buffer(capacity)
        got = self._libc.proc_pidinfo(pid, _PROC_PIDVNODEPATHINFO, 0, buf, capacity)
        if got < _VNODE_CDIR_PATH + _VNODE_PATH_SIZE:
            return None
        path = buf.raw[_VNODE_CDIR_PATH:_VNODE_CDIR_PATH + _VNODE_PATH_SIZE]
        return os.fsdecode(path.split(b"\0", 1)[0]) or None

    def same_user(self, pid):
        return self.bsd_info(pid).ruid == os.getuid()

    def parent_and_start(self, pid):
        """(parent pid, start time in seconds since the epoch) of a process of this user."""
        info = self.bsd_info(pid)
        return info.ppid, info.started

    def alive(self, pid):
        """True while the process runs. A zombie (exited, not yet reaped by its parent) is
        not alive: the kernel no longer finds it by pid. A process this user may not look
        at still exists."""
        try:
            return self.bsd_info(pid).status != _SZOMB
        except OSError as exc:
            if exc.errno == errno.ESRCH:
                return False
            if exc.errno in (errno.EPERM, errno.EACCES):
                return True
            raise

    def facts(self, pid):
        """(same_user, argv, cwd), as proc_facts.facts() returns them."""
        info = self.bsd_info(pid)
        if info.status == _SZOMB:
            raise _oserror(errno.ESRCH, f"pid {pid} is a zombie")
        if info.ruid != os.getuid():
            return False, [], None
        return True, self.argv(pid), self.cwd(pid)

    # ---- the process list --------------------------------------------------------------

    def all_pids(self):
        ct = self._ct
        size = self._libc.proc_listpids(_PROC_ALL_PIDS, 0, None, 0)
        if size <= 0:
            raise _oserror(ct.get_errno(), "proc_listpids")
        for _ in range(4):
            capacity = size // 4 + 64          # processes start between the two calls
            pids = (ct.c_int * capacity)()
            got = self._libc.proc_listpids(_PROC_ALL_PIDS, 0, pids, ct.sizeof(pids))
            if got <= 0:
                raise _oserror(ct.get_errno(), "proc_listpids")
            if got < ct.sizeof(pids):
                return [pids[i] for i in range(got // 4) if pids[i] > 0]
            size = got * 2
        raise OSError(errno.EIO, "the process list kept growing")

    def holder(self, env_dir):
        """A pid of this user that runs a file under `env_dir`, by the path it was started
        with (argv[0] or the exec path, kept as written: a venv's bin/python is a symlink
        out of the environment, and its resolved path would never match) or by its
        resolved executable. None when there is none."""
        prefix = os.path.realpath(env_dir).rstrip(os.sep) + os.sep
        buf = self._procargs_buffer()
        for pid in self.all_pids():
            try:
                exe, argv = self.procargs(pid, buf)
            except OSError:
                continue                   # gone, or not this user's
            candidates = [exe, argv[0] if argv else ""]
            try:
                candidates.append(self.image_path(pid))
            except OSError:
                pass
            for path in candidates:
                if path and os.path.isabs(path) and _real_dir(path).startswith(prefix):
                    return pid
        return None

    # ---- sockets -----------------------------------------------------------------------

    def _socket_fds(self, pid):
        """The descriptor numbers of the sockets `pid` holds (PROC_PIDLISTFDS). The buffer
        is sized from the kernel's own count (a NULL-buffer call, as psutil and lsof do), with
        room for descriptors opened since; one that still fills up is asked for again."""
        _check_pid(pid)
        ct = self._ct
        for _ in range(4):
            ct.set_errno(0)                # a stale errno must not name the failure below
            needed = self._libc.proc_pidinfo(pid, _PROC_PIDLISTFDS, 0, None, 0)
            if needed <= 0:
                raise _oserror(ct.get_errno() or errno.ESRCH, f"proc_pidinfo({pid}, FDS)")
            capacity = needed + _FD_SLACK
            buf = ct.create_string_buffer(capacity)
            got = self._libc.proc_pidinfo(pid, _PROC_PIDLISTFDS, 0, buf, capacity)
            if got <= 0:
                raise _oserror(ct.get_errno() or errno.ESRCH, f"proc_pidinfo({pid}, FDS)")
            if got < capacity:
                raw = buf.raw[:got]
                return [fd for fd, kind in struct.iter_unpack("=iI", raw[:got - got % 8])
                        if kind == _PROX_FDTYPE_SOCKET]
        raise OSError(errno.EIO, f"the descriptor list of pid {pid} kept growing")

    def process_connections(self, pid):
        """Conn rows (pid filled in) for the TCP sockets of one process. OSError when its
        descriptors cannot be listed."""
        ct = self._ct
        rows = []
        buf = ct.create_string_buffer(_SOCKET_FDINFO_BUFFER)
        for fd in self._socket_fds(pid):
            got = self._libc.proc_pidfdinfo(pid, fd, _PROC_PIDFDSOCKETINFO, buf,
                                            _SOCKET_FDINFO_BUFFER)
            if got < _SOCKET_FDINFO_MIN:
                continue                   # closed since the listing
            row = parse_socket_fdinfo(ct.string_at(buf, got))
            if row is not None:
                rows.append(row._replace(pid=pid))
        return rows

    def connections(self, pids=None):
        """Conn rows for every TCP socket of the processes this user can open (`pids`, or
        all of them). Processes that cannot be read are skipped, never an error. OSError when
        the socket layout fails its self-check: no rows rather than wrong ones."""
        self._require_sockets()
        rows = []
        for pid in (self.all_pids() if pids is None else pids):
            try:
                rows += self.process_connections(pid)
            except OSError:
                continue
        return rows

    def listeners(self, port, claimed=None):
        """[(local address, pid)] of every TCP listener on `port`. With `claimed`, that
        pid's own sockets are read first and an empty list comes back when it listens
        nowhere on `port`, so the whole table is read only when it can matter. OSError when
        the claimed pid's sockets cannot be read (the answer would be a guess). A claimed pid
        that no longer exists listens nowhere, so the answer is an empty list."""
        self._require_sockets()

        def on_port(rows):
            return [(c.laddr.ip, c.pid) for c in rows
                    if c.status == CONN_LISTEN and c.laddr.port == port]
        if claimed is not None:
            try:
                own = on_port(self.process_connections(claimed))
            except OSError as exc:
                if exc.errno != errno.ESRCH:
                    raise
                return []
            if not own:
                return []
        return on_port(self.connections())

    def _require_sockets(self):
        if self._sockets_checked is None:
            # An OSError here is a failure of the probe itself (out of descriptors, a
            # listing that kept growing): it is raised for this call and the check runs
            # again on the next one. Only a verdict is cached.
            self._sockets_checked = self._sockets_make_sense()
        if not self._sockets_checked:
            raise OSError(errno.ENOTSUP, "the socket table layout is not the expected one")

    # ---- the self-check ------------------------------------------------------------------

    def _self_check(self):
        """Refuse to answer when this process cannot be read back: its own pid, owner, argv
        and cwd must come out as the interpreter knows them. A layout that moved reads
        garbage here first."""
        info = self.bsd_info(os.getpid())
        if info.pid != os.getpid() or info.ruid != os.getuid() or info.status == _SZOMB:
            raise OSError(errno.EPROTO, "proc_pidinfo does not describe this process")
        exe, argv = self.procargs(os.getpid())
        if not argv or not exe:
            raise OSError(errno.EPROTO, "KERN_PROCARGS2 does not describe this process")
        try:
            expected = os.path.realpath(os.getcwd())
        except OSError:
            return                         # the directory is gone: nothing to compare with
        here = self.cwd(os.getpid())
        if here is None or os.path.realpath(here) != expected:
            raise OSError(errno.EPROTO, "PROC_PIDVNODEPATHINFO does not name the cwd")

    def _sockets_make_sense(self):
        """A listener opened here must show up in this process's own socket list with its
        port, which is the check that the socket layout offsets are right. True or False is
        a verdict on the layout. An OSError from the probe (EMFILE opening its socket, a
        descriptor list that kept growing) says nothing about the layout, so it propagates
        for the caller to retry rather than to cache."""
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            sock.listen(1)
            port = sock.getsockname()[1]
            rows = self.process_connections(os.getpid())
        return any(c.status == CONN_LISTEN and c.laddr.port == port
                   and c.laddr.ip == "127.0.0.1" for c in rows)


def _real_dir(path):
    """`path` with its directory resolved and its last component left alone: the real
    location of a file named by a symlink, without following the symlink itself."""
    path = os.path.abspath(path)
    head, tail = os.path.split(path)
    return os.path.join(os.path.realpath(head), tail)
