"""
Unit tests for version skew across the three components (server, launcher, extension).

Two layers:
  * server/reporting/version_check.py — the pure sentence builder. Its EXACT wording is the
    contract with whoever reads /health (an agent, usually), so the phrasing is
    asserted verbatim, including the closing "update the server to X" clause that
    only appears when something is NEWER than the server.
  * server.py's wiring — get_connections_info()/get_health_info() must build that
    sentence from the parties they actually report, and the browser_state push that
    feeds the connection map's browser box must survive junk without corrupting it.

No browser, no websockets: the registry and the HTTP-client table are driven directly.
"""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from server.browsers.browser_registry import (ACTIVE_TAB_LIST_MAX, ACTIVE_TAB_TITLE_MAX,
                                              handle_browser_state)
from server.reporting import client_identity
from server.server import BrowserPlugServer
from browserplug_shared.version import __version__ as BROWSERPLUG_VERSION
from server.reporting.version_check import (build_version_warning, compare_versions,
                                            parse_version)

SV = "1.7.5"   # "the server" in these tests; independent of the real __version__


def browser(name, pid, version):
    return {"kind": "browser", "name": name, "pid": pid, "version": version}


def launcher(name, pid, version, unversioned=False):
    return {"kind": "launcher", "name": name, "pid": pid, "version": version,
            "unversioned": unversioned}


class TestParsing:

    @pytest.mark.parametrize("text,expected", [
        ("1.7.5", (1, 7, 5)),
        ("v1.7.5", (1, 7, 5)),
        ("1.7", (1, 7)),
        ("1.7.5-beta", (1, 7, 5)),   # pre-release tags are not ordered, only ignored
        ("1.7.5b", (1, 7, 5)),
        ("", None),
        ("unknown", None),
        (None, None),
        (17, None),
    ])
    def test_parse(self, text, expected):
        assert parse_version(text) == expected

    def test_compare_pads_missing_components(self):
        assert compare_versions("1.7", "1.7.0") == 0
        assert compare_versions("1.7.1", "1.7") == 1
        assert compare_versions("1.10.0", "1.9.9") == 1, "numeric, not lexicographic"

    def test_compare_is_none_when_either_side_is_unreadable(self):
        assert compare_versions("1.7.5", "unknown") is None
        assert compare_versions(None, "1.7.5") is None


