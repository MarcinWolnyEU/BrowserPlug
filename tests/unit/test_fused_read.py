"""Unit tests for server/tools/fused_read.py — the fused navigate+read policy.

Pure-function tests (no browser, no server, no socket). What they defend:

  * `read_block_reason` — acceptance criterion 3, the centre of the module. Content must
    NEVER be returned for a page the agent did not ask for, so every navigation failure and
    every off-site landing suppresses the read leg, while a clean settle lets it run.
    The ONE deliberate non-blocker is a settle timeout: that IS the page the agent
    asked for, just possibly incomplete, so it is returned LABELLED rather than withheld.
  * `same_site` — the host comparison that decides "did I land where I asked?". It fails
    CLOSED (an unreadable URL is not a match) and it compares on label boundaries, so
    notallegro.pl cannot pass as allegro.pl the way a naive endswith would let it.
  * `leg_budgets` — a fused call must not change what an unfused navigate costs.
  * `read_plan` — records scope with `root`, text reads scope with `scope`; swapping them
    silently ignores the selector, which reads as "the page has no such subtree".
"""

import test_imports  # noqa: F401  (path setup)
from pathlib import Path

import pytest

from server.tools import fused_read
from server.tools.fused_read import (
    READ_FORMATS,
    RECORDS_MAX_DEFAULT,
    READ_LEG_TIMEOUT_SECONDS,
    SEPARATOR_RECORDS,
    SEPARATOR_TEXT,
    SEPARATOR_TIMEDOUT,
    host_of,
    leg_budgets,
    normalize_read,
    read_block_reason,
    read_failed_line,
    read_plan,
    read_request_error,
    read_skipped_line,
    same_site,
    separator_for,
)

# A settled, on-site navigation result — the shape every "must be allowed" case starts from.
CLEAN_NAV = {"url": "https://shop.example.pl/p/123", "tabId": 549, "title": "Widget",
             "settled": True, "waitedMs": 812}
ASKED_FOR = "https://shop.example.pl/p/123"


def _nav(**overrides):
    """CLEAN_NAV with the named fields replaced — one failure flag per test, so a
    passing case cannot be passing for the wrong reason."""
    nav = dict(CLEAN_NAV)
    nav.update(overrides)
    return nav


# ---- same_site / host_of ------------------------------------------------------------

@pytest.mark.parametrize("requested,landed,expected", [
    # www is not a different site.
    ("https://allegro.pl/x", "https://www.allegro.pl/y", True),
    ("https://www.allegro.pl/y", "https://allegro.pl/x", True),
    # An http->https upgrade is not a different site, and neither is a port.
    ("http://x.com", "https://x.com", True),
    ("http://dev.local:5173/a", "http://dev.local:8080/b", True),
    # A subdomain of the site you asked for is still the site you asked for.
    ("https://allegro.pl", "https://edge.allegro.pl", True),
    ("https://edge.allegro.pl", "https://allegro.pl", True),
    # The label-boundary check: a naive endswith would call this one True.
    ("https://allegro.pl", "https://notallegro.pl", False),
    ("https://notallegro.pl", "https://allegro.pl", False),
    # The case the fused read exists to catch: an SSO / consent wall on another host.
    ("https://shop.pl", "https://accounts.google.com", False),
    # Same machine, different host STRING — the hermetic stand-in for a real redirect.
    ("http://127.0.0.1:9/a", "http://localhost:9/b", False),
])
def test_same_site_table(requested, landed, expected):
    assert same_site(requested, landed) is expected


@pytest.mark.parametrize("requested,landed", [
    ("", "https://x.com"),
    ("https://x.com", ""),
    ("https://x.com", "garbage"),
    ("garbage", "https://x.com"),
    ("https://x.com", None),
    (None, "https://x.com"),
    ("https://x.com", "about:blank"),
    ("https://x.com", 42),
])
def test_same_site_fails_closed_on_anything_it_cannot_read(requested, landed):
    """An unreadable URL is never a match. The cost of a false refusal is one extra
    call; the cost of a false accept is content from a page nobody asked for."""
    assert same_site(requested, landed) is False


def test_same_site_never_matches_a_bare_tld_through_www_stripping():
    """"www.pl" must not collapse to "pl" — that would suffix-match every .pl host."""
    assert host_of("https://www.pl/") == "www.pl"
    assert same_site("https://www.pl/", "https://allegro.pl/") is False


