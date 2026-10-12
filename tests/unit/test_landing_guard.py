"""T5/T6 regression table: the landed-elsewhere guard and url_diff's false alarms.

Written red-first against the pre-fix code (see .tmp/server-launcher-review-2026-09-29.md,
findings T5 and T6). Two families:

  * QUIET: benign differences a browser or site introduces must not raise the loud
    LANDED ELSEWHERE header (url_diff) and must not block a fused read (fused_read).
  * LOUD: same-host login walls, hash-route redirects and a page that moved between the
    settle and the read must block the read and be named by url_diff.
"""

import pytest

import test_imports  # noqa: F401  (path setup)
from server.tools.fused_read import (host_of, read_block_reason, read_result_moved,
                                     same_site)
from server.tools.url_diff import landing_report
from server.tools import url_norm

LOUD = "LANDED ELSEWHERE"


# ---- T6: url_diff false alarms ---------------------------------------------------------

@pytest.mark.parametrize("name,requested,landed", [
    ("site drops utm_source", "https://shop.test/p/1?utm_source=news&id=7",
     "https://shop.test/p/1?id=7"),
    ("site drops gclid and fbclid", "https://shop.test/p/1?id=7&gclid=abc&fbclid=x",
     "https://shop.test/p/1?id=7"),
    ("site rewrites a tracking value", "https://shop.test/p/1?utm_medium=a",
     "https://shop.test/p/1?utm_medium=b"),
    ("site adds page=1", "https://shop.test/search?q=gpu",
     "https://shop.test/search?q=gpu&page=1"),
    ("sharp s in UTS-46 form", "https://faß.de/", "https://xn--fa-hia.de/"),
    ("sharp s, punycode requested", "https://xn--fa-hia.de/", "https://faß.de/"),
    ("dot segments resolved", "https://example.org/a/../b", "https://example.org/b"),
    ("single dot segments", "https://example.org/a/./b", "https://example.org/a/b"),
    ("encoded dot segments", "https://example.org/a/%2e%2e/b", "https://example.org/b"),
    ("IDN vs punycode", "https://żółć.pl/", "https://xn--kda4b0koi.pl/"),
    ("scheme-less request", "example.org/docs", "https://example.org/docs"),
])
def test_benign_change_is_not_loud(name, requested, landed):
    out = landing_report(requested, landed)
    assert LOUD not in out, name
    assert "BLOCKED" not in out and "DROPPED" not in out, name


def test_added_parameter_is_still_named_but_quietly():
    out = landing_report("https://shop.test/search?q=gpu", "https://shop.test/search?q=gpu&page=1")
    assert "- query parameter ADDED by the site: page=1" in out
    assert LOUD not in out


@pytest.mark.parametrize("name,requested,landed", [
    ("ambiguous source param dropped", "https://shop.test/p?source=web&id=1",
     "https://shop.test/p?id=1"),
    ("real filter dropped alongside tracking", "https://shop.test/l?stan=uzywane&utm_source=n",
     "https://shop.test/l"),
    ("ss is not sharp s", "https://faß.de/", "https://fass.de/"),
    ("dot segments hide a real change", "https://example.org/a/../b", "https://example.org/c"),
])
def test_real_change_stays_loud(name, requested, landed):
    assert LOUD in landing_report(requested, landed), name


def test_hash_route_change_is_loud_in_url_diff():
    out = landing_report("https://app.test/#/orders", "https://app.test/#/login")
    assert LOUD in out and "hash route" in out.lower()


def test_plain_anchor_fragment_stays_silent():
    assert landing_report("https://app.test/doc", "https://app.test/doc#intro") == ""
    assert landing_report("https://app.test/app", "https://app.test/app#/home") == ""


# ---- shared normaliser ---------------------------------------------------------------

