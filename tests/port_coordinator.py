"""
Dynamic Port Coordination System for BrowserPlug Testing

Every port handed out here is allocated at RUN TIME and verified free, so two
concurrent pytest sessions (e.g. `pytest unit/` started while `pytest integration/`
is running) can never end up on the same port.

Main API:
- get_port_by_type(port_type): the session's port for a named role (stable per process)
- allocate_port(): a fresh, genuinely-free port (never handed out twice in this process)
- coordinated_test_ports(): context manager yielding a fresh (websocket, mcp) pair
- dead_port(): context manager yielding a claimed port that nothing listens on

Port types (each is allocated once per process, on first use):
- 'websocket'           : the session's main WebSocket server port
- 'mcp'                 : the session's main MCP server port
- 'test_individual'     : per-test WebSocket server port
- 'test_mcp_individual' : per-test MCP server port

How "free" is decided (this is load-bearing):
  The probe binds with SO_EXCLUSIVEADDRUSE on Windows, NEVER SO_REUSEADDR. A
  SO_REUSEADDR bind on Windows SUCCEEDS on a port another process is actively
  LISTENING on when that listener also set SO_REUSEADDR — which uvicorn (our MCP
  HTTP server) does. The old probe did exactly that, so "is this port free?" always
  answered yes and two sessions silently shared a port: a test could reach the OTHER
  session's server (e.g. fetch a ws-token from it). SO_EXCLUSIVEADDRUSE refuses
  (WSAEADDRINUSE) instead, which is the whole point of the probe.

Cross-process coordination:
  The probe only sees ports that are already BOUND. Two sessions could still pick the
  same port in the window between "probed free" and "server actually binds it", so a
  port is also claimed with an O_EXCL lock file in a shared temp dir for the lifetime
  of the process. Stale claims (from a killed session) are purged on first use.
"""

import atexit
import errno
import json
import os
import random
import socket
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Optional

# Allocation pool. Deliberately below the Windows dynamic/ephemeral range (49152+)
# so the OS never hands one of these to an unrelated outbound socket.
PORT_POOL_START = 40000
PORT_POOL_END = 48999

# The named roles callers ask for. Each is allocated once per process (see
# get_port_by_type) — a role is a stable name for a port, not a fixed number.
PORT_TYPES = ('websocket', 'mcp', 'test_individual', 'test_mcp_individual')

# Where a port is claimed for other PROCESSES to see. One file per claimed port.
RESERVATION_DIR = Path(tempfile.gettempdir()) / 'browserplug-test-ports'

# A claim older than this is assumed to belong to a session that died without
# cleaning up (a backstop for when the owning PID can't be checked).
_RESERVATION_MAX_AGE = 24 * 3600

# Bind errors that mean "somebody else has this port".
_PORT_BUSY_ERRNOS = {errno.EADDRINUSE, errno.EACCES, 10013, 10048}

_LOCK = threading.RLock()
_ALLOCATED = set()          # ports this process has handed out
_TYPE_PORTS = {}            # port_type -> port, memoized per process
_RESERVED_FILES = {}        # port -> claim file path (this process owns these)
_PURGED = False
_RANDOM = random.Random((os.getpid() << 16) ^ int(time.time() * 1000))


def _bind_probe(family, addr, port):
    """Bind `port` the strictest way the platform allows. Raises OSError if taken.

    SO_EXCLUSIVEADDRUSE (Windows) refuses the bind if ANY socket already holds the
    port, whatever options that socket set. Never use SO_REUSEADDR here — it is what
    made the old probe hand out ports that had a live server on them.
    """
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        sock.bind((addr, port))


def is_port_free(port: int) -> bool:
    """True when `port` can actually be bound on loopback right now.

    Servers here bind "localhost", which resolves to BOTH 127.0.0.1 and ::1, so a
    port is only usable if both families are free.
    """
    try:
        _bind_probe(socket.AF_INET, '127.0.0.1', port)
    except OSError:
        return False

    if socket.has_ipv6:
        try:
            _bind_probe(socket.AF_INET6, '::1', port)
        except OSError as e:
            # Busy means busy; anything else (no IPv6 stack, address unavailable)
            # just means this probe can't speak — don't reject the port for it.
            if e.errno in _PORT_BUSY_ERRNOS:
                return False

    return True


def _pid_alive(pid: int) -> bool:
    """Whether `pid` is still running. NEVER use os.kill(pid, 0) here: on Windows
    os.kill TERMINATES the target for any signal that isn't CTRL_C/CTRL_BREAK.
    """
    try:
        import psutil
        return psutil.pid_exists(pid)
    except Exception:
        return True  # can't tell — let the age backstop handle it


