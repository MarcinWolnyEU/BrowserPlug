r"""Who owns a process, what it was started with, and where it runs, from the standard library.

mcp_port.server_pid_is_ours needs three facts about the pid a /health names: whether its
owner is the current user, its argv, and its cwd. The launcher reads them on whatever
interpreter a client registered, usually uv's bare Python with no site-packages, so they are
read natively rather than through psutil:

- Windows: ctypes over kernel32, advapi32, ntdll and shell32. The owner is the token's user
  SID compared with EqualSid. argv is NtQueryInformationProcess(ProcessCommandLineInformation)
  split by CommandLineToArgvW, the split psutil uses. The cwd comes from the target's PEB at
  the offsets psutil reads (the 32-bit PEB of a WOW64 process); a 32-bit reader gets none.
- Linux: the real uid on the `Uid:` line of /proc/<pid>/status, /proc/<pid>/cmdline split on
  NUL, and the /proc/<pid>/cwd link.
- Anywhere else (macOS, the BSDs): psutil when it can be imported (`psutil_holder` also tells
  server_env whether a process runs from the server environment), else ProbeUnavailable, which
  server_pid_is_ours turns into "trust /health and warn once".

`pid_owns_listener(pid, port)` ties a pid to the socket that answers on a port: the TCP table
with owning pids (GetExtendedTcpTable) on Windows, /proc/net/tcp plus the pid's socket inodes
on Linux. `pid_alive(pid)` says whether a process is still running (a zombie, or an exited
process whose handle someone holds, is not). `pids_running_image(path)` lists the processes
whose image is a file, from the process list, without opening the file (Windows only).

`facts(pid)` returns (same_user, argv, cwd). OSError means the process could not be read
(gone, protected, not a pid), which the caller counts as foreign. A process of another user
comes back as (False, [], None) without its argv being read. An unreadable cwd alone is None,
not an error: only the relative and `-m` launch forms need it.

WindowsProbe also answers the image path, creation time and parent pid that
launcher/client_pid.py walks the client chain with, so the launcher has one ctypes surface.
tests/unit/test_proc_facts.py compares every reading with psutil on live processes, which is
what catches a PEB offset that a future Windows moves.

Stdlib-only: the launcher imports this (tests/unit/test_import_boundaries.py).
"""

import ipaddress
import os
import socket
import struct
import sys

_IS_WINDOWS = os.name == "nt"

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_SYNCHRONIZE = 0x00100000
_WAIT_TIMEOUT = 0x102
_ERROR_ACCESS_DENIED = 5
_PROCESS_QUERY_INFORMATION = 0x0400
_PROCESS_VM_READ = 0x0010
_TOKEN_QUERY = 0x0008
_TOKEN_USER = 1                              # TOKEN_INFORMATION_CLASS.TokenUser
_PROCESS_BASIC_INFORMATION = 0               # PROCESSINFOCLASS values
_PROCESS_WOW64_INFORMATION = 26
_PROCESS_COMMAND_LINE_INFORMATION = 60
_FILETIME_TICKS_PER_SECOND = 10_000_000
# PEB.ProcessParameters, then RTL_USER_PROCESS_PARAMETERS.CurrentDirectory.DosPath: the x64
# layout, and the 32-bit one a WOW64 process keeps beside it.
_PEB_PROCESS_PARAMETERS = 0x20
_PARAMS_CURRENT_DIRECTORY = 0x38
_PEB32_PROCESS_PARAMETERS = 0x10
_PARAMS32_CURRENT_DIRECTORY = 0x24


class ProbeUnavailable(Exception):
    """This platform has no native probe and psutil cannot be imported."""


