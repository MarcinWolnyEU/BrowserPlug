"""server/tools/browser_proxy.py: what an agent is told when a proxy switches a whole browser.

The wording is a contract (an agent that is not told leaves a person's browser on Tor), so the
four required facts are pinned: that it happened, how to undo it, that it should be undone at
the first opportunity after the current set of operations, and that turning it on again is free
apart from every tab (the user's included) being switched."""

from server.tools import browser_proxy as bp

CHROME = "[2] Google Chrome Canary pid 24188"
ENDPOINT = "socks5://127.0.0.1:50767"


def test_the_on_notice_states_all_four_facts():
    text = bp.on_notice(CHROME, ENDPOINT, via_tor=True)
    assert "BROWSER-WIDE PROXY ON" in text and "ENTIRE BROWSER" in text            # it happened
    assert CHROME in text and f"Tor ({ENDPOINT})" in text
    assert "the user may be using right now" in text
    assert 'proxy=""' in text and bp.OFF_CALL in text                                # the undo
    assert "first opportunity after you finish the current set of operations" in text
    assert "Switching it on again is free" in text                                   # re-enabling
    assert "all tabs, including those the user may be actively using" in text
    assert f"switched to Tor ({ENDPOINT}) each time it is on" in text


def test_a_plain_endpoint_is_named_as_itself_and_tor_only_when_asked_for():
    assert f"Tor ({ENDPOINT})" not in bp.on_notice(CHROME, ENDPOINT)
    assert ENDPOINT in bp.on_notice(CHROME, ENDPOINT)


def test_already_on_says_so_and_still_carries_the_way_out():
    text = bp.on_notice(CHROME, ENDPOINT, already=True)
    assert "already on" in text and "ENTIRE BROWSER" not in text
    assert bp.OFF_CALL in text


def test_the_off_notice_names_what_it_was_or_says_nothing_changed():
    assert ENDPOINT in bp.off_notice(CHROME, ENDPOINT)
    assert "BROWSER-WIDE PROXY OFF" in bp.off_notice(CHROME, ENDPOINT)
    assert "nothing changed" in bp.off_notice(CHROME, None)


def test_the_state_line_is_empty_unless_the_proxy_is_on():
    assert bp.state_line(CHROME, None) == ""
    line = bp.state_line(CHROME, {"endpoint": ENDPOINT})
    assert ENDPOINT in line and bp.OFF_CALL in line


def test_change_notice_dispatches_on_the_extensions_report():
    assert bp.change_notice(CHROME, None) == ""
    assert "OFF" in bp.change_notice(CHROME, {"off": True, "was": ENDPOINT})
    assert "ON" in bp.change_notice(CHROME, {"off": False, "endpoint": ENDPOINT})


def test_the_hello_decides_whether_a_proxy_means_the_whole_browser():
    assert bp.is_browser_wide({"proxy_scope": "browser"})
    assert not bp.is_browser_wide({"has_proxy_permission": True})
    assert not bp.is_browser_wide(None)