@pytest.mark.parametrize("url,expected", [
    ("https://WWW.Allegro.PL:443/listing?q=1", "allegro.pl"),
    ("http://EDGE.allegro.pl", "edge.allegro.pl"),
    ("https://localhost:8772/health", "localhost"),
    ("about:blank", None),
    ("file:///C:/tmp/page.html", None),
    ("/relative/path", None),
    ("", None),
    (None, None),
])
def test_host_of(url, expected):
    assert host_of(url) == expected


# ---- read_block_reason: the failures that must suppress the read --------------------

@pytest.mark.parametrize("flag,value,expected_words", [
    ("netError", "NS_ERROR_CONNECTION_REFUSED", "network error"),
    ("aboutBlank", True, "about:blank"),
    ("superseded", True, "superseded"),
    ("tabClosed", True, "closed"),
])
def test_navigation_failures_suppress_the_read(flag, value, expected_words):
    reason = read_block_reason(_nav(**{flag: value}), ASKED_FOR)
    assert reason, f"{flag} must block the read leg"
    assert expected_words in reason


def test_clean_settle_allows_the_read():
    assert read_block_reason(CLEAN_NAV, ASKED_FOR) is None


def test_landing_on_an_unrelated_site_suppresses_the_read_and_names_both_hosts():
    """The AC-3 case in the wild: an SSO wall answers the navigation. The refusal has
    to name where it went AND where it was sent, or the agent cannot tell what happened."""
    nav = _nav(url="https://accounts.example-sso.com/login?next=%2Fp%2F123",
               title="Sign in")
    reason = read_block_reason(nav, ASKED_FOR)
    assert reason
    assert "accounts.example-sso.com" in reason
    assert "shop.example.pl" in reason


def test_a_subdomain_landing_is_not_an_unrelated_site():
    assert read_block_reason(_nav(url="https://m.shop.example.pl/p/123"), ASKED_FOR) is None


def test_settle_timeout_does_not_suppress_the_read():
    """The one deliberate non-blocker. A settle timeout is the page you asked for,
    possibly incomplete — withholding it repeats the act-step mistake of reporting a
    timeout as "nothing happened". It is returned, LABELLED."""
    nav = _nav(settled=False, timedOut=True, waitedMs=20000)
    assert read_block_reason(nav, ASKED_FOR) is None
    assert separator_for(nav, "markdown") == SEPARATOR_TIMEDOUT
    assert "TIMEOUT" in SEPARATOR_TIMEDOUT


def test_settle_timeout_on_an_unrelated_site_still_suppresses_the_read():
    """Not a blocker is not a bypass: the cross-site rule still applies to a timed-out
    navigation, so a half-loaded SSO wall is refused like a fully-loaded one."""
    nav = _nav(url="https://accounts.example-sso.com/login", settled=False, timedOut=True)
    assert read_block_reason(nav, ASKED_FOR)


def test_history_moves_can_never_trip_the_cross_site_rule():
    """back/forward/reload pass requested_url=None — there is no "you asked for X" to
    compare a history move against, so any landing is legitimate."""
    for landed in ("https://accounts.google.com/x", "https://anything.example", None):
        assert read_block_reason(_nav(url=landed), None) is None


def test_history_moves_still_report_navigation_failures():
    """requested_url=None only disables the URL comparison, not the failure flags."""
    assert read_block_reason(_nav(netError="NS_ERROR_UNKNOWN_HOST"), None)
    assert read_block_reason(_nav(tabClosed=True), None)


def test_an_unverifiable_landing_is_refused_rather_than_guessed_at():
    """No landing URL came back, so neither a match nor a mismatch can be claimed. Fail
    closed and say exactly that — do not invent a comparison the data does not support."""
    reason = read_block_reason(_nav(url=None), ASKED_FOR)
    assert reason
    assert "could not confirm" in reason
    assert "shop.example.pl" in reason
    assert "landed on" not in reason  # not dressed up as a mismatch it did not observe


def test_net_error_is_reported_in_preference_to_the_landing_host():
    """Order matters: the first CAUSE wins, so a connection failure is not reported as
    "you landed somewhere else" just because the tab is still on the previous page."""
    nav = _nav(url="https://previous.example.org/", netError="NS_ERROR_CONNECTION_REFUSED")
    assert read_block_reason(nav, ASKED_FOR) == "the navigation reported a network error"