class WindowsProbe:
    """Facts about a pid via kernel32, advapi32, ntdll and shell32.

    Each instance loads its own WinDLL objects, so the argtypes set here never collide with
    another module's use of ctypes.windll."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes
        ct, wt = ctypes, wintypes
        self._ct, self._wt = ct, wt
        k32 = ct.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wt.HANDLE
        k32.OpenProcess.argtypes = (wt.DWORD, wt.BOOL, wt.DWORD)
        k32.CloseHandle.argtypes = (wt.HANDLE,)
        k32.WaitForSingleObject.restype = wt.DWORD
        k32.WaitForSingleObject.argtypes = (wt.HANDLE, wt.DWORD)
        k32.GetCurrentProcess.restype = wt.HANDLE
        k32.QueryFullProcessImageNameW.argtypes = (
            wt.HANDLE, wt.DWORD, wt.LPWSTR, ct.POINTER(wt.DWORD))
        k32.GetProcessTimes.argtypes = (wt.HANDLE,) + (ct.POINTER(wt.FILETIME),) * 4
        k32.ReadProcessMemory.argtypes = (wt.HANDLE, ct.c_void_p, ct.c_void_p, ct.c_size_t,
                                          ct.POINTER(ct.c_size_t))
        k32.LocalFree.argtypes = (ct.c_void_p,)
        k32.LocalFree.restype = ct.c_void_p
        adv = ct.WinDLL("advapi32", use_last_error=True)
        adv.OpenProcessToken.argtypes = (wt.HANDLE, wt.DWORD, ct.POINTER(wt.HANDLE))
        adv.GetTokenInformation.argtypes = (wt.HANDLE, ct.c_int, ct.c_void_p, wt.DWORD,
                                            ct.POINTER(wt.DWORD))
        adv.EqualSid.argtypes = (ct.c_void_p, ct.c_void_p)
        ntdll = ct.WinDLL("ntdll")
        ntdll.NtQueryInformationProcess.argtypes = (
            wt.HANDLE, ct.c_ulong, ct.c_void_p, ct.c_ulong, ct.POINTER(ct.c_ulong))
        iphlpapi = ct.WinDLL("iphlpapi", use_last_error=True)
        iphlpapi.GetExtendedTcpTable.restype = wt.DWORD
        iphlpapi.GetExtendedTcpTable.argtypes = (
            ct.c_void_p, ct.POINTER(wt.DWORD), wt.BOOL, wt.ULONG, wt.ULONG, wt.ULONG)
        psapi = ct.WinDLL("psapi", use_last_error=True)
        psapi.EnumProcesses.argtypes = (ct.POINTER(wt.DWORD), wt.DWORD, ct.POINTER(wt.DWORD))
        shell32 = ct.WinDLL("shell32", use_last_error=True)
        shell32.CommandLineToArgvW.restype = ct.POINTER(wt.LPWSTR)
        shell32.CommandLineToArgvW.argtypes = (wt.LPCWSTR, ct.POINTER(ct.c_int))

        class UnicodeString(ct.Structure):
            _fields_ = [("Length", ct.c_ushort), ("MaximumLength", ct.c_ushort),
                        ("Buffer", ct.c_void_p)]

        class ProcessBasicInformation(ct.Structure):
            _fields_ = [("Reserved1", ct.c_void_p), ("PebBaseAddress", ct.c_void_p),
                        ("Reserved2", ct.c_void_p * 2), ("UniqueProcessId", ct.c_size_t),
                        ("InheritedFromUniqueProcessId", ct.c_size_t)]

        self._k32, self._adv, self._ntdll, self._shell32 = k32, adv, ntdll, shell32
        self._iphlpapi, self._psapi = iphlpapi, psapi
        self._UnicodeString = UnicodeString
        self._ProcessBasicInformation = ProcessBasicInformation

    def _open(self, pid, access=_PROCESS_QUERY_LIMITED_INFORMATION):
        handle = self._k32.OpenProcess(access, False, pid)
        if not handle:
            raise OSError(self._ct.get_last_error(), f"cannot open pid {pid}")
        return handle

    # ---- what launcher/client_pid.py walks the chain with ----------------------------

    def image_path(self, pid):
        handle = self._open(pid)
        try:
            size = self._wt.DWORD(32768)
            buf = self._ct.create_unicode_buffer(size.value)
            if not self._k32.QueryFullProcessImageNameW(handle, 0, buf, self._ct.byref(size)):
                raise OSError(f"no image path for pid {pid}")
            return buf.value
        finally:
            self._k32.CloseHandle(handle)

    def command_line(self, pid):
        ct = self._ct
        handle = self._open(pid)
        try:
            needed = ct.c_ulong(0)
            # The first call only reports the size; its status is "length mismatch" by design.
            self._ntdll.NtQueryInformationProcess(
                handle, _PROCESS_COMMAND_LINE_INFORMATION, None, 0, ct.byref(needed))
            buf = ct.create_string_buffer(max(needed.value, ct.sizeof(self._UnicodeString)))
            status = self._ntdll.NtQueryInformationProcess(
                handle, _PROCESS_COMMAND_LINE_INFORMATION, buf, len(buf), ct.byref(needed))
            if status != 0:
                raise OSError(f"no command line for pid {pid}: {status & 0xFFFFFFFF:#x}")
            text = self._UnicodeString.from_buffer(buf)
            # The kernel points Buffer into our own buffer, right after the structure.
            return ct.wstring_at(text.Buffer, text.Length // 2) if text.Buffer else ""
        finally:
            self._k32.CloseHandle(handle)

    def created(self, pid):
        """Creation time in seconds on the FILETIME clock; only differences mean anything."""
        handle = self._open(pid)
        try:
            ft = [self._wt.FILETIME() for _ in range(4)]
            if not self._k32.GetProcessTimes(handle, *[self._ct.byref(f) for f in ft]):
                raise OSError(f"no times for pid {pid}")
            return ((ft[0].dwHighDateTime << 32) | ft[0].dwLowDateTime) / _FILETIME_TICKS_PER_SECOND
        finally:
            self._k32.CloseHandle(handle)

    def parent(self, pid):
        ct = self._ct
        handle = self._open(pid)
        try:
            info = self._ProcessBasicInformation()
            returned = ct.c_ulong(0)
            status = self._ntdll.NtQueryInformationProcess(
                handle, _PROCESS_BASIC_INFORMATION, ct.byref(info), ct.sizeof(info),
                ct.byref(returned))
            if status != 0:
                raise OSError(f"NtQueryInformationProcess failed: {status & 0xFFFFFFFF:#x}")
            return int(info.InheritedFromUniqueProcessId)
        finally:
            self._k32.CloseHandle(handle)

    def alive(self, pid):
        """True while the process runs. An exited process whose object someone still holds
        opens fine but is signalled, which is how it is told from a live one."""
        handle = self._k32.OpenProcess(_SYNCHRONIZE | _PROCESS_QUERY_LIMITED_INFORMATION,
                                       False, pid)
        if not handle:
            # Access denied means it exists and is not ours to open; anything else, gone.
            return self._ct.get_last_error() == _ERROR_ACCESS_DENIED
        try:
            return self._k32.WaitForSingleObject(handle, 0) == _WAIT_TIMEOUT
        finally:
            self._k32.CloseHandle(handle)

    # ---- the process list and the TCP table -----------------------------------------

    def all_pids(self):
        wt = self._wt
        size = 1024
        while True:
            pids = (wt.DWORD * size)()
            returned = wt.DWORD(0)
            if not self._psapi.EnumProcesses(pids, wt.DWORD(size * 4), self._ct.byref(returned)):
                raise OSError(self._ct.get_last_error(), "EnumProcesses failed")
            count = returned.value // 4
            if count < size:
                return [pids[i] for i in range(count)]
            size *= 4        # a full buffer may have cut the list off

    def pids_running_image(self, path):
        """Pids whose image file is `path`. Processes this one may not query are skipped:
        they are another user's or protected, and not the env's trampoline."""
        want = os.path.normcase(os.path.realpath(path))
        base = os.path.basename(want)
        found = []
        for pid in self.all_pids():
            try:
                image = self.image_path(pid)
            except OSError:
                continue
            if os.path.basename(os.path.normcase(image)) != base:
                continue
            if os.path.normcase(os.path.realpath(image)) == want:
                found.append(pid)
        return found

    def listeners(self, port):
        """[(local address, owning pid)] of every TCP listener on `port`, IPv4 and IPv6."""
        out = []
        for family, row_size in ((socket.AF_INET, 24), (socket.AF_INET6, 56)):
            raw = self._tcp_table(family)
            (count,) = struct.unpack_from("<I", raw, 0)
            for index in range(count):
                row = raw[4 + index * row_size:4 + (index + 1) * row_size]
                if family == socket.AF_INET:
                    _state, addr, local_port, _ra, _rp, owner = struct.unpack("<6I", row)
                    address = socket.inet_ntoa(struct.pack("<I", addr))
                else:
                    address = str(ipaddress.IPv6Address(row[:16]))
                    (local_port,) = struct.unpack_from("<I", row, 20)
                    (owner,) = struct.unpack_from("<I", row, 52)
                if socket.ntohs(local_port & 0xFFFF) == port:
                    out.append((address, owner))
        return out

    def _tcp_table(self, family):
        ct, wt = self._ct, self._wt
        listener_class = 3          # TCP_TABLE_OWNER_PID_LISTENER
        size = wt.DWORD(0)
        for _ in range(5):          # the table can grow between the size query and the read
            buf = ct.create_string_buffer(size.value or 1)
            status = self._iphlpapi.GetExtendedTcpTable(buf, ct.byref(size), False, family,
                                                        listener_class, 0)
            if status == 0:
                return buf.raw[:size.value]
            if status != 122:       # ERROR_INSUFFICIENT_BUFFER
                raise OSError(status, f"GetExtendedTcpTable failed: {status}")
        raise OSError("GetExtendedTcpTable kept growing")

    # ---- what server_pid_is_ours needs -------------------------------------------------

    def _user_sid(self, process_handle):
        """The TOKEN_USER buffer of a process; its first field points at the SID inside it,
        so the buffer must outlive every use of that pointer."""
        ct, wt = self._ct, self._wt
        token = wt.HANDLE()
        if not self._adv.OpenProcessToken(process_handle, _TOKEN_QUERY, ct.byref(token)):
            raise OSError(ct.get_last_error(), "OpenProcessToken failed")
        try:
            needed = wt.DWORD(0)
            self._adv.GetTokenInformation(token, _TOKEN_USER, None, 0, ct.byref(needed))
            if not needed.value:
                raise OSError(ct.get_last_error(), "GetTokenInformation reported no size")
            buf = ct.create_string_buffer(needed.value)
            if not self._adv.GetTokenInformation(token, _TOKEN_USER, buf, needed,
                                                 ct.byref(needed)):
                raise OSError(ct.get_last_error(), "GetTokenInformation failed")
            return buf
        finally:
            self._k32.CloseHandle(token)

    def same_user(self, pid):
        handle = self._open(pid)
        try:
            theirs = self._user_sid(handle)
        finally:
            self._k32.CloseHandle(handle)
        mine = self._user_sid(self._k32.GetCurrentProcess())

        def sid(buf):
            return self._ct.cast(buf, self._ct.POINTER(self._ct.c_void_p))[0]
        return bool(self._adv.EqualSid(sid(theirs), sid(mine)))

    def argv(self, pid):
        ct = self._ct
        line = self.command_line(pid)
        if not line:
            return []        # CommandLineToArgvW("") answers this process's own path
        argc = ct.c_int(0)
        array = self._shell32.CommandLineToArgvW(line, ct.byref(argc))
        if not array:
            raise OSError(ct.get_last_error(), f"CommandLineToArgvW failed for pid {pid}")
        try:
            return [array[i] for i in range(argc.value)]
        finally:
            self._k32.LocalFree(array)

    def _read(self, handle, address, size):
        ct = self._ct
        buf = ct.create_string_buffer(size)
        got = ct.c_size_t(0)
        if not self._k32.ReadProcessMemory(handle, ct.c_void_p(address), buf, size,
                                           ct.byref(got)):
            raise OSError(ct.get_last_error(), "ReadProcessMemory failed")
        return buf.raw[:got.value]

    def _cwd_string(self, handle, peb, wide):
        """(Buffer, Length) of CurrentDirectory.DosPath, read from the PEB at `peb`: the x64
        layout when `wide`, else the 32-bit one."""
        if wide:
            params = int.from_bytes(self._read(handle, peb + _PEB_PROCESS_PARAMETERS, 8),
                                    "little")
            if not params:
                return 0, 0
            raw = self._read(handle, params + _PARAMS_CURRENT_DIRECTORY, 16)
            return int.from_bytes(raw[8:16], "little"), int.from_bytes(raw[0:2], "little")
        params = int.from_bytes(self._read(handle, peb + _PEB32_PROCESS_PARAMETERS, 4), "little")
        if not params:
            return 0, 0
        raw = self._read(handle, params + _PARAMS32_CURRENT_DIRECTORY, 8)
        return int.from_bytes(raw[4:8], "little"), int.from_bytes(raw[0:2], "little")

    def cwd(self, pid):
        """The current directory, or None when it cannot be read: a 32-bit reader, or a
        process this one may not read the memory of."""
        ct = self._ct
        if ct.sizeof(ct.c_void_p) != 8:
            return None
        try:
            handle = self._open(pid, _PROCESS_QUERY_INFORMATION | _PROCESS_VM_READ)
        except OSError:
            return None
        try:
            # A WOW64 process answers the address of its 32-bit PEB here, anything else 0.
            peb32 = ct.c_void_p(0)
            self._ntdll.NtQueryInformationProcess(handle, _PROCESS_WOW64_INFORMATION,
                                                  ct.byref(peb32), ct.sizeof(peb32), None)
            if peb32.value:
                buffer, length = self._cwd_string(handle, peb32.value, wide=False)
            else:
                info = self._ProcessBasicInformation()
                status = self._ntdll.NtQueryInformationProcess(
                    handle, _PROCESS_BASIC_INFORMATION, ct.byref(info), ct.sizeof(info), None)
                if status != 0 or not info.PebBaseAddress:
                    return None
                buffer, length = self._cwd_string(handle, info.PebBaseAddress, wide=True)
            if not buffer or not length:
                return None
            path = self._read(handle, buffer, length).decode("utf-16-le", "replace")
        except OSError:
            return None
        finally:
            self._k32.CloseHandle(handle)
        # The kernel keeps a trailing backslash on every directory but a drive root; psutil
        # strips it, and so does this.
        if len(path) > 3 and path.endswith("\\"):
            path = path[:-1]
        return path

    def facts(self, pid):
        if not self.same_user(pid):
            return False, [], None
        return True, self.argv(pid), self.cwd(pid)


_windows_probe = None


def _windows():
    global _windows_probe
    if _windows_probe is None:
        _windows_probe = WindowsProbe()
    return _windows_probe


# ---- Linux -------------------------------------------------------------------------------

def parse_status_uid(text):
    """The real uid from /proc/<pid>/status (the first number on its `Uid:` line)."""
    for line in text.splitlines():
        if line.startswith("Uid:"):
            fields = line.split()
            if len(fields) >= 2 and fields[1].isdigit():
                return int(fields[1])
    raise OSError("no Uid: line in /proc/<pid>/status")


def parse_cmdline(raw):
    """argv from /proc/<pid>/cmdline: NUL-separated, usually NUL-terminated."""
    if not raw:
        return []
    parts = raw.split(b"\0")
    if parts and parts[-1] == b"":
        parts.pop()
    return [os.fsdecode(part) for part in parts]


def linux_facts(pid, proc_root="/proc", uid=None):
    """(same_user, argv, cwd) read from `proc_root`; `uid` defaults to os.getuid()."""
    base = os.path.join(proc_root, str(pid))
    with open(os.path.join(base, "status"), "r", encoding="utf-8", errors="replace") as fh:
        owner = parse_status_uid(fh.read())
    if owner != (os.getuid() if uid is None else uid):
        return False, [], None
    with open(os.path.join(base, "cmdline"), "rb") as fh:
        argv = parse_cmdline(fh.read())
    try:
        cwd = os.readlink(os.path.join(base, "cwd"))
    except OSError:
        cwd = None
    return True, argv, cwd


# ---- which process owns the socket that answers on a port ----------------------------------

# A connection to 127.0.0.1:port reaches the most specific listener: one bound to 127.0.0.1
# itself, else the IPv4 wildcard, else an IPv6 one (dual-stack). On Windows a bind to
# 127.0.0.1 succeeds beside a wildcard listener of another process, and then wins every
# loopback connection, so "the pid listens on the port" is not enough.
_REACH_ORDER = ("127.0.0.1", "0.0.0.0", "::", "::1")


def reached_owners(entries):
    """The owners a connection to 127.0.0.1 can reach, from [(local address, owner)] of one
    port's listeners: those of the first tier of _REACH_ORDER that has any. Empty when none
    of the listeners is reachable that way. An IPv4-mapped IPv6 address counts as IPv4."""
    tiers = {}
    for address, owner in entries:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            continue
        if ip.version == 6 and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        tiers.setdefault(str(ip), set()).add(owner)
    for name in _REACH_ORDER:
        if tiers.get(name):
            return tiers[name]
    return set()


def parse_proc_net_tcp(text):
    """[(local address, port, inode)] of the LISTEN rows of /proc/net/tcp or tcp6."""
    rows = []
    for line in text.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 10 or fields[3] != "0A":
            continue
        host, _, port_hex = fields[1].rpartition(":")
        try:
            raw = bytes.fromhex(host)
            if len(raw) not in (4, 16):
                continue
            words = [raw[i:i + 4] for i in range(0, len(raw), 4)]
            # Each 32-bit word is printed in the host's byte order.
            ordered = b"".join(w[::-1] if sys.byteorder == "little" else w for w in words)
            ip = ipaddress.ip_address(ordered)
            # Some 3.12 patch releases print a mapped IPv4 as ::ffff:7f00:1; callers match the dotted form.
            address = f"::ffff:{ip.ipv4_mapped}" if ip.version == 6 and ip.ipv4_mapped else str(ip)
            rows.append((address, int(port_hex, 16), int(fields[9])))
        except ValueError:
            continue
    return rows


def socket_inodes(pid, proc_root="/proc"):
    """The inodes of the sockets pid holds open (/proc/<pid>/fd links `socket:[inode]`)."""
    base = os.path.join(proc_root, str(pid), "fd")
    inodes = set()
    for name in os.listdir(base):
        try:
            target = os.readlink(os.path.join(base, name))
        except OSError:
            continue        # closed while we looked
        if target.startswith("socket:[") and target.endswith("]"):
            inodes.add(int(target[8:-1]))
    return inodes


def linux_owns_listener(pid, port, proc_root="/proc"):
    entries = []
    for name in ("tcp", "tcp6"):
        try:
            with open(os.path.join(proc_root, "net", name), "r", encoding="utf-8") as fh:
                text = fh.read()
        except FileNotFoundError:       # no IPv6 stack
            continue
        entries += [(addr, inode) for addr, p, inode in parse_proc_net_tcp(text) if p == port]
    owners = reached_owners(entries)
    return bool(owners) and owners <= socket_inodes(pid, proc_root)


def pid_owns_listener(pid, port):
    """True when `pid` is the only process a connection to 127.0.0.1:`port` can reach. False
    when another process is, or nothing listens. OSError when the table cannot be read;
    ProbeUnavailable on a platform without a native reader."""
    if _IS_WINDOWS:
        owners = reached_owners(_windows().listeners(port))
        return owners == {pid}
    if sys.platform.startswith("linux") and os.path.isdir("/proc/net"):
        return linux_owns_listener(pid, port)
    raise ProbeUnavailable(f"no native socket-owner probe on {sys.platform}")


def pid_alive(pid):
    """True when process `pid` is running."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    if _IS_WINDOWS:
        return _windows().alive(pid)
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            # "pid (comm) S ...": comm may hold spaces and parentheses, so cut at the last ")".
            state = fh.read().rpartition(b")")[2].split()[0]
        return state not in (b"Z", b"X")
    except (FileNotFoundError, ProcessLookupError):
        if os.path.isdir("/proc/self"):
            return False
    except (OSError, IndexError):
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True         # PermissionError: it exists, and is not ours
    return True


