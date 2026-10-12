"""Naming a browser that has no process here: what the popup's Connections row says.

A LOCAL browser is named by probing its process, which is the only thing that can see
past a fork's "I am Firefox". A REMOTE one has no process on this machine, so its hello
is the whole story, and the row used to give up and print the socket's address:

    [2] browser extension - remote-192.168.30.171

That is the right answer for a remote DESKTOP Firefox, whose hello says "Firefox" and
nothing more. It is a bad one for a phone, which says "Firefox Nightly" and reports
`os: android` - enough to name it "Firefox for Android Nightly" outright. The address
still matters; it just stopped being the name. It moved to the detail line, and
"Remote" became a marker on the slot.

Firefox ships four builds on Android (release / Beta / Nightly, plus Focus, a separate
product sharing the first word), so the naming is derived from what the browser claims
rather than matched against a list of them.
"""

from unittest.mock import patch

import pytest

from server.browsers.browser_identity import platform_edition
from server.server import BrowserPlugServer


ANDROID = {"os": "android", "arch": "aarch64"}


class _Sock:
    """The two attributes BrowserRegistry.register reads off a live socket."""

    close_code = None

    def __init__(self, addr):
        self.remote_address = (addr, 51000)


def _hello(name, version="157.0a1"):
    return {"name": name, "vendor": "Mozilla", "version": version,
            "buildID": "20260901215037"}


def _remote(server, instance_id, browser_info, *, addr="192.168.30.171",
            platform=ANDROID, version="1.7.12"):
    """Register a connected REMOTE browser, as a connect + extension_hello would."""
    browser = server.registry.register(_Sock(addr))
    browser = server.registry.promote(browser, instance_id)
    server.registry.handle_hello(browser, {
        "instance_id": instance_id, "version": version,
        "platform": platform, "browserInfo": browser_info,
    })
    return server.browsers[instance_id]


def _local(server, instance_id, *, pid=4472, friendly="Firefox Developer Edition"):
    ident = {"pid": pid, "friendly": friendly, "brand": "Firefox",
             "exe_path": "C:/Program Files/Firefox Developer Edition/firefox.exe"}
    with patch.object(server, "_probe_browser_identity", return_value=ident):
        browser = server.registry.register(_Sock("127.0.0.1"))
    browser = server.registry.promote(browser, instance_id)
    browser.version = "1.7.33"
    return browser


@pytest.fixture
def server():
    return BrowserPlugServer(host="localhost", port=8772, start_mcp=False)


def _rows(server):
    return [r for r in server.get_connections_info()["connections"]
            if r["kind"] == "extension"]


# --- the pure function ---------------------------------------------------------------

class TestPlatformEdition:
    """All four Android Firefoxes, from the one thing each of them claims."""

    @pytest.mark.parametrize("claimed, expected", [
        ("Firefox Nightly", "Firefox for Android Nightly"),
        ("Firefox Beta", "Firefox for Android Beta"),
        ("Firefox", "Firefox for Android"),
        # Focus is a DIFFERENT browser, not a Firefox release train, so it keeps its
        # whole name and takes the OS behind it. "Firefox for Android Focus" would file
        # it as a channel of Firefox.
        ("Firefox Focus", "Firefox Focus for Android"),
        ("Firefox Klar", "Firefox Klar for Android"),
    ])
    def test_every_android_firefox_gets_a_name_of_its_own(self, claimed, expected):
        assert platform_edition(claimed, "Firefox", ANDROID) == expected

    @pytest.mark.parametrize("os_name", ["win", "mac", "linux", "openbsd"])
    def test_a_desktop_browser_is_left_exactly_as_it_named_itself(self, os_name):
        assert platform_edition("Firefox Nightly", "Firefox", {"os": os_name}) \
            == "Firefox Nightly"

    def test_it_never_says_android_twice(self):
        once = platform_edition("Firefox Nightly", "Firefox", ANDROID)
        assert platform_edition(once, "Firefox", ANDROID) == once

    def test_nothing_to_work_with_changes_nothing(self):
        assert platform_edition("Firefox", "Firefox", None) == "Firefox"
        assert platform_edition("Firefox", "Firefox", {}) == "Firefox"
        assert platform_edition(None, "Firefox", ANDROID) is None

    def test_a_blink_browser_on_a_phone_is_not_called_firefox(self):
        assert platform_edition("Chrome", "Chrome", ANDROID) == "Chrome for Android"