def test_missing_or_malformed_navigation_result_is_refused():
    assert read_block_reason(None, ASKED_FOR)
    assert read_block_reason({}, ASKED_FOR)
    assert read_block_reason("not a dict", ASKED_FOR)


# ---- leg budgets --------------------------------------------------------------------

def test_leg_budgets_leave_the_navigation_leg_exactly_as_it_was():
    """navigate's budget has always been timeout_ms/1000 + 15. Wiring the fusion in must
    not move it, or an unfused navigate changes behaviour for a feature it never used."""
    assert leg_budgets(20000) == (35.0, 30.0)
    assert leg_budgets(60000) == (75.0, 30.0)
    assert leg_budgets(0) == (15.0, 30.0)


def test_the_read_leg_budget_does_not_scale_with_the_settle_budget():
    """timeout_ms buys settling, and settling is over before the read starts. A read leg
    that scaled would only buy worst-case latency."""
    budgets = {leg_budgets(ms)[1] for ms in (1000, 20000, 120000)}
    assert budgets == {READ_LEG_TIMEOUT_SECONDS}


# ---- read_plan ----------------------------------------------------------------------

@pytest.mark.parametrize("fmt", ["markdown", "text", "html"])
def test_text_reads_go_to_content_read_with_page_reads_own_shape(fmt):
    action, data = read_plan(fmt, tab_id=549, scope=None, max_chars=8000, offset=120,
                             frame_id=None)
    assert action == "content.read"
    assert data == {"format": fmt, "maxChars": 8000, "offset": 120, "tabId": 549}


def test_text_reads_scope_with_scope():
    _, data = read_plan("markdown", tab_id=1, scope="#main", max_chars=20000, offset=0,
                        frame_id=None)
    assert data["scope"] == "#main"
    assert "root" not in data


def test_records_reads_go_to_content_snapshot_and_scope_with_root():
    """The copy-paste bug this test exists for: records scopes with `root`. Sending
    `scope` there is silently ignored, i.e. the whole page is scanned and the agent is
    told nothing about it."""
    action, data = read_plan("records", tab_id=549, scope="#grid", max_chars=20000,
                             offset=0, frame_id=None)
    assert action == "content.snapshot"
    assert data["root"] == "#grid"
    assert "scope" not in data
    assert data["mode"] == "records"
    assert data["max"] == RECORDS_MAX_DEFAULT == 150
    assert data == {"root": "#grid", "max": 150, "includeHidden": False, "dedupe": True,
                    "mode": "records", "tabId": 549}


def test_records_reads_never_carry_the_text_read_arguments():
    _, data = read_plan("records", tab_id=1, scope=None, max_chars=999, offset=42,
                        frame_id=None)
    assert "maxChars" not in data and "offset" not in data and "format" not in data
    assert data["root"] is None  # sent even when unset, exactly as page_snapshot does


def test_read_plan_omits_optional_ids_it_was_not_given():
    """No tabId means the extension resolves the active tab — the convention every tool
    here follows. Sending tabId=None instead would be a stale-id error waiting to happen."""
    _, data = read_plan("text", tab_id=None, scope=None, max_chars=20000, offset=0,
                        frame_id=None)
    assert "tabId" not in data and "frameId" not in data


def test_read_plan_threads_the_frame_id_for_both_shapes():
    for fmt in ("markdown", "records"):
        _, data = read_plan(fmt, tab_id=7, scope=None, max_chars=20000, offset=0,
                            frame_id=3)
        assert data["frameId"] == 3, fmt


def test_read_plan_refuses_an_unknown_format():
    with pytest.raises(ValueError):
        read_plan("pdf", tab_id=1, scope=None, max_chars=20000, offset=0, frame_id=None)


# ---- argument validation ------------------------------------------------------------

def test_normalize_read_accepts_every_advertised_format():
    for fmt in READ_FORMATS:
        assert normalize_read(fmt) == fmt


def test_normalize_read_treats_absence_and_emptiness_as_no_read():
    assert normalize_read(None) is None
    assert normalize_read("") is None
    assert normalize_read("   ") is None


def test_normalize_read_is_forgiving_about_presentation():
    assert normalize_read(" Markdown ") == "markdown"
    assert normalize_read("RECORDS") == "records"


@pytest.mark.parametrize("bad", ["md", "text/html", "json", True, 3])
def test_normalize_read_rejects_anything_it_does_not_know(bad):
    with pytest.raises(ValueError):
        normalize_read(bad)