def pids_running_image(path):
    """Pids whose image is the file `path` (Windows). OSError when the process list cannot
    be read; ProbeUnavailable elsewhere."""
    if _IS_WINDOWS:
        return _windows().pids_running_image(path)
    raise ProbeUnavailable(f"no process-image probe on {sys.platform}")


# ---- elsewhere ------------------------------------------------------------------------

def _psutil_facts(pid):
    try:
        import psutil
    except ImportError:
        raise ProbeUnavailable(f"no native process probe on {sys.platform} and psutil is not "
                               f"importable") from None
    try:
        proc = psutil.Process(pid)
        if proc.username() != psutil.Process().username():
            return False, [], None
        argv = list(proc.cmdline())
    except psutil.Error as exc:     # not an OSError subclass: the caller expects one
        raise OSError(f"pid {pid}: {exc}") from None
    try:
        cwd = proc.cwd()
    except psutil.Error:
        cwd = None
    return True, argv, cwd


def psutil_holder(env_dir):
    """A pid whose executable or argv[0] lies under `env_dir`, by psutil; None when there is
    none or psutil is not importable (a bare interpreter on macOS or a BSD)."""
    try:
        import psutil
    except ImportError:
        return None

    def norm(path):
        return os.path.normcase(os.path.realpath(path))
    prefix = norm(env_dir).rstrip(os.sep) + os.sep
    for proc in psutil.process_iter(["pid", "exe", "cmdline"]):
        try:
            info = proc.info
            argv0 = (info.get("cmdline") or [""])[0]
            for path in (info.get("exe") or "", argv0):
                if path and norm(path).startswith(prefix):
                    return info["pid"]
        except (psutil.Error, OSError):
            continue
    return None


def facts(pid):
    """(same_user, argv, cwd) of `pid`. Raises OSError when it cannot be read and
    ProbeUnavailable when this platform has no way to read it."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise OSError(f"not a pid: {pid!r}")
    if _IS_WINDOWS:
        return _windows().facts(pid)
    if sys.platform.startswith("linux") and os.path.isdir("/proc/self"):
        return linux_facts(pid)
    return _psutil_facts(pid)