class TestWarningText:

    def test_everything_matching_is_silence(self):
        assert build_version_warning(SV, [browser("Firefox", 111, SV),
                                          launcher("Claude Code", 888, SV)]) is None

    def test_no_parties_at_all(self):
        assert build_version_warning(SV, []) is None

    def test_a_newer_browser_says_what_to_update_the_server_to(self):
        out = build_version_warning(SV, [browser("Zen Browser", 38288, "1.7.6")])
        assert out == (
            "Browser Zen Browser (PID 38288) has a newer version (1.7.6) than the "
            "server (1.7.5), after finishing the current tasks notify the user that he "
            "should update the server to 1.7.6, version mismatch can cause unpredictable "
            "issues")

    def test_an_older_launcher_is_reported_without_the_update_clause(self):
        out = build_version_warning(SV, [launcher("Claude Code", 12345, "1.7.4")])
        assert out == ("Launcher for Claude Code (PID 12345) has an older version (1.7.4) "
                       "than the server (1.7.5)")
        assert "update the server" not in out, (
            "nothing is newer than the server, so telling the user to move the server "
            "forward would be wrong advice")

    def test_several_parties_are_comma_separated_newest_first(self):
        out = build_version_warning(SV, [
            launcher("Claude Code", 12345, "1.7.4"),
            browser("Zen Browser", 38288, "1.7.6"),
            browser("Firefox Developer Edition", 111, "1.8.0"),
        ])
        assert out == (
            "Browser Firefox Developer Edition (PID 111) has a newer version (1.8.0) than "
            "the server (1.7.5), "
            "Browser Zen Browser (PID 38288) has a newer version (1.7.6) than the "
            "server (1.7.5), "
            "Launcher for Claude Code (PID 12345) has an older version (1.7.4) than the "
            "server (1.7.5), "
            "after finishing the current tasks notify the user that he should update the "
            "server to 1.8.0, version mismatch can cause unpredictable issues")

    def test_launchers_of_one_name_on_one_version_share_a_clause(self):
        # The ordinary state of a machine running several Claude Code sessions: one
        # sentence per launcher said the same thing seven times over.
        out = build_version_warning(SV, [launcher("Claude Code", pid, "1.7.4")
                                         for pid in (40244, 1464, 6904)])
        assert out == ("Launchers for Claude Code (PIDs 1464, 6904, 40244) have an older "
                       "version (1.7.4) than the server (1.7.5)"), (
            "pids sorted as numbers, not as strings — 6904 belongs before 40244")

    def test_one_name_spread_over_versions_keeps_every_pid_with_its_own_version(self):
        out = build_version_warning(SV, [
            launcher("Claude Code", 1464, "1.7.4"),
            launcher("Claude Code", 24616, "1.7.2"),
            launcher("Claude Code", 36620, "1.7.4"),
            launcher("Claude Code", 6904, None, unversioned=True),
        ])
        assert out == (
            "Launchers for Claude Code have older versions than the server (1.7.5): "
            "1.7.4 (PIDs 1464, 36620), 1.7.2 (PID 24616), "
            "unknown — predates version reporting (PID 6904)"), (
            "merging may shorten the sentence but must never lose which pid runs which "
            "version — that is what the reader restarts on")

    def test_different_browsers_on_the_same_version_share_a_clause(self):
        out = build_version_warning(SV, [browser("Zen Browser", 2, "1.7.6"),
                                         browser("Firefox", 1, "1.7.6")])
        assert out.startswith(
            "Browsers Firefox (PID 1) and Zen Browser (PID 2) have a newer version "
            "(1.7.6) than the server (1.7.5), "), (
            f"'and' rather than a comma: clauses are comma-joined, so a comma here would "
            f"read as the end of the clause; got: {out}")

    def test_a_lone_party_reads_exactly_as_it_did_before_any_merging(self):
        out = build_version_warning(SV, [browser("Zen Browser", 38288, "1.7.6"),
                                         launcher("Claude Code", 12345, "1.7.4")])
        assert out.startswith("Browser Zen Browser (PID 38288) has a newer version")
        assert "Launcher for Claude Code (PID 12345) has an older version (1.7.4)" in out

    def test_the_update_target_is_the_newest_version_anywhere(self):
        out = build_version_warning(SV, [browser("A", 1, "1.7.6"), browser("B", 2, "2.0.0")])
        assert "update the server to 2.0.0" in out

    def test_a_remote_browser_without_a_pid_still_gets_named(self):
        out = build_version_warning(SV, [browser("remote-192.168.0.7", None, "1.7.6")])
        assert out.startswith("Browser remote-192.168.0.7 (PID unknown) has a newer version")

    def test_an_unreadable_version_is_skipped_rather_than_guessed_at(self):
        assert build_version_warning(SV, [browser("Firefox", 111, "unknown")]) is None

    def test_a_launcher_too_old_to_report_a_version_is_itself_the_evidence(self):
        out = build_version_warning(SV, [launcher("Kilo CLI", 999, None, unversioned=True)])
        assert out == ("Launcher for Kilo CLI (PID 999) has an older version "
                       "(unknown — predates version reporting) than the server (1.7.5)")

    def test_our_own_unreadable_version_never_produces_a_warning(self):
        assert build_version_warning("", [browser("Firefox", 111, "1.7.6")]) is None


def connect(server, instance_id, *, pid=None, version="1.7.5"):
    """Register a connected browser as a connect + extension_hello would."""
    sock = AsyncMock()
    sock.close_code = None
    sock.remote_address = ("127.0.0.1", 40000 + (pid or 0) % 1000)
    ident = {"pid": pid, "friendly": "Firefox",
             "exe_path": r"C:\Program Files\Firefox\firefox.exe"} if pid else {}
    with patch.object(server, "_probe_browser_identity", return_value=ident):
        b = server.registry.register(sock)
    b = server.registry.promote(b, instance_id)
    b.version = version
    return b