def _purge_stale_reservations():
    """Drop claim files left behind by sessions that died (killed run, crash)."""
    global _PURGED
    if _PURGED:
        return
    _PURGED = True
    try:
        RESERVATION_DIR.mkdir(parents=True, exist_ok=True)
        entries = list(RESERVATION_DIR.iterdir())
    except OSError:
        return

    now = time.time()
    for entry in entries:
        if not entry.name.startswith('port-'):
            continue
        try:
            age = now - entry.stat().st_mtime
            try:
                pid = int(entry.read_text().strip() or 0)
            except (OSError, ValueError):
                pid = 0
            if age > _RESERVATION_MAX_AGE or (pid and not _pid_alive(pid)):
                entry.unlink()
        except OSError:
            continue  # someone else is using or removing it — fine either way


def _claim(port: int) -> bool:
    """Claim `port` for this process so other sessions skip it. False if already claimed."""
    path = RESERVATION_DIR / f'port-{port}.lock'
    try:
        RESERVATION_DIR.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    except OSError:
        # No shared temp dir? Fall back to probe-only allocation rather than failing.
        return True
    try:
        os.write(fd, str(os.getpid()).encode())
    finally:
        os.close(fd)
    _RESERVED_FILES[port] = path
    return True


def _unclaim(port: int):
    path = _RESERVED_FILES.pop(port, None)
    if path is not None:
        try:
            path.unlink()
        except OSError:
            pass


@atexit.register
def _release_all_reservations():
    for port in list(_RESERVED_FILES):
        _unclaim(port)


def allocate_port(max_attempts: int = 300) -> int:
    """Return a port that is genuinely free right now and not already ours.

    Retries other candidates instead of exploding when a probe fails — the port may
    have been taken between the scan and the probe (and the caller binds a moment
    later still, so a lost race here is normal, not fatal).
    """
    _purge_stale_reservations()

    with _LOCK:
        for _ in range(max_attempts):
            port = _RANDOM.randint(PORT_POOL_START, PORT_POOL_END)
            if port in _ALLOCATED:
                continue
            if not _claim(port):
                continue          # another session called dibs
            if not is_port_free(port):
                _unclaim(port)    # something is actually listening there
                continue
            _ALLOCATED.add(port)
            return port

    raise RuntimeError(
        f"No free port found in {PORT_POOL_START}-{PORT_POOL_END} "
        f"after {max_attempts} attempts ({len(_ALLOCATED)} already allocated here)"
    )


def get_port_by_type(port_type: str) -> int:
    """The port this process uses for a named role, allocated on first use.

    Stable within a session (callers all over the suite ask for the same role and
    must agree), unique across sessions (that is the bug this fixes).
    """
    if port_type not in PORT_TYPES:
        raise ValueError(f"Invalid port type '{port_type}'. Available types: {list(PORT_TYPES)}")

    with _LOCK:
        if port_type not in _TYPE_PORTS:
            _TYPE_PORTS[port_type] = allocate_port()
        return _TYPE_PORTS[port_type]


def release_port(port: int):
    """Give a port back: it can be re-allocated, and other sessions may claim it."""
    with _LOCK:
        _ALLOCATED.discard(port)
        for port_type, allocated in list(_TYPE_PORTS.items()):
            if allocated == port:
                del _TYPE_PORTS[port_type]
        _unclaim(port)


class PortCoordinator:
    """Thin OO wrapper over the module-level allocator (kept for existing callers).

    The registry is per PROCESS, not per instance — two coordinators must never hand
    out the same port.
    """

    def __init__(self):
        self.coordination_file = None

    @property
    def allocated_ports(self):
        return set(_ALLOCATED)

    def allocate_port(self) -> int:
        return allocate_port()

    def get_port_by_type(self, port_type: str) -> int:
        return get_port_by_type(port_type)

    def release_port(self, port: int):
        release_port(port)

    def release_all_ports(self):
        for port in list(_ALLOCATED):
            release_port(port)

    def release_ports(self, ports: Dict[str, int]):
        for port in ports.values():
            release_port(port)

    def create_coordination_file(self, ports: Dict[str, int]) -> str:
        """Create a temporary file with port coordination info"""
        fd, path = tempfile.mkstemp(prefix='browserplug-ports-', suffix='.json')

        try:
            with os.fdopen(fd, 'w') as f:
                coordination_data = {
                    'websocket_port': ports['websocket'],
                    'mcp_port': ports['mcp'],
                    'hostname': 'localhost',
                    'timestamp': str(int(time.time()))
                }
                json.dump(coordination_data, f, indent=2)

            self.coordination_file = path
            return path

        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.unlink(path)
            except OSError:
                pass
            raise

    def read_coordination_file(self, file_path: str) -> Optional[Dict[str, int]]:
        """Read port coordination from file"""
        try:
            with open(file_path, 'r') as f:
                data = json.load(f)
                return {
                    'websocket': data['websocket_port'],
                    'mcp': data['mcp_port']
                }
        except Exception:
            return None

    def cleanup(self):
        """Clean up coordination file"""
        if self.coordination_file and os.path.exists(self.coordination_file):
            try:
                os.unlink(self.coordination_file)
            except OSError:
                pass
            self.coordination_file = None


