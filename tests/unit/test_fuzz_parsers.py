"""Property-based fuzzing of parsers that read text an outsider controls.

Two groups. `proc_facts` reads /proc files and so must survive whatever is in them.
`url_diff` and `url_norm` compare the URL an agent typed with the URL the browser reports
back, so they must answer for any string, equal URLs must read as equal, and the
normalisers must be idempotent (normalising a normal form changes nothing).
"""

import ipaddress
import os
import sys

from hypothesis import given, strategies as st

from browserplug_shared import proc_facts
from fuzz_support import fuzz
from server.tools import url_diff, url_norm


# ---- proc_facts ------------------------------------------------------------------------

def _hex_address(ip):
    """How /proc/net/tcp prints an address: each 32-bit word in the host's byte order."""
    raw = ip.packed
    words = [raw[i:i + 4] for i in range(0, len(raw), 4)]
    return b"".join(w[::-1] if sys.byteorder == "little" else w for w in words).hex().upper()


def _row(ip, port, state, inode):
    return (f"   0: {_hex_address(ip)}:{port:04X} 00000000:0000 {state} 00000000:00000000 "
            f"00:00000000 00000000  1000        0 {inode} 1 0000000000000000 100 0 0 10 0")


def _expected_address(ip):
    return f"::ffff:{ip.ipv4_mapped}" if ip.version == 6 and ip.ipv4_mapped else str(ip)


_HEADER = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode"
ips = st.one_of(st.ip_addresses(v=4), st.ip_addresses(v=6))


@fuzz()
@given(st.text(max_size=600))
def test_parse_proc_net_tcp_never_raises_on_noise(text):
    for _address, port, inode in proc_facts.parse_proc_net_tcp(text):
        assert 0 <= port <= 0xFFFF and isinstance(inode, int)


@fuzz()
@given(st.lists(st.tuples(ips, st.integers(0, 65535), st.sampled_from(["0A", "01", "06", "0B", "FF"]),
                          st.integers(0, 2**40)), max_size=8))
def test_parse_proc_net_tcp_round_trips_and_keeps_only_listeners(rows):
    text = "\n".join([_HEADER] + [_row(*r) for r in rows])
    expected = [(_expected_address(ip), port, inode) for ip, port, state, inode in rows if state == "0A"]
    assert proc_facts.parse_proc_net_tcp(text) == expected


@fuzz()
@given(st.lists(st.text(max_size=20), max_size=6), st.text(alphabet="0123456789ABCDEFxyz:. ", max_size=30))
def test_parse_proc_net_tcp_skips_a_malformed_row_between_good_ones(junk, bad_address):
    good = _row(ipaddress.ip_address("127.0.0.1"), 2830, "0A", 4242)
    broken = f"   1: {bad_address} 00000000:0000 0A 0:0 0:0 0 1000 0 7 1 0"
    rows = proc_facts.parse_proc_net_tcp("\n".join([_HEADER, good, broken, *junk, good]))
    assert rows.count(("127.0.0.1", 2830, 4242)) == 2


@fuzz()
@given(st.lists(st.text(alphabet=st.characters(blacklist_characters="\0", blacklist_categories=("Cs",)),
                        max_size=30), min_size=1, max_size=6))
def test_parse_cmdline_round_trips_a_nul_separated_argv(argv):
    raw = b"\0".join(os.fsencode(a) for a in argv) + b"\0"
    assert proc_facts.parse_cmdline(raw) == argv
    if argv[-1] != "":
        assert proc_facts.parse_cmdline(raw[:-1]) == argv


@fuzz()
@given(st.binary(max_size=200))
def test_parse_cmdline_accepts_any_bytes(raw):
    try:
        out = proc_facts.parse_cmdline(raw)
    except UnicodeDecodeError:
        # Only Windows's strict fsdecode; this parser serves /proc, where it uses surrogateescape.
        assert os.name == "nt"
        return
    assert isinstance(out, list) and all(isinstance(a, str) for a in out)


@fuzz()
@given(st.text(max_size=300))
def test_parse_status_uid_returns_an_int_or_raises_oserror(text):
    try:
        uid = proc_facts.parse_status_uid(text)
    except OSError:
        return
    assert isinstance(uid, int) and uid >= 0


_filler = st.text(alphabet=st.characters(blacklist_categories=("Cs", "Cc", "Zl", "Zp")), max_size=30).filter(
    lambda line: not line.startswith("Uid:"))


@fuzz()
@given(st.lists(_filler, max_size=5), st.integers(0, 2**32), st.integers(0, 2**32), st.integers(0, 5))
def test_parse_status_uid_reads_the_first_real_uid(filler, uid, effective, at):
    lines = filler[:at] + [f"Uid:\t{uid}\t{effective}\t{effective}\t{effective}"] + filler[at:]
    assert proc_facts.parse_status_uid("\n".join(lines)) == uid


@fuzz()
@given(st.lists(st.tuples(st.text(max_size=30) | ips.map(str), st.integers(0, 5)), max_size=10))
def test_reached_owners_is_total_and_a_subset(entries):
    owners = proc_facts.reached_owners(entries)
    assert owners <= {owner for _, owner in entries}


@fuzz()
@given(st.lists(st.tuples(ips.filter(lambda ip: ip not in (ipaddress.ip_address("127.0.0.1"),
                                                            ipaddress.ip_address("::ffff:127.0.0.1"))).map(str),
                          st.integers(0, 5)), max_size=10),
       st.sets(st.integers(10, 15), min_size=1))
