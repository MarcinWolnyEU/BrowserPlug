"""The machine's TCP connections with owning pids, for the server's process lookups.

Five server call sites map a port to the process behind it (a client's source port to its
pid, the WebSocket port to the browser, a listening port to its owner). They all ask this
module, which answers with psutil's table wherever psutil can read it, and on macOS, where
psutil's system-wide call raises AccessDenied for a non-root user, with the table of the
user's own processes read through libproc (darwin_procs.py).

The rows are psutil's `sconn` where psutil answered, and `darwin_procs.Conn` otherwise. The
attributes callers use are the same in both: `laddr` and `raddr` (an (ip, port) pair, `()`
when unconnected), `status` (psutil's spelling; `CONN_LISTEN` and `CONN_ESTABLISHED` below
are the two that matter) and `pid`.

On macOS the table holds sockets of this user's processes only, so a socket of another
account is not in it. The call sites that decide something from a missing row (is the
connecting process another user's) have to treat "not found" as unknown, never as a
verdict. `sees_other_users()` says whether a missing row can mean that at all.

A full walk of the macOS table opens every process of the user, so callers run it on a
worker thread. `only_pid` limits the macOS walk to one process, for a caller that wants its
own sockets; psutil's table is always system-wide, so such a caller still filters by pid.

Stdlib-only apart from the lazy psutil import (tests/unit/test_import_boundaries.py).
"""

import os
import sys

from browserplug_shared import proc_facts
from browserplug_shared.darwin_procs import CONN_ESTABLISHED, CONN_LISTEN  # noqa: F401


def sees_other_users():
    """Can the table `tcp_connections` returns hold another account's sockets?

    False on macOS for a non-root user: psutil's system-wide call is refused there and the
    native fallback walks this user's processes only. A caller that wants to know whether a
    peer belongs to someone else can never get a "yes" from that table, so it can skip the
    walk. Root on macOS, and every other platform, read the whole machine. Cheap: no table
    is read."""
    return sys.platform != "darwin" or os.geteuid() == 0


def tcp_connections(only_pid=None):
    """Every TCP connection the caller can see, as psutil-shaped rows.

    psutil is asked first, so root, Windows and Linux are answered exactly as before. When
    psutil is missing or denied, macOS falls back to the native table; elsewhere the
    original ImportError or AccessDenied is raised, which every caller already handles."""
    refusal = None
    try:
        import psutil
    except ImportError as exc:
        refusal = exc
    else:
        try:
            return psutil.net_connections(kind="inet")
        except psutil.AccessDenied as exc:
            refusal = exc
    probe = proc_facts.darwin_probe()
    if probe is None:
        raise refusal
    return probe.connections([only_pid] if only_pid else None)
