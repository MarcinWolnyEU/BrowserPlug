"""Which hosts the extension WebSocket may be told to bind, and whether one can be.

Stdlib only, no state. `POST /ws-bind` runs `validate_ws_host` before anything is written
to config.json; `server.py` runs `host_is_bindable` on a host read back from that file at
start-up. Both exist because a persisted host that cannot be bound fails every later start
until someone hand-edits the file.
"""

import ipaddress
import logging
import socket

from server.ws.ws_auth import LOCALHOST_BIND_HOSTS

logger = logging.getLogger(__name__)

# The documented tokens: the two localhost names and the two all-interfaces wildcards.
# Everything else must be a literal address that an interface on this machine owns.
WILDCARD_HOSTS = ("0.0.0.0", "::")


def validate_ws_host(host, assigned_ips):
    """None when `host` may be persisted as the bind address, else a message for a 400.

    `assigned_ips` are the addresses of this machine's interfaces (the `ip` values of
    `net_addresses.ws_lan_addresses()`). A hostname other than localhost is refused: it
    resolves differently per start, and the panels only ever offer addresses.
    """
    if not isinstance(host, str) or not host or host != host.strip():
        return "host must be a non-empty address without surrounding whitespace"
    if host.lower() in LOCALHOST_BIND_HOSTS or host in WILDCARD_HOSTS:
        return None
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return (f"host {host!r} is not 'localhost', '0.0.0.0', '::' or an IP address "
                f"assigned to one of this machine's network interfaces")
    if address.is_loopback or str(address) in set(assigned_ips or ()):
        return None
    return (f"host {host!r} is not assigned to any network interface on this machine, so "
            f"the server could not bind it")


def host_is_bindable(host):
    """True if a listening socket can be bound to `host` (port 0, closed at once).

    Names resolve the way the server's own bind does, so 'localhost' passes when either
    family binds. Never raises.
    """
    if not isinstance(host, str) or not host.strip() or host != host.strip():
        return False
    try:
        infos = socket.getaddrinfo(host, 0, type=socket.SOCK_STREAM,
                                   flags=socket.AI_PASSIVE)
    except (OSError, UnicodeError):
        return False
    for family, kind, proto, _canon, sockaddr in infos:
        try:
            with socket.socket(family, kind, proto) as probe:
                probe.bind(sockaddr)
            return True
        except OSError:
            continue
    return False