def coordinated_mcp_port(coordination_file):
    """The MCP HTTP port a coordination file names, or None (no file, unreadable, stale).

    The managers seed it as the extension's `mcpPort` setting: the extension fetches its
    first token from /ws-token only on that port or inside the production seek range, and
    a test server's MCP port is random.
    """
    if not coordination_file:
        return None
    ports = PortCoordinator().read_coordination_file(coordination_file)
    return ports.get('mcp') if ports else None


@contextmanager
def coordinated_test_ports():
    """Yield a FRESH (websocket, mcp) port pair plus a coordination file.

    Fresh per invocation, not the named session ports: nested/overlapping uses must
    not land on the same server. The ports are released on exit.
    """
    coordinator = PortCoordinator()
    ports = None

    try:
        ports = {
            'websocket': allocate_port(),
            'mcp': allocate_port(),
        }

        coordination_file = coordinator.create_coordination_file(ports)

        yield ports, coordination_file

    finally:
        if ports:
            coordinator.release_ports(ports)
        coordinator.cleanup()


@contextmanager
def dead_port():
    """Yield a port nothing listens on, claimed until the block exits.

    For a test that needs a refused connection. Binding port 0 and closing the socket
    hands out an ephemeral port that any process may bind a moment later, a parallel
    worker's fixture server included, and then the "dead" port answers. A claimed pool
    port is skipped by every session that allocates here, and on Windows the OS never
    assigns one on its own because the pool sits below the dynamic range. Linux's
    default ephemeral range (32768-60999) does overlap the pool.
    """
    port = allocate_port()
    try:
        yield port
    finally:
        release_port(port)


class FirefoxPortCoordinator:
    """Specialized coordinator for Firefox extension testing"""

    @staticmethod
    def read_ports(coordination_file: str) -> Dict[str, int]:
        """The ports a Firefox manager should use, straight from the coordination file."""
        ports = PortCoordinator().read_coordination_file(coordination_file)
        if not ports:
            raise ValueError(f"Could not read coordination file: {coordination_file}")
        return ports

    @staticmethod
    def create_extension_config(coordination_file: str, profile_dir: str):
        """Write the legacy browser-extension-data/config.json into a profile.

        NOTE: Firefox does NOT read this file — the extension's config comes from
        browser.storage.sync (storage-sync-v2.sqlite), which FirefoxTestManager seeds
        directly. Kept only for callers that still expect the file; the port itself
        should come from read_ports().
        """
        ports = FirefoxPortCoordinator.read_ports(coordination_file)

        storage_dir = os.path.join(profile_dir, 'browser-extension-data', 'browserplug@marcinwolny.net')
        os.makedirs(storage_dir, exist_ok=True)

        extension_config = {
            'hostname': 'localhost',
            'port': ports['websocket'],
            'retryInterval': 1000,  # Fast retry for testing
            'maxRetries': 5,        # Limited retries
            'pingTimeout': 2000     # Short timeout
        }

        config_file = os.path.join(storage_dir, 'config.json')
        with open(config_file, 'w') as f:
            json.dump(extension_config, f, indent=2)

        return ports['websocket']

    @staticmethod
    def wait_for_coordination_file(file_path: str, timeout: float = 10.0) -> Optional[Dict[str, int]]:
        """Wait for coordination file to be created"""
        start_time = time.time()
        while time.time() - start_time < timeout:
            if os.path.exists(file_path):
                ports = PortCoordinator().read_coordination_file(file_path)
                if ports:
                    return ports
            time.sleep(0.1)

        return None


if __name__ == "__main__":
    print("Testing Port Coordination System...")

    with coordinated_test_ports() as (ports, coord_file):
        print(f"OK Allocated ports: {ports}")
        print(f"OK Coordination file: {coord_file}")

        coordinator = PortCoordinator()
        read_ports = coordinator.read_coordination_file(coord_file)
        assert read_ports == ports
        print(f"OK Coordination file readable: {read_ports}")

    with coordinated_test_ports() as (ports1, _):
        with coordinated_test_ports() as (ports2, _):
            assert ports1['websocket'] != ports2['websocket']
            assert ports1['mcp'] != ports2['mcp']
            print(f"OK Concurrent allocations differ: {ports1} vs {ports2}")

    print("Port Coordination System working correctly!")