def add_client(server, sid, *, pid, launcher_pid, friendly="Claude Code"):
    now = datetime.now()
    server.http_clients[sid] = {
        "pid": pid, "friendly": friendly, "exe_path": r"C:\claude\claude.exe",
        "cwd": r"C:\dr\browserplug", "start_time": now, "launcher_pid": launcher_pid,
        "launcher_start_time": now, "peer_port": 50000, "first_seen": now,
        "last_activity": now, "history": [],
    }


class TestServerWiring:

    @pytest.fixture
    def server(self):
        return BrowserPlugServer(host="localhost", port=8772, start_mcp=False)

    def test_a_matched_fleet_warns_about_nothing(self, server):
        connect(server, "ff-1", pid=111, version=BROWSERPLUG_VERSION)
        add_client(server, "s1", pid=777, launcher_pid=888)
        server.record_launcher_version(888, BROWSERPLUG_VERSION)
        assert server.get_connections_info()["version_warning"] is None
        assert server.get_health_info()["version_warning"] is None
        assert server.get_health_info()["server_version"] == BROWSERPLUG_VERSION

    def test_health_names_the_browser_and_the_launcher_that_drifted(self, server):
        connect(server, "ff-1", pid=111, version="99.0.0")
        add_client(server, "s1", pid=777, launcher_pid=888)
        server.record_launcher_version(888, "0.0.1")
        warning = server.get_health_info()["version_warning"]
        assert "Browser Firefox (PID 111) has a newer version (99.0.0)" in warning, (
            f"the pid belongs in the (PID n) slot, not twice — got: {warning}")
        assert "Launcher for Claude Code (PID 888) has an older version (0.0.1)" in warning
        assert "update the server to 99.0.0" in warning

    def test_a_launcher_that_never_reported_a_version_is_called_out_as_older(self, server):
        add_client(server, "s1", pid=777, launcher_pid=888)
        row = next(c for c in server.get_connections_info()["connections"]
                   if c["kind"] == "mcp_client")
        assert row["launcher_version"] is None
        assert "predates version reporting" in server.get_health_info()["version_warning"]

    def test_a_direct_http_client_has_no_launcher_to_be_out_of_step(self, server):
        add_client(server, "s1", pid=777, launcher_pid=None, friendly="GitLab Duo")
        assert server.get_connections_info()["version_warning"] is None

    def test_a_launchers_version_expires_when_it_stops_polling(self, server):
        server.record_launcher_version(888, "1.0.0")
        assert server.launcher_version(888) == "1.0.0"
        stale = datetime.now() - timedelta(seconds=client_identity.LAUNCHER_VERSION_TTL_SECONDS + 5)
        server.launcher_versions[888]["seen"] = stale
        assert server.launcher_version(888) is None, (
            "a launcher that exited must not keep producing a skew warning")

    def test_a_remote_browser_is_named_by_its_address(self, server):
        b = connect(server, "ff-remote", pid=None, version="99.0.0")
        b.is_remote, b.remote_addr = True, "192.168.0.7"
        warning = server.get_health_info()["version_warning"]
        assert "Browser remote-192.168.0.7 (PID unknown)" in warning, (
            f"a remote browser has no pid and a generic edition — its ADDRESS is the "
            f"only thing that identifies it; got: {warning}")

    def test_a_disconnected_browser_is_not_compared(self, server):
        b = connect(server, "ff-1", pid=111, version="99.0.0")
        b.ws.close_code = 1000
        assert server.get_connections_info()["version_warning"] is None