def test_unknown_read_value_is_refused_with_the_accepted_values():
    err = read_request_error("md", "settled")
    assert err == 'Error: unknown read="md" (use markdown | text | html | records)'


def test_read_with_wait_none_is_refused_before_any_wire_request():
    """wait="none" returns before the load starts, so there is nothing to read. The
    refusal names the working alternative rather than just declining."""
    err = read_request_error("markdown", "none")
    assert err.startswith("Error: read= needs a loaded page")
    assert 'wait="settled"' in err
    assert "drop read=" in err


def test_wait_none_without_read_is_fine():
    assert read_request_error(None, "none") is None
    assert read_request_error("", "none") is None


def test_valid_combinations_pass_validation():
    for fmt in READ_FORMATS:
        for wait in ("dom", "settled", "load"):
            assert read_request_error(fmt, wait) is None
    assert read_request_error(None, "settled") is None


def test_an_unknown_value_is_reported_even_when_wait_is_also_wrong():
    """Both are wrong; the value is the one the agent typed, so name it first."""
    assert "unknown read" in read_request_error("md", "none")


# ---- the appended lines -------------------------------------------------------------

def test_read_skipped_line_names_the_cause_and_the_next_call():
    line = read_skipped_line("landed on accounts.example-sso.com, not the shop.example.pl "
                             "you asked for", 549)
    assert line.startswith("\n  READ SKIPPED: ")
    assert "accounts.example-sso.com" in line
    # The escape hatch is a call the agent can paste — no override argument needed.
    assert "page_read(tab_id=549)" in line
    # And the navigation result above it still stands; this is not a failed call.
    assert "the navigation result above is what happened" in line


def test_read_failed_line_never_erases_the_navigation_result():
    """A read-leg error must not read as "nothing happened" — the tab DID move, and an
    agent told otherwise navigates again."""
    line = read_failed_line("page read failed: Defuddle threw", 549)
    assert "READ FAILED after a successful navigation" in line
    assert "the tab DID move" in line
    assert "page_read(tab_id=549)" in line


def test_the_escape_hatch_degrades_to_the_active_tab_when_there_is_no_tab_id():
    """page_read(tab_id=None) is advice that does not run."""
    for line in (read_skipped_line("the tab closed during navigation", None),
                 read_failed_line("boom", None)):
        assert "page_read()" in line
        assert "tab_id=None" not in line


# ---- separators ---------------------------------------------------------------------

def test_separator_for_picks_the_block_label():
    assert separator_for(CLEAN_NAV, "markdown") == SEPARATOR_TEXT
    assert separator_for(CLEAN_NAV, "records") == SEPARATOR_RECORDS
    assert separator_for(_nav(timedOut=True), "html") == SEPARATOR_TIMEDOUT
    assert separator_for(None, "text") == SEPARATOR_TEXT


def test_separators_are_distinct_and_fence_the_block_off_from_the_diagnostics():
    seps = (SEPARATOR_TEXT, SEPARATOR_TIMEDOUT, SEPARATOR_RECORDS)
    assert len(set(seps)) == 3
    for sep in seps:
        assert sep.startswith("\n\n---") and sep.endswith("---\n")


def test_module_is_pure_policy_with_no_server_imports():
    """It is imported by mcp_tools, never the other way round; keeping it free of server
    state is what makes the whole fused-read rule testable without a browser.

    Checked on the parsed module, so a mention in a comment or docstring neither passes
    nor fails it: only an import statement or a name reference counts."""
    import ast
    import sys
    tree = ast.parse(Path(fused_read.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            # The only sanctioned siblings are the pure policy modules (url_diff, url_norm),
            # imported by absolute name. Deliberate change for T5: one shared URL
            # normaliser instead of two that disagree.
            assert not node.level, "a relative import reaches into the server package"
            if node.module in ("server.tools.url_diff", "server.tools.url_norm"):
                continue
            imported.add((node.module or "").split(".")[0])
    stdlib = set(sys.stdlib_module_names)
    assert imported <= stdlib, f"fused_read imports non-stdlib modules: {sorted(imported - stdlib)}"
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert "BrowserPlugTools" not in names
    assert not hasattr(fused_read, "BrowserPlugTools")
    assert fused_read.__doc__ and "ONLY that page" in fused_read.__doc__
