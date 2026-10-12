"""Is the loopback peer a process of ANOTHER OS user?

`http_auth` trusts a loopback peer with everything, but 127.0.0.1 is reachable by every
account on the machine (RDP sessions, services under other users) and by any tunnel or
port relay that ends here. The token file's ACL keeps other users out of the file, not out
of `/ws-token`. So the gate also asks who owns the process on the far end of the socket:
the peer port maps to a pid (`client_identity.pid_for_local_port`), the pid to an account
name, and a name that differs from the server's own is refused.

Fail OPEN, on purpose, whenever the owner cannot be resolved: no pid for the port (the
socket closed already, or psutil could not see it), no psutil, a process that vanished, or
an owner name psutil is not allowed to read. Refusing there would lock out the launcher of
the user who owns the server over a race, which is the failure this gate must never cause.
The cost is that an attacker whose process is unreadable to us is not stopped by this
check alone; the Host/Origin guard and the endpoint token checks still apply. The first
unresolved peer is logged at DEBUG, later ones stay silent.

The lookup walks the OS connection table and can take tens of milliseconds, so the async
entry point runs it in a thread and caches the verdict per peer port for a few seconds.
A keep-alive client reuses one port, so the table is read once per connection burst.

A port can change hands inside that window: a short-lived client exits and another
account's process is given the same ephemeral port. So a cached verdict also remembers the
process it was made for, as (pid, create_time), and is honoured only while that process is
still alive with the same start time. The check is one `psutil.Process(pid).create_time()`
call, microseconds against the table walk. It does not cover a long-lived process that
closes the socket and hands the port to a foreign one inside the window; the window is
short and the endpoint token checks still apply.
"""

import asyncio
import logging
import time

from server.reporting import client_identity

logger = logging.getLogger(__name__)

CACHE_SECONDS = 5.0
# A peer whose owner could not be resolved has no process to re-check. It is usually a
# socket that closed already, so the verdict is kept only briefly.
UNRESOLVED_CACHE_SECONDS = 1.0
CACHE_MAX = 512

_server_owner = None


def _norm(name):
    return (name or "").strip().casefold()


def server_owner():
    """This process's account name, read once. None when it cannot be read."""
    global _server_owner
    if _server_owner is None:
        try:
            import psutil
            _server_owner = _norm(psutil.Process().username())
        except Exception as e:
            logger.debug(f"server owner unreadable: {e}")
            return None
    return _server_owner or None


def peer_owner(port):
    """Account name owning the process behind loopback port `port`, or None."""
    pid = client_identity.pid_for_local_port(port)
    if not pid:
        return None
    try:
        import psutil
        return _norm(psutil.Process(pid).username()) or None
    except Exception as e:
        logger.debug(f"owner of pid {pid} (peer port {port}) unreadable: {e}")
        return None


def _identity(pid):
    """(pid, create_time) of a live process, or None when it cannot be read."""
    try:
        import psutil
        return (pid, psutil.Process(pid).create_time())
    except Exception as e:
        logger.debug(f"identity of pid {pid} unreadable: {e}")
        return None


def resolve_peer(port):
    """(foreign, identity): the verdict for `port` and the process it was made for.

    `identity` is (pid, create_time), or None when the owner could not be resolved (the
    verdict is then False, fail open).
    """
    if not isinstance(port, int) or not port:
        return False, None
    mine = server_owner()
    pid = client_identity.pid_for_local_port(port)
    if mine is None or not pid:
        return False, None
    try:
        import psutil
        theirs = _norm(psutil.Process(pid).username()) or None
    except Exception as e:
        logger.debug(f"owner of pid {pid} (peer port {port}) unreadable: {e}")
        return False, None
    if theirs is None:
        return False, None
    return mine != theirs, _identity(pid)


def same_process(identity):
    """Is the process a cached verdict was made for still the one running under its pid?"""
    return identity is not None and _identity(identity[0]) == tuple(identity)


class PeerOwnerGate:
    """Cached, off-loop `resolve_peer`. One per HttpEndpoints."""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._cache = {}
        self._logged_unresolved = False

    async def is_foreign(self, port):
        now = self._clock()
        hit = self._cache.get(port)
        if hit and hit[1] > now and (hit[2] is None or same_process(hit[2])):
            return hit[0]
        identity = None
        try:
            verdict, identity = await asyncio.to_thread(resolve_peer, port)
        except Exception as e:
            verdict = False
            if not self._logged_unresolved:
                self._logged_unresolved = True
                logger.debug(f"peer owner check failed open for port {port}: {e}")
        if len(self._cache) >= CACHE_MAX:
            self._cache = {k: v for k, v in self._cache.items() if v[1] > now}
            if len(self._cache) >= CACHE_MAX:
                self._cache.clear()
        ttl = CACHE_SECONDS if identity is not None else UNRESOLVED_CACHE_SECONDS
        self._cache[port] = (verdict, now + ttl, identity)
        return verdict