class TestBrowserState:
    """The browser_state push behind the map's BROWSER box (windows/tab counts)."""

    @pytest.fixture
    def server(self):
        return BrowserPlugServer(host="localhost", port=8772, start_mcp=False)

    def instance(self, server):
        return server.get_connections_info()["extension_instances"][0]

    def test_counts_reach_the_registry_and_the_payload(self, server):
        b = connect(server, "ff-1", pid=111)
        handle_browser_state(b, {"windows": 3, "tabs": 42,
                                         "active_tabs": 3, "loaded_tabs": 12})
        inst = self.instance(server)
        assert (inst["windows"], inst["tabs"]) == (3, 42)
        assert (inst["active_tabs"], inst["loaded_tabs"]) == (3, 12)
        assert inst["state_hms"], "and when it was measured"

    def test_absent_until_the_first_push(self, server):
        connect(server, "ff-1", pid=111)
        inst = self.instance(server)
        assert inst["windows"] is None and inst["tabs"] is None, (
            "an extension too old to push counts must report NOTHING — a 0 would be a lie")
        assert inst["active_tab_list"] is None, (
            "and NULL, not [] — an empty list would claim the browser has no active tabs")
        assert inst["state_hms"] is None

    def test_the_active_tabs_are_listed_not_just_counted(self, server):
        b = connect(server, "ff-1", pid=111)
        handle_browser_state(b, {
            "windows": 2, "tabs": 9, "active_tabs": 2, "loaded_tabs": 4,
            "active_tab_list": [
                {"id": 7, "title": "GitLab · merge requests", "last_hms": "12:00:01"},
                {"id": 9, "title": "", "last_hms": None},
            ],
        })
        listed = self.instance(server)["active_tab_list"]
        assert [t["id"] for t in listed] == [7, 9]
        assert listed[0]["title"] == "GitLab · merge requests"
        assert listed[1]["last_hms"] is None, "a tab we know nothing about says so"

    def test_a_browser_with_no_active_tabs_says_so_rather_than_going_quiet(self, server):
        b = connect(server, "ff-1", pid=111)
        handle_browser_state(b, {"windows": 0, "tabs": 0, "active_tab_list": []})
        assert self.instance(server)["active_tab_list"] == [], (
            "[] means 'none'; only a missing push reads as 'never said'")

    @pytest.mark.parametrize("entry", ["nope", 42, None, {"title": "no id"},
                                       {"id": True, "title": "bools are not ids"},
                                       {"id": "7"}])
    def test_unusable_tab_entries_are_dropped_not_forwarded(self, server, entry):
        # This list lands verbatim in a tooltip, and a REMOTE browser is the one
        # pushing it: an entry we can't read is dropped, never passed through.
        b = connect(server, "ff-1", pid=111)
        handle_browser_state(b, {"tabs": 1, "active_tab_list": [
            entry, {"id": 3, "title": "real", "last_hms": "12:00:00"}]})
        assert self.instance(server)["active_tab_list"] == [
            {"id": 3, "title": "real", "last_hms": "12:00:00"}]

    def test_the_tab_list_is_capped_and_titles_are_bounded(self, server):
        b = connect(server, "ff-1", pid=111)
        handle_browser_state(b, {"tabs": 99, "active_tab_list": [
            {"id": i, "title": "x" * 500, "last_hms": "12:00:00"} for i in range(50)]})
        listed = self.instance(server)["active_tab_list"]
        assert len(listed) == ACTIVE_TAB_LIST_MAX
        assert all(len(t["title"]) == ACTIVE_TAB_TITLE_MAX for t in listed)

    def test_the_extension_version_rides_alongside_the_browser_facts(self, server):
        connect(server, "ff-1", pid=111, version="1.7.5")
        assert self.instance(server)["version"] == "1.7.5"

    @pytest.mark.parametrize("junk", [None, "3", [], {"windows": "many"},
                                      {"tabs": -1}, {"tabs": True}])
    def test_junk_never_lands(self, server, junk):
        b = connect(server, "ff-1", pid=111)
        handle_browser_state(b, {"windows": 1, "tabs": 2})
        handle_browser_state(b, junk)
        assert b.browser_state == {"windows": 1, "tabs": 2}, (
            "a malformed push leaves the last good state alone")

    def test_a_push_is_not_browser_ACTIVITY(self, server):
        b = connect(server, "ff-1", pid=111)
        b.last_action = None
        handle_browser_state(b, {"windows": 1, "tabs": 1})
        assert b.last_action is None, (
            "diagnostics traffic must not light the connection map's edge — only real "
            "browser commands do")