@pytest.mark.parametrize("host,expected", [
    ("żółć.pl", "xn--kda4b0koi.pl"),
    ("XN--KDA4B0KOI.PL", "xn--kda4b0koi.pl"),
    ("faß.de", "xn--fa-hia.de"),
    ("BÜCHER.example", "xn--bcher-kva.example"),
    ("example.org.", "example.org."),
    ("[::1]", "[::1]"),
    ("127.0.0.1", "127.0.0.1"),
])
def test_ascii_host(host, expected):
    assert url_norm.ascii_host(host) == expected


def test_both_modules_use_one_normaliser():
    for url in ("https://żółć.pl/", "https://faß.de/x", "https://BÜCHER.example/"):
        assert host_of(url) == url_norm.ascii_host(url.split("/")[2].lower())


# ---- T5: fused_read guard ----------------------------------------------------------------

def test_same_site_idn_both_spellings():
    assert same_site("https://żółć.pl/", "https://xn--kda4b0koi.pl/") is True
    assert same_site("https://xn--kda4b0koi.pl/", "https://żółć.pl/") is True
    assert same_site("https://faß.de/", "https://xn--fa-hia.de/") is True
    assert same_site("https://faß.de/", "https://fass.de/") is False


def test_idn_landing_allows_the_read():
    assert read_block_reason({"url": "https://xn--kda4b0koi.pl/"}, "https://żółć.pl/") is None


@pytest.mark.parametrize("requested,landed", [
    ("https://shop.test/orders", "https://shop.test/login?next=/orders"),
    ("https://shop.test/orders", "https://shop.test/account/signin"),
    ("https://shop.test/orders", "https://shop.test/sso/auth"),
    ("https://app.test/#/orders", "https://app.test/#/login"),
    ("https://app.test/#/orders", "https://app.test/#/dashboard"),
    ("https://shop.test/orders", "https://shop.test/orders#/login"),
    ("https://shop.test/p/1", "https://shop.test/captcha/add"),
])
def test_same_host_wall_or_route_change_blocks_the_read(requested, landed):
    reason = read_block_reason({"url": landed}, requested)
    assert reason, (requested, landed)


@pytest.mark.parametrize("requested,landed", [
    ("https://shop.test/login", "https://shop.test/login?x=1"),
    ("https://shop.test/login", "https://shop.test/account/login"),
    ("https://shop.test/p/1", "https://shop.test/p/1?page=1"),
    ("https://shop.test/p/1?utm_source=a", "https://shop.test/p/1"),
    ("https://shop.test/a/../p/1", "https://shop.test/p/1"),
    ("https://shop.test/p/1", "https://shop.test/p/1-red-shoes"),
    ("https://app.test/#/orders", "https://app.test/#/orders"),
    ("https://app.test/doc", "https://app.test/doc#intro"),
    ("https://shop.test/blog/login-tips", "https://shop.test/blog/login-tips/"),
])
def test_benign_same_site_landing_allows_the_read(requested, landed):
    assert read_block_reason({"url": landed}, requested) is None, (requested, landed)


@pytest.mark.parametrize("nav_url,read_url,moved", [
    ("https://shop.test/p/1", "https://shop.test/p/1", False),
    ("https://shop.test/p/1", "https://www.shop.test/p/1/", False),
    ("https://shop.test/p/1", "https://shop.test/p/1#reviews", False),
    ("https://shop.test/p/1", "https://shop.test/login?next=/p/1", True),
    ("https://shop.test/p/1", "https://other.test/", True),
    ("https://app.test/#/orders", "https://app.test/#/login", True),
    ("https://shop.test/p/1", None, False),
    ("https://shop.test/p/1", "", False),
])
def test_read_url_is_compared_with_the_landed_url(nav_url, read_url, moved):
    reason = read_result_moved({"url": nav_url}, {"url": read_url})
    assert bool(reason) is moved, reason


