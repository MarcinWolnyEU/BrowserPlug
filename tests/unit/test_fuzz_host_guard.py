"""Property-based fuzzing of the Host and Origin admission code (DNS-rebinding defence).

Both headers come straight off the network. The guard must answer every value without
raising, admit the names that mean this machine, and refuse the wildcard addresses and
every foreign name. The guard runs with a stubbed address list and hostname, so no
property touches the network stack.
"""

import ipaddress
import os
from unittest import mock

from hypothesis import assume, given, strategies as st

from fuzz_support import fuzz
from server.web import host_guard, mcp_host_origin

OWN_HOSTNAME = "fuzzbox"
OWN_IP = "192.168.77.5"


def make_guard():
    return host_guard.HostGuard(addresses=lambda: {OWN_IP}, hostname=lambda: OWN_HOSTNAME)


def isolated_env():
    """No allow-list from the developer's shell."""
    return mock.patch.dict(os.environ, {host_guard.ALLOWED_HOSTS_ENV: ""})


ports = st.integers(0, 65535)
ips = st.one_of(st.ip_addresses(v=4), st.ip_addresses(v=6))
_labels = st.from_regex(r"[a-z][a-z0-9-]{0,12}", fullmatch=True)
foreign_names = st.builds(lambda a, b: f"{a}.{b}.example", _labels, _labels)


def authority(ip, port=None, bracket=True):
    text = f"[{ip}]" if ip.version == 6 and bracket else str(ip)
    return text + (f":{port}" if port is not None else "")


@fuzz()
@given(st.text(max_size=200))
def test_split_host_never_raises(value):
    name = host_guard.split_host(value)
    assert name is None or (isinstance(name, str) and name != "" and name == name.lower())


@fuzz()
@given(ips, st.none() | ports)
def test_split_host_canonicalises_an_address_with_or_without_port_and_brackets(ip, port):
    expected = str(ip.ipv4_mapped if ip.version == 6 and ip.ipv4_mapped else ip)
    assert host_guard.split_host(authority(ip, port)) == expected
    assert host_guard.split_host(authority(ip, port).upper()) == expected


@fuzz()
@given(st.text(max_size=200))
def test_host_allowed_and_origin_allowed_never_raise(value):
    with isolated_env():
        guard = make_guard()
        assert guard.host_allowed(value) in (True, False)
        assert guard.origin_allowed(value) in (True, False)
        assert mcp_host_origin._normalize_origin(value) is not None


@fuzz()
@given(foreign_names, st.none() | ports)
def test_a_foreign_name_is_refused(name, port):
    with isolated_env():
        guard = make_guard()
        host = name + (f":{port}" if port is not None else "")
        assert guard.host_allowed(host) is False
        assert guard.origin_allowed(f"http://{host}") is False
        assert guard.origin_allowed(f"https://{host}/x") is False


@fuzz()
@given(st.sampled_from(["0.0.0.0", "::"]), st.none() | ports, st.sampled_from(["", "http://", "https://"]))
def test_the_wildcard_address_is_never_a_name_for_this_machine(address, port, scheme):
    with isolated_env():
        guard = make_guard()
        host = authority(ipaddress.ip_address(address), port)
        assert guard.host_allowed(host) is False
        if scheme:
            assert guard.origin_allowed(scheme + host) is False


@fuzz()
@given(st.sampled_from(["localhost", "127.0.0.1", "[::1]", "LOCALHOST", OWN_HOSTNAME, OWN_IP]),
       st.none() | ports, st.sampled_from(["http", "https"]))
def test_this_machines_own_names_are_admitted_on_any_port(name, port, scheme):
    with isolated_env():
        guard = make_guard()
        host = name + (f":{port}" if port is not None else "")
        assert guard.host_allowed(host) is True
        assert guard.origin_allowed(f"{scheme}://{host}") is True


@fuzz()
@given(st.sampled_from(["null", "NULL", " null "]) | st.text(alphabet="abc:/.[]@ ", max_size=40))
def test_an_origin_without_an_http_scheme_and_host_is_refused(origin):
    assume(not origin.strip().lower().startswith(("http://", "https://")))
    with isolated_env():
        assert make_guard().origin_allowed(origin) is False


header_bytes = st.binary(max_size=80)
header_names = st.sampled_from([b"host", b"origin", b"Host", b"x-other"]) | st.binary(max_size=10)


@fuzz()
@given(st.lists(st.tuples(header_names, header_bytes), max_size=5),
       st.sampled_from(["/mcp", "/mcp/", "/health", "/", ""]))
def test_check_answers_every_header_set_with_none_or_a_403(headers, path):
    with isolated_env():
        verdict = make_guard().check({"headers": headers, "path": path, "method": "POST"})
    assert verdict is None or (verdict[0] == 403 and isinstance(verdict[1], str))
    # The wrapper's own refusal sits behind this one and must answer just as safely.
    refusal = mcp_host_origin.refusal({"headers": headers, "path": path, "method": "POST"})
    assert refusal is None or refusal[0] in (400, 403, 421)


@fuzz()
@given(foreign_names, st.sampled_from(["/mcp", "/health", "/ws-token", "/"]))
def test_a_foreign_host_header_is_refused_on_every_path(name, path):
    scope = {"headers": [(b"host", name.encode())], "path": path, "method": "GET"}
    with isolated_env():
        assert make_guard().check(scope) == (403, "unexpected Host header")


def test_an_origin_urlsplit_cannot_parse_is_refused_not_raised():
    """Found by the fuzz run above: `urlsplit` raises ValueError on an unbalanced IPv6
    bracket, and `origin_allowed` let it escape from `check` on a /mcp request."""
    with isolated_env():
        guard = make_guard()
        for origin in ("//[", "http://[", "http://[::1", "https://[]", "http://[::1]x"):
            assert guard.origin_allowed(origin) is False
        scope = {"headers": [(b"origin", b"http://[")], "path": "/mcp", "method": "POST"}
        assert guard.check(scope) == (403, "cross-origin requests to /mcp are refused")
