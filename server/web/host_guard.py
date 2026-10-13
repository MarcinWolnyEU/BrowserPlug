"""Host and Origin admission for the MCP HTTP port (DNS-rebinding defence, review finding S1).

The peer gate in http_auth trusts a loopback peer with everything. A web page whose DNS
name is rebound to 127.0.0.1 IS a loopback peer, but its requests still carry the attacker's
name in `Host` (a page cannot forge that header). So the guard admits only Host values that
name this machine: localhost, the loopback IPs, this machine's own interface addresses and
hostname, plus whatever the user lists in BROWSERPLUG_ALLOWED_HOSTS (a DDNS name or a WAN
address a remote browser dials). The port never matters, and IPv6 brackets are handled.

A request with no Host header at all is admitted: HTTP/1.0 clients omit it and a browser
never does, so the rebinding vector cannot produce that shape.

`/mcp` gets one more rule: a browser sends `Origin` on cross-site POSTs, and none of the
real /mcp callers (stdio launcher, HTTP MCP clients) does, so an Origin that does not name
this machine is refused there.

Stdlib plus a lazy psutil, no import-time work; server.py injects nothing but the hook.
"""

import asyncio
import ipaddress
import logging
import os
import socket
import threading
import time
from typing import Callable, Iterable, Optional, Tuple
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

ALLOWED_HOSTS_ENV = "BROWSERPLUG_ALLOWED_HOSTS"
STATIC_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
# How long the own-address set is trusted before a lookup miss may rebuild it. A DHCP
# change or a VPN coming up must not need a restart, and a hostile Host must not make
# every request pay for an interface scan.
_REFRESH_SECONDS = 2.0


class LookupNeeded(Exception):
    """Raised by `HostGuard.check(..., may_block=False)` when answering would mean a
    network-interface or hostname lookup, which blocks (a hostname lookup can wait on DNS)."""


def split_host(value: str) -> Optional[str]:
    """`Host` / authority to a normalised name: no port, no brackets, lower case, IPs
    canonicalised (`[0:0:0:0:0:0:0:1]:80` -> `::1`). None when it is unusable."""
    v = (value or "").strip().lower()
    if not v:
        return None
    if v.startswith("["):
        end = v.find("]")
        if end < 0:
            return None
        name, rest = v[1:end], v[end + 1:]
        if rest and not (rest.startswith(":") and rest[1:].isascii() and rest[1:].isdigit()):
            return None
    else:
        name, sep, port = v.partition(":")
        if sep and not (port.isascii() and port.isdigit()):
            return None
        if ":" in port:
            return None
    name = name.rstrip(".")
    if not name:
        return None
    try:
        ip = ipaddress.ip_address(name.split("%", 1)[0])
    except ValueError:
        return name
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return str(ip)


def _is_unspecified(name: str) -> bool:
    try:
        return ipaddress.ip_address(name).is_unspecified
    except ValueError:
        return False


def own_addresses() -> set:
    """Every address on this machine's interfaces, canonical strings. Loopback included."""
    found = set()
    try:
        import psutil
        for addrs in psutil.net_if_addrs().values():
            for a in addrs:
                if a.family in (socket.AF_INET, socket.AF_INET6):
                    n = split_host(a.address)
                    if n:
                        found.add(n)
    except Exception as e:  # net_if_addrs CAN throw; the guard must still answer
        logger.debug(f"interface address lookup failed: {e}")
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            n = split_host(info[4][0])
            if n:
                found.add(n)
    except Exception as e:
        logger.debug(f"hostname address lookup failed: {e}")
    return found


def _env_hosts() -> set:
    out = set()
    for part in (os.environ.get(ALLOWED_HOSTS_ENV) or "").replace(";", ",").split(","):
        n = split_host(part)
        if n:
            out.add(n)
    return out


class HostGuard:
    """`check(scope)` returns None to admit or (status, message) to refuse; it is the
    `host_check` hook of HttpEndpoints."""

    def __init__(self, extra_hosts: Callable[[], Iterable[str]] = lambda: (),
                 addresses: Optional[Callable[[], set]] = None,
                 hostname: Callable[[], str] = socket.gethostname):
        self._extra_hosts = extra_hosts
        self._addresses = addresses or (lambda: own_addresses())
        self._hostname = hostname
        self._lock = threading.Lock()
        self._addr_cache: set = set()
        self._addr_at = float("-inf")

    def _refresh_addresses(self, force: bool, may_block: bool = True) -> set:
        # The lock is held across the lookup, so a caller that may not block must not wait
        # on it either: it hands the request to a thread (LookupNeeded) instead.
        if not self._lock.acquire(blocking=may_block):
            raise LookupNeeded()
        try:
            now = time.monotonic()
            if force and now - self._addr_at >= _REFRESH_SECONDS or self._addr_at == float("-inf"):
                if not may_block:
                    raise LookupNeeded()
                try:
                    self._addr_cache = set(self._addresses())
                except Exception as e:
                    logger.debug(f"address provider failed: {e}")
                self._addr_at = now
            return self._addr_cache
        finally:
            self._lock.release()

    def _named(self) -> set:
        names = set(STATIC_HOSTS) | _env_hosts()
        for h in self._extra_hosts():
            n = split_host(h)
            if n:
                names.add(n)
        try:
            n = split_host(self._hostname())
            if n:
                names.add(n)
        except Exception:
            pass
        return names

    def host_allowed(self, host_header: str, may_block: bool = True) -> bool:
        name = split_host(host_header)
        if name is None or _is_unspecified(name):
            return False  # 0.0.0.0 / :: reach loopback from a browser ("0.0.0.0 day")
        if name in self._named() or name in self._refresh_addresses(False, may_block):
            return True
        try:
            ipaddress.ip_address(name)
        except ValueError:
            return False
        # An IP literal we have not seen: an interface may have appeared since the cache.
        return name in self._refresh_addresses(True, may_block)

    def origin_allowed(self, origin: str, may_block: bool = True) -> bool:
        if origin.strip().lower() == "null":
            return False
        try:
            parts = urlsplit(origin)
        except ValueError:   # "http://[" and kin: an Origin no browser sends
            return False
        if parts.scheme not in ("http", "https") or not parts.netloc:
            return False
        return self.host_allowed(parts.netloc, may_block)

    async def check_async(self, scope) -> Optional[Tuple[int, str]]:
        """`check` for an event loop: the common answer comes straight from the cached
        address set, and a request that needs an interface or hostname lookup runs it on
        a worker thread, so the loop never waits on the network stack."""
        try:
            return self.check(scope, may_block=False)
        except LookupNeeded:
            return await asyncio.to_thread(self.check, scope)

    def check(self, scope, may_block: bool = True) -> Optional[Tuple[int, str]]:
        headers = {}
        for k, v in scope.get("headers") or ():
            headers.setdefault(k.decode("latin-1").lower(), v.decode("latin-1"))
        host = headers.get("host")
        if host is not None and not self.host_allowed(host, may_block):
            logger.warning(f"Refused request for Host {host[:80]!r} "
                           f"({scope.get('method')} {scope.get('path')})")
            return 403, "unexpected Host header"
        origin = headers.get("origin")
        path = scope.get("path", "")
        if origin is not None and (path == "/mcp" or path.startswith("/mcp/")):
            if not self.origin_allowed(origin, may_block):
                logger.warning(f"Refused cross-origin {scope.get('method')} {path} "
                               f"from Origin {origin[:80]!r}")
                return 403, "cross-origin requests to /mcp are refused"
        return None