@pytest.mark.parametrize("nav_url,read_url,moved", [
    # A script that changes only the query string is a move.
    ("https://shop.test/search?q=shoes", "https://shop.test/search?q=boots", True),
    ("https://shop.test/search?q=shoes&page=2", "https://shop.test/search?q=shoes", True),
    ("https://shop.test/search?q=shoes", "https://shop.test/search?q=shoes&error=expired", True),
    ("https://shop.test/list", "https://shop.test/list?filter=sale", True),
    # State carried in a fragment is compared; a plain anchor is a scroll position.
    ("https://app.test/view#tab=billing", "https://app.test/view#tab=profile", True),
    ("https://app.test/view#tab=billing", "https://app.test/view", True),
    ("https://app.test/view", "https://app.test/view#reviews", False),
    ("https://app.test/view#intro", "https://app.test/view#reviews", False),
    # Tracking parameters arriving, leaving or changing say nothing about the page.
    ("https://shop.test/p/1", "https://shop.test/p/1?utm_source=news&gclid=abc", False),
    ("https://shop.test/p/1?utm_campaign=a&fbclid=1", "https://shop.test/p/1", False),
    ("https://shop.test/p/1?utm_campaign=a", "https://shop.test/p/1?utm_campaign=b", False),
    # Order and encoding of the same parameters are not moves.
    ("https://shop.test/s?a=1&b=2", "https://shop.test/s?b=2&a=1", False),
    ("https://shop.test/s?q=red%20shoe", "https://shop.test/s?q=red+shoe", False),
])
def test_query_and_state_fragment_changes_after_settle_count_as_moves(nav_url, read_url, moved):
    reason = read_result_moved({"url": nav_url}, {"url": read_url})
    assert bool(reason) is moved, reason


# ---- wiring: navigate(read=) honours both guards ---------------------------------------

from test_agent_economy import READ_OK, SETTLED_NAV, _fn, server, tools  # noqa: E402,F401


@pytest.mark.asyncio
async def test_same_host_login_wall_suppresses_the_fused_read(tools, server):
    server.set_response("navigation.navigate", dict(
        SETTLED_NAV, url="https://example.org/login?next=%2Forders"))
    server.set_response("content.read", READ_OK)
    out = await (await _fn(tools, "navigate"))(url="https://example.org/orders", read="markdown")
    assert "READ SKIPPED" in out and "login" in out
    assert server.actions == ["navigation.navigate"]


@pytest.mark.asyncio
async def test_script_redirect_between_settle_and_read_discards_the_content(tools, server):
    server.set_response("navigation.navigate", SETTLED_NAV)
    server.set_response("content.read", dict(READ_OK, url="https://example.org/login"))
    out = await (await _fn(tools, "navigate"))(url="https://example.org/", read="markdown")
    assert "READ SKIPPED" in out and "https://example.org/login" in out
    assert "This domain is for use in examples." not in out
    assert server.actions == ["navigation.navigate", "content.read"]


@pytest.mark.asyncio
async def test_matching_read_url_still_returns_the_content(tools, server):
    server.set_response("navigation.navigate", SETTLED_NAV)
    server.set_response("content.read", READ_OK)
    out = await (await _fn(tools, "navigate"))(url="https://example.org/", read="markdown")
    assert "This domain is for use in examples." in out


@pytest.mark.asyncio
async def test_query_only_script_redirect_between_settle_and_read_discards_the_content(
        tools, server):
    server.set_response("navigation.navigate", dict(
        SETTLED_NAV, url="https://example.org/?view=all"))
    server.set_response("content.read", dict(READ_OK, url="https://example.org/?view=none"))
    out = await (await _fn(tools, "navigate"))(url="https://example.org/?view=all",
                                               read="markdown")
    assert "READ SKIPPED" in out and "view=none" in out
    assert "This domain is for use in examples." not in out


@pytest.mark.asyncio
async def test_tracking_only_change_between_settle_and_read_keeps_the_content(tools, server):
    server.set_response("navigation.navigate", dict(
        SETTLED_NAV, url="https://example.org/?utm_source=x"))
    server.set_response("content.read", dict(READ_OK, url="https://example.org/"))
    out = await (await _fn(tools, "navigate"))(url="https://example.org/?utm_source=x",
                                               read="markdown")
    assert "READ SKIPPED" not in out
    assert "This domain is for use in examples." in out
