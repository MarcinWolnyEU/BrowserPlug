#!/usr/bin/env python3
"""Port overlap checks for the BSD socket stack (macOS).

On Linux and Windows an `SO_REUSEADDR` bind cannot sit beside a live listener on an
overlapping address (a wildcard against a specific address, `127.0.0.1` against
`0.0.0.0`). On BSD it can: only an identical address is refused. Measured on macOS 27:

  * listener on 0.0.0.0:P, new socket on 127.0.0.1:P with SO_REUSEADDR: bound, listening.
  * listener on 127.0.0.1:P, new socket on 0.0.0.0:P with SO_REUSEADDR: bound, listening.
  * the same address twice: EADDRINUSE with or without the option.
  * without the option, any overlap, a TIME_WAIT socket included, is EADDRINUSE.

asyncio and uvicorn both set SO_REUSEADDR on POSIX, so a BrowserPlug server can start
beside a different-spec listener on its port, and a probe that sets the option calls
that port free. Two binds tell the cases apart:

  strict bind (no option) succeeds        -> nothing on the port, TIME_WAIT included
  strict fails, reuse bind fails           -> an identical address is held
  strict fails, reuse bind succeeds        -> a TIME_WAIT socket, or an overlapping listener

The last case needs a connect to the loopback side: a live listener, wildcard or
loopback, accepts, and a TIME_WAIT socket refuses. A timeout counts as a listener, the
fail-safe direction. A listener on a LAN address alone does not answer on loopback and is
read as stale; nothing unprivileged can tell it apart (the system-wide socket table needs
root on macOS).

Stdlib-only (the launcher imports mcp_port, which imports this).

Copyright 2026 Marcin Wolny
Licensed under the MIT License - see LICENSE file for details
"""
import errno
import socket
import sys

FREE = "free"
OVERLAP = "overlap"  # a live listener on an overlapping address
STALE = "stale"      # overlap in the table, but nothing answers: TIME_WAIT

# How long the connect to a suspected listener may take. Loopback answers in microseconds;
# only a listener with a full backlog runs this out, and that is a listener.
CONNECT_PROBE_SECONDS = 0.25

# Where a connect reaches a wildcard listener.
_LOOPBACK_OF = {"0.0.0.0": "127.0.0.1", "": "127.0.0.1", "::": "::1"}
_IPV4_WILDCARD = "0.0.0.0"
_IPV6_WILDCARD = "::"


def applies():
    """True where SO_REUSEADDR lets a bind overlap a live listener (macOS)."""
    return sys.platform == "darwin"


def _bind(family, addr, port, reuse):
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        if reuse:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((addr, port))


def _answers(family, addr, port):
    """True when a connect to `addr`'s loopback side is accepted or hangs."""
    target = _LOOPBACK_OF.get(addr, addr)
    try:
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            sock.settimeout(CONNECT_PROBE_SECONDS)
            sock.connect((target, port))
    except TimeoutError:
        return True
    except OSError:
        return False
    return True


def classify(family, addr, port):
    """FREE, OVERLAP or STALE for binding `addr`:`port`; OSError if it cannot be bound.

    The OSError is the real bind's own answer: EADDRINUSE for an identical address (whatever
    holds it, listening or not), and whatever else the strict bind raised (EACCES, no
    address family, address not on this machine).
    """
    try:
        _bind(family, addr, port, reuse=False)
        return FREE
    except OSError as e:
        if e.errno != errno.EADDRINUSE:
            raise
    _bind(family, addr, port, reuse=True)  # EADDRINUSE here: an identical address is held
    return OVERLAP if _answers(family, addr, port) else STALE


def with_loopback_siblings(addresses):
    """`addresses` (one host or a list) plus the loopback addresses of any wildcard in it.

    A wildcard listener does not stop a later SO_REUSEADDR bind on 127.0.0.1 or ::1, and
    that more specific socket then receives every loopback connection. Holding the loopback
    addresses ourselves leaves a squatter an identical address, which BSD refuses.
    """
    hosts = [addresses] if isinstance(addresses, str) else list(addresses)
    wanted = []
    if _IPV4_WILDCARD in hosts:
        wanted.append("127.0.0.1")
    if _IPV6_WILDCARD in hosts:
        wanted.append("::1")
    wanted = [h for h in wanted if h not in hosts]
    return hosts + wanted if wanted else addresses