# --- what the registry entry answers -------------------------------------------------

class TestRemoteBrowserEntry:

    def test_a_phone_names_itself_all_the_way_down(self, server):
        b = _remote(server, "phone-1", _hello("Firefox Nightly"))
        assert b.family == "Firefox"
        assert b.edition == "Firefox for Android Nightly"
        assert b.display_name == "Firefox for Android Nightly"

    def test_a_remote_desktop_firefox_still_has_no_name_worth_printing(self, server):
        # It claims exactly its family and nothing else. Printing "Firefox" would be
        # true and useless: it does not pick out a machine on the LAN.
        b = _remote(server, "desktop-1", _hello("Firefox", version="155.0"),
                    platform={"os": "linux", "arch": "x86-64"})
        assert b.edition == "Firefox"
        assert b.display_name == "browser extension"

    def test_a_local_browser_is_named_by_its_process_as_before(self, server):
        b = _local(server, "local-1")
        assert b.display_name == "Firefox Developer Edition"
        assert b.name == "Firefox Developer Edition pid 4472"


# --- the row the popup renders -------------------------------------------------------

class TestConnectionsRow:

    def test_the_phone_row_reads_as_the_browser_it_is(self, server):
        _local(server, "local-1")
        _remote(server, "phone-1", _hello("Firefox Nightly"))
        rows = _rows(server)
        # The name only. "Remote" stays in it because no column of the panel carries
        # that; the slot and the pid left because two of them do.
        assert [r["label"] for r in rows] == ["Firefox Developer Edition",
                                              "[Remote] Firefox for Android Nightly"]
        assert [(r["slot"], r["pid"]) for r in rows] == [(1, 4472), (2, None)]

    def test_remote_is_marked_even_when_it_is_the_only_browser(self, server):
        # There is no slot to choose between, but "this is not the browser on this
        # machine" is a fact about the row either way.
        _remote(server, "phone-1", _hello("Firefox Nightly"))
        assert _rows(server)[0]["label"] == "[Remote] Firefox for Android Nightly"

    def test_the_address_moved_to_the_detail_line_rather_than_being_dropped(self, server):
        _remote(server, "phone-1", _hello("Firefox Nightly"))
        assert _rows(server)[0]["detail"].endswith("from 192.168.30.171")

    def test_a_local_row_still_says_nothing_about_being_remote(self, server):
        _local(server, "local-1")
        row = _rows(server)[0]
        assert row["label"] == "Firefox Developer Edition"
        assert row["detail"] == f"WebSocket: {server.port}"

    def test_two_phones_are_told_apart_by_slot_and_address(self, server):
        _remote(server, "phone-1", _hello("Firefox Nightly"))
        _remote(server, "phone-2", _hello("Firefox Nightly"), addr="192.168.30.172")
        rows = _rows(server)
        assert [r["label"] for r in rows] == [
            "[Remote] Firefox for Android Nightly",
            "[Remote] Firefox for Android Nightly"]
        assert [r["slot"] for r in rows] == [1, 2]
        assert [r["detail"].rsplit(" ", 1)[-1] for r in rows] == [
            "192.168.30.171", "192.168.30.172"]

    def test_the_row_carries_what_identifies_the_browser(self, server):
        # The popup's tooltip and its "Copy connection info" print these; for a remote
        # browser they are the only identification there is (no pid, no exe path).
        _remote(server, "phone-1", _hello("Firefox Nightly"))
        row = _rows(server)[0]
        assert row["platform"] == ANDROID
        assert row["browser_info"]["name"] == "Firefox Nightly"
        assert row["browser_info"]["buildID"] == "20260901215037"
        assert row["version"] == "1.7.12"
        assert row["remote_addr"] == "192.168.30.171"
