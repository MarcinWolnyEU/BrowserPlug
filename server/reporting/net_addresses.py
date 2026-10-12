"""This machine's network addresses, for the reports and the WAN diagnosis.

Stdlib plus a lazy psutil, no state, no import-time work. Callers inject these into the
things that need them (`Reports` takes them as sources, `wan_diagnosis` as an argument),
so nothing has to import server.py to reach them.
"""

import logging

logger = logging.getLogger(__name__)


def lan_addresses():
    """This machine's non-loopback IPv4s: the addresses a browser on ANOTHER machine
    would dial. Reported on /health so the options page can print the exact ws:// URL
    to paste into a remote browser instead of asking the user to go find it."""
    addrs = set()
    try:
        import socket
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127."):
                addrs.add(ip)
    except Exception as e:
        logger.debug(f"lan address lookup failed: {e}")
    return sorted(addrs)


def ws_lan_addresses():
    """Adapter-named IPv4s for the options page's LAN panel: which of this machine's
    addresses belongs to which interface, so the user can tell their real LAN NIC from
    the WSL/Hyper-V/Docker virtual ones. `recommended` marks the home-router range
    (192.168.*) the page highlights; recommended rows sort first.

    Superset of lan_addresses() (which stays for /health back-compat): getaddrinfo can
    miss interfaces the resolver doesn't know, psutil asks the OS for the real table."""
    rows = []
    try:
        import psutil
        import socket
        for interface, addrs in psutil.net_if_addrs().items():
            for addr in addrs:
                if addr.family != socket.AF_INET:
                    continue
                ip = addr.address
                if ip.startswith("127.") or ip.startswith("169.254."):
                    continue  # loopback / link-local; never a usable LAN address
                rows.append({"ip": ip, "interface": interface,
                             "recommended": ip.startswith("192.168.")})
    except Exception as e:  # net_if_addrs CAN throw; /health must not
        logger.debug(f"adapter address lookup failed: {e}")
    rows.sort(key=lambda r: (not r["recommended"], r["ip"]))
    return rows