def test_a_listener_on_127_0_0_1_wins_over_every_wildcard(others, loopback_owners):
    entries = others + [("127.0.0.1", o) for o in loopback_owners] + [("::ffff:127.0.0.1", 99)]
    assert proc_facts.reached_owners(entries) == loopback_owners | {99}


# ---- url_norm and url_diff -------------------------------------------------------------

any_url = st.one_of(st.none(), st.text(max_size=120))
_schemes = st.sampled_from(["http", "https", "ftp"])
_host = st.from_regex(r"(www\.)?[a-z][a-z0-9-]{0,10}\.(com|org|co\.uk|pl)(:[0-9]{2,4})?", fullmatch=True)
_pathish = st.from_regex(r"(/[A-Za-z0-9._~%-]{0,8}){0,4}/?", fullmatch=True)
_query = st.from_regex(r"([a-z]{1,5}=[A-Za-z0-9%+]{0,6}(&[a-z]{1,5}=[A-Za-z0-9%+]{0,6}){0,3})?", fullmatch=True)
_fragment = st.from_regex(r"(/?[A-Za-z0-9/]{0,8})?", fullmatch=True)
urls = st.builds(lambda s, h, p, q, f: f"{s}://{h}{p}" + (f"?{q}" if q else "") + (f"#{f}" if f else ""),
                 _schemes, _host, _pathish, _query, _fragment)


@fuzz()
@given(st.text(max_size=80))
def test_ascii_host_is_total(host):
    assert isinstance(url_norm.ascii_host(host), str)


@fuzz()
@given(st.text(max_size=80))
def test_ascii_host_is_idempotent(host):
    once = url_norm.ascii_host(host)
    assert url_norm.ascii_host(once) == once


_segments = st.sampled_from(["a", "b", "..", ".", "%2e%2e", "%2E", "%2e.", "", "c d"])


@fuzz()
@given(st.lists(_segments, max_size=10))
def test_resolve_dot_segments_removes_dots_and_is_idempotent(segments):
    path = "/" + "/".join(segments)
    out = url_norm.resolve_dot_segments(path)
    assert out.startswith("/")
    assert not any(seg.lower() in (".", "..", "%2e", "%2e.", ".%2e", "%2e%2e") for seg in out.split("/"))
    assert url_norm.resolve_dot_segments(out) == out


@fuzz()
@given(st.text(max_size=100))
def test_normalize_path_is_total_absolute_and_idempotent(tail):
    once = url_diff.normalize_path("/" + tail)    # a URL's path is empty or starts with "/"
    assert once.startswith("/")
    # It drops ONE trailing slash by design, so `///` needs two passes to reach `/`; any
    # other normal form is a fixed point.
    again = url_diff.normalize_path(once)
    assert again == once if (not once.endswith("/") or once == "/") else again.rstrip("/") == once.rstrip("/")


@fuzz()
@given(any_url, any_url)
def test_diff_urls_and_reports_are_total(requested, landed):
    diff = url_diff.diff_urls(requested, landed)
    assert isinstance(diff.significant(), bool)
    assert isinstance(url_diff.landing_report(requested, landed), str)
    wall = url_diff.landing_wall(requested, landed)
    assert wall is None or isinstance(wall, str)
    assert isinstance(url_diff.state_fragment_changed(requested, landed), bool)


@fuzz()
@given(any_url, st.none() | st.text(max_size=40), st.none() | st.integers(0, 999))
def test_interstitial_evidence_is_total(url, title, status):
    found = url_diff.interstitial_evidence(url, title, status)
    assert found is None or found[1] in ("url", "title", "status")


@fuzz()
@given(urls)
def test_a_url_compared_with_itself_differs_in_nothing(url):
    diff = url_diff.diff_urls(url, url)
    assert not diff.significant()
    assert url_diff.landing_report(url, url) == ""
    assert url_diff.landing_wall(url, url) is None


@fuzz()
@given(urls, urls)
def test_host_and_path_verdicts_are_symmetric(a, b):
    ab, ba = url_diff.diff_urls(a, b), url_diff.diff_urls(b, a)
    assert ab.host_changed == ba.host_changed
    assert ab.path_changed == ba.path_changed
    assert ab.site_changed == ba.site_changed


@fuzz()
@given(_schemes, _host, st.from_regex(r"[A-Za-z0-9._~-]{0,8}(/[A-Za-z0-9._~-]{1,8}){0,3}", fullmatch=True),
       st.sampled_from(["www.", "WWW.", ""]), st.booleans())
def test_www_case_and_a_trailing_slash_are_not_a_different_page(scheme, host, tail, prefix, slash):
    host = host.removeprefix("www.")
    url = f"{scheme}://{host}/{tail}"
    other = f"{scheme.upper()}://{prefix}{host.upper()}/{tail}" + ("/" if slash and tail else "")
    diff = url_diff.diff_urls(url, other)
    assert not diff.host_changed and not diff.path_changed


def test_an_unbalanced_ipv6_bracket_is_compared_not_raised():
    """Found by the fuzz run above: `urlsplit` raises ValueError on `[`, and diff_urls let
    it escape into navigate's landing check."""
    for bad in ("[", "http://[", "http://[::1", "example.org/[x"):
        assert isinstance(url_diff.diff_urls(bad, "https://example.org/").significant(), bool)
        assert isinstance(url_diff.diff_urls("https://example.org/", bad).significant(), bool)
        assert url_diff.landing_wall(bad, bad) is None
        assert url_diff.interstitial_evidence(bad) is None
