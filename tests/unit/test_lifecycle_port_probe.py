"""lifecycle.is_port_in_use must not connect to 'localhost'.

A refused loopback connect to 'localhost' costs about 2 s on Windows (::1 is tried
first and the SYN is dropped), and the restart wait paid it four times per 0.2 s tick.
The probe is a bind test (mcp_port.port_is_bindable), which is free.
"""
import os
import socket
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from server.runtime import lifecycle


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_probe_never_connects_or_resolves_localhost(monkeypatch):
    calls = []

    def boom_connect(*a, **k):
        calls.append(("create_connection", a))
        raise AssertionError("is_port_in_use must not connect")

    real_gai = socket.getaddrinfo

    def spy_gai(host, *a, **k):
        calls.append(("getaddrinfo", host))
        return real_gai(host, *a, **k)

    monkeypatch.setattr(socket, "create_connection", boom_connect)
    monkeypatch.setattr(socket, "getaddrinfo", spy_gai)
    assert lifecycle.is_port_in_use(_free_port()) is False
    assert not [c for c in calls if c[0] == "create_connection"]
    assert not [c for c in calls if c[0] == "getaddrinfo" and c[1] == "localhost"]


def test_free_port_probe_is_fast():
    port = _free_port()
    t = time.monotonic()
    for _ in range(4):
        assert lifecycle.is_port_in_use(port) is False
    assert time.monotonic() - t < 0.5


def test_probe_sees_a_loopback_listener():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        assert lifecycle.is_port_in_use(s.getsockname()[1]) is True


def test_probe_sees_a_wildcard_listener():
    """LAN mode binds 0.0.0.0; a loopback-only bind probe would call that port free."""
    with socket.socket() as s:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        s.bind(("0.0.0.0", 0))
        s.listen()
        assert lifecycle.is_port_in_use(s.getsockname()[1]) is True
