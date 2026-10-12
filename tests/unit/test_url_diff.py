"""Unit tests for server/tools/url_diff.py — the requested-vs-landed landing check.

Pure-function tests (no browser, no server, no network). Two things are being
defended, and the second is the harder one:

  1. The wrong answer actually delivered to a user. Requesting
     `?string=RTX+3090&order=pd&stan=uzywane` and landing on
     `?string=RTX%2B3090&order=pd` searched for the literal string "RTX+3090"
     with the used-condition filter GONE, and navigate reported plain success.
     Both changes must be named, exactly.

  2. Silence on everything benign. A warning that fires on ordinary navigations
     is worse than no warning — the agent learns to skip it and (1) goes back to
     being invisible. Every normalisation a browser or CDN performs routinely
     (scheme upgrade, www., trailing slash, default ports, fragments, parameter
     order, tracking params, %20 <-> +) is pinned as a NON-warning here.
"""

import pytest

import test_imports  # noqa: F401  (path setup)
from server.tools.url_diff import (
    MAX_URL_CHARS,
    QueryChange,
    diff_urls,
    interstitial_evidence,
    is_ip_literal,
    landing_report,
    normalize_host,
    normalize_path,
    plus_literalized,
    registrable_domain,
    split_query,
)

# The ground truth, re-measured live through the real browser on 2026-08-03.
# Do not "tidy" these two strings.
ALLEGRO_REQUESTED = "https://allegro.pl/listing?string=RTX+3090&order=pd&stan=uzywane"
ALLEGRO_LANDED = "https://allegro.pl/listing?string=RTX%2B3090&order=pd"


# ---- the repro that produced a wrong answer ------------------------------------------

def test_allegro_repro_names_both_changes_verbatim():
    """The whole feature in one assertion: the dropped filter AND the space that
    became a literal '+', in one block, with the redirect that did it named."""
    out = landing_report(ALLEGRO_REQUESTED, ALLEGRO_LANDED,
                         redirects=[{"to": ALLEGRO_LANDED, "status": 308}])
    assert out == (
        "\n  LANDED ELSEWHERE — the loaded URL is not the one requested"
        " (changed across a 308 redirect)."
        f"\n    requested: {ALLEGRO_REQUESTED}"
        f"\n    landed:    {ALLEGRO_LANDED}"
        '\n    - query parameter DROPPED: stan="uzywane" — a dropped filter changes what'
        " every result on this page means"
        '\n    - query parameter RE-ENCODED: string — you sent "RTX 3090", the page loaded'
        ' "RTX+3090" (a literal + arrived instead of a space; send %20 for a space)'
        "\n    Answers read from this page describe the LOADED url; re-issue with the"
        " exact url if the difference matters."
    )


def test_allegro_repro_says_nothing_about_the_parameter_that_survived():
    # order=pd came through untouched; naming it would be noise in the one place
    # where the agent must read every line. (It still appears inside the echoed
    # URLs, so look only at the change lines.)
    out = landing_report(ALLEGRO_REQUESTED, ALLEGRO_LANDED)
    change_lines = out.split("\n    - ")[1:]
    assert len(change_lines) == 2
    assert not any("order" in line for line in change_lines)
    assert "DIFFERENT PATH" not in out          # /listing -> /listing
    assert "DIFFERENT HOST" not in out


def test_landing_header_omits_the_hop_clause_when_no_redirect_was_seen():
    out = landing_report(ALLEGRO_REQUESTED, ALLEGRO_LANDED)
    assert out.startswith("\n  LANDED ELSEWHERE — the loaded URL is not the one requested.")
    assert "redirect" not in out


def test_tesla_v100_repro_reports_exactly_one_reencoding():
    out = landing_report("https://www.ceneo.pl/;szukaj-Tesla+V100+32GB",
                         "https://www.ceneo.pl/;szukaj-Tesla%2BV100%2B32GB")
    assert out.count("query parameter") == 0     # the mangling is in the PATH here
    assert "DIFFERENT PATH: /;szukaj-Tesla+V100+32GB -> /;szukaj-Tesla%2BV100%2B32GB" in out


def test_query_form_of_the_tesla_repro_is_a_single_reencoded_line():
    out = landing_report("https://shop.example.com/s?q=Tesla+V100+32GB",
                         "https://shop.example.com/s?q=Tesla%2BV100%2B32GB")
    assert out.count("\n    - ") == 1
    assert ('- query parameter RE-ENCODED: q — you sent "Tesla V100 32GB", the page loaded '
            '"Tesla+V100+32GB" (a literal + arrived instead of a space; send %20 for a space)'
            ) in out


# ---- the three space encodings (the exact mechanism of the bug) ----------------------

def test_plus_and_percent20_encode_the_SAME_value_and_stay_silent():
    # In a query string '+' means space. "RTX+3090" and "RTX%203090" are one value.
    assert landing_report("https://x.test/s?q=RTX+3090", "https://x.test/s?q=RTX%203090") == ""
    assert landing_report("https://x.test/s?q=RTX%203090", "https://x.test/s?q=RTX+3090") == ""


def test_plus_becoming_percent2B_is_a_different_value_and_warns():
    out = landing_report("https://x.test/s?q=RTX+3090", "https://x.test/s?q=RTX%2B3090")
    assert "RE-ENCODED: q" in out
    assert 'you sent "RTX 3090", the page loaded "RTX+3090"' in out
    assert "send %20 for a space" in out         # the remedy, not just the diagnosis


def test_percent20_becoming_percent2B_warns_as_a_changed_value():
    # Same corruption, but the request never contained a '+', so the "+ arrived
    # instead of a space" explanation would be a fabrication — report it plainly.
    out = landing_report("https://x.test/s?q=RTX%203090", "https://x.test/s?q=RTX%2B3090")
    assert 'query parameter CHANGED: q — you sent "RTX 3090", the page loaded "RTX+3090"' in out
    assert "RE-ENCODED" not in out


def test_plus_literalized_helper_boundaries():
    assert plus_literalized("RTX+3090", "RTX%2B3090")
    assert plus_literalized("RTX+3090", "RTX%2b3090")     # escape hex is case-insensitive
    assert not plus_literalized("RTX%203090", "RTX%2B3090")   # no '+' was ever sent
    assert not plus_literalized("RTX+3090", "RTX%2B4090")     # a real value change too
    assert not plus_literalized("", "RTX%2B3090")
    assert not plus_literalized("RTX+3090", "")


# ---- benign normalisation: the half that decides whether the warning is read ---------

BENIGN = [
    ("identical", "https://x.test/a?b=1", "https://x.test/a?b=1"),
    ("trailing slash added", "https://x.test/a", "https://x.test/a/"),
    ("trailing slash removed", "https://x.test/a/", "https://x.test/a"),
    ("empty path vs root", "https://x.test", "https://x.test/"),
    ("host case", "https://X.TEST/a", "https://x.test/a"),
    ("scheme case", "HTTPS://x.test/a", "https://x.test/a"),
    ("www added", "https://x.test/a", "https://www.x.test/a"),
    ("www removed", "https://www.x.test/a", "https://x.test/a"),
    ("default https port added", "https://x.test/a", "https://x.test:443/a"),
    ("default http port added", "http://x.test/a", "http://x.test:80/a"),
    ("fqdn root dot", "https://x.test./a", "https://x.test/a"),
    ("fragment dropped", "https://x.test/a#frag", "https://x.test/a"),
    ("fragment added", "https://x.test/a", "https://x.test/a#frag"),
    ("parameter reorder", "https://x.test/a?one=1&two=2", "https://x.test/a?two=2&one=1"),
    ("repeated parameter reorder", "https://x.test/a?t=1&t=2", "https://x.test/a?t=2&t=1"),
    ("plus vs percent20", "https://x.test/s?q=a+b", "https://x.test/s?q=a%20b"),
    ("scheme upgrade", "http://x.test/a", "https://x.test/a"),
    ("unreserved path escape", "https://x.test/%7Euser", "https://x.test/~user"),
    ("escape hex case", "https://x.test/a%2Fb", "https://x.test/a%2fb"),
    ("raw space in path", "https://x.test/a b", "https://x.test/a%20b"),
    ("non-ascii path encoded by the browser",
     "https://pl.wikipedia.org/wiki/Zażółć",
     "https://pl.wikipedia.org/wiki/Za%C5%BC%C3%B3%C5%82%C4%87"),
    ("idn host in punycode", "https://żółć.pl/a", "https://xn--kda4b0koi.pl/a"),
    ("blank value spelling", "https://x.test/a?flag", "https://x.test/a?flag="),
    ("tracking params appended",
     "https://x.test/a?id=7",
     "https://x.test/a?id=7&utm_source=news&gclid=abc&fbclid=def"),
]


@pytest.mark.parametrize("name,requested,landed", BENIGN, ids=[b[0] for b in BENIGN])
def test_benign_normalisation_produces_no_warning(name, requested, landed):
    assert diff_urls(requested, landed).significant() is False, name
    assert landing_report(requested, landed) == "", name


def test_everything_benign_at_once_is_still_silent():
    # The realistic shape of an ordinary navigation: upgraded, www'd, slashed,
    # re-ordered, and decorated with ad tracking.
    assert landing_report(
        "http://example.com/products?colour=red&size=xl",
        "https://www.example.com/products/?size=xl&colour=red&utm_medium=cpc&gclid=z") == ""


# ---- what a real difference reads like ------------------------------------------------

def test_dropped_parameter_is_named_with_its_value():
    out = landing_report("https://x.test/l?a=1&filter=used", "https://x.test/l?a=1")
    assert ('- query parameter DROPPED: filter="used" — a dropped filter changes what '
            "every result on this page means") in out


def test_one_of_several_values_dropped_is_still_reported():
    out = landing_report("https://x.test/l?t=1&t=2", "https://x.test/l?t=1")
    assert '- query parameter DROPPED: t="2"' in out


def test_changed_value_is_named_both_ways():
    out = landing_report("https://x.test/l?sort=price", "https://x.test/l?sort=relevance")
    assert ('- query parameter CHANGED: sort — you sent "price", the page loaded '
            '"relevance"') in out


def test_non_tracking_parameter_added_by_the_site_is_a_quiet_note():
    out = landing_report("https://x.test/l?q=gpu", "https://x.test/l?q=gpu&page=2")
    assert "- query parameter ADDED by the site: page=2" in out


def test_change_lines_are_ordered_dropped_first():
    out = landing_report("https://x.test/l?keep=1&gone=2&enc=a+b",
                         "https://x.test/l?keep=9&enc=a%2Bb&extra=x")
    kinds = [line.split(":")[0].strip("- ") for line in out.split("\n    - ")[1:]]
    assert kinds == ["query parameter DROPPED", "query parameter RE-ENCODED",
                     "query parameter CHANGED", "query parameter ADDED by the site"]


def test_different_path_on_the_same_host():
    out = landing_report("https://x.test/product/123", "https://x.test/search")
    assert "- DIFFERENT PATH: /product/123 -> /search" in out


def test_path_case_change_is_a_real_change():
    # Paths are case-sensitive to the server; folding them would hide a redirect
    # that genuinely served a different document.
    assert diff_urls("https://x.test/Product", "https://x.test/product").path_changed is True


# ---- host / origin wording ------------------------------------------------------------

def test_cross_site_landing_suppresses_path_and_query_lines():
    out = landing_report("https://hardwaredirect.pl/karty-graficzne?q=tesla",
                         "https://en.wikipedia.org/wiki/Nvidia_Tesla")
    assert "- DIFFERENT SITE: hardwaredirect.pl -> en.wikipedia.org" in out
    assert "DIFFERENT PATH" not in out           # a different site always has one
    assert "query parameter" not in out


def test_subdomain_landing_reads_as_same_site():
    out = landing_report("https://allegro.pl/listing?string=gpu",
                         "https://m.allegro.pl/listing?string=gpu")
    assert "- DIFFERENT HOST (same site): allegro.pl -> m.allegro.pl" in out
    assert "DIFFERENT SITE" not in out


def test_multi_label_public_suffix_keeps_the_same_site_wording():
    out = landing_report("https://shop.example.co.uk/a", "https://www.example.co.uk/a")
    assert "- DIFFERENT HOST (same site): shop.example.co.uk -> example.co.uk" in out


def test_port_change_on_an_ip_is_a_different_origin():
    out = landing_report("http://127.0.0.1:5001/a", "http://127.0.0.1:5002/a")
    assert "- DIFFERENT ORIGIN: 127.0.0.1:5001 -> 127.0.0.1:5002" in out


def test_scheme_downgrade_is_flagged():
    out = landing_report("https://x.test/a", "http://x.test/a")
    assert "- SCHEME DOWNGRADED: https -> http" in out


def test_scheme_change_that_is_not_a_downgrade_is_not_called_one():
    out = landing_report("http://x.test/a", "ftp://x.test/a")
    assert "- SCHEME CHANGED: http -> ftp" in out
    assert "DOWNGRADED" not in out


# ---- interstitials --------------------------------------------------------------------

def test_ceneo_captcha_landing_is_reported_as_blocked_not_as_a_url_diff():
    landed = "https://www.ceneo.pl/Captcha/Add?ReturnUrl=%2F%3Bszukaj-rtx"
    out = landing_report("https://www.ceneo.pl/;szukaj-rtx", landed)
    assert out.startswith("\n  BLOCKED — landed on a captcha challenge, not the "
                          "requested document.")
    assert "The requested content was NOT loaded; anything read from this tab describes "\
           "the captcha challenge." in out
    assert "LANDED ELSEWHERE" not in out         # one verdict per landing, not two


def test_consent_host_is_reported_as_a_consent_wall():
    out = landing_report("https://www.google.com/search?q=rtx",
                         "https://consent.google.com/m?continue=https://www.google.com/")
    assert "BLOCKED — landed on a cookie / consent wall" in out


def test_cloudflare_title_alone_is_enough_when_the_url_says_nothing():
    out = landing_report("https://shop.test/listing?q=gpu", "https://shop.test/waiting",
                         title="Just a moment…")
    assert "BLOCKED — landed on a bot / access check" in out


def test_a_bare_403_reports_the_status_but_does_not_claim_the_page_never_loaded():
    """A status-only suspicion is WEAK evidence and must not suppress the change lines.

    Measured live on 2026-08-03: allegro.pl answered HTTP 403 on the main document and then
    rendered 8,202 characters of a genuine listing. Calling that "the requested content was
    NOT loaded" is simply false — and the cost of saying it is that the report SKIPS the
    query diff, which is where the real defect (a silently dropped `stan=uzywane` filter)
    lives. So: report the status, keep describing what changed, and reserve BLOCKED for a
    wall the landed URL or title actually identifies.
    """
    out = landing_report("https://shop.test/listing?q=gpu&stan=used",
                         "https://shop.test/listing?q=gpu", http_status=403)
    assert "BLOCKED" not in out
    assert "main document HTTP 403" in out
    assert 'query parameter DROPPED: stan="used"' in out


def test_a_wall_the_landed_url_identifies_still_blocks_and_suppresses_the_diff():
    """The other side of the same rule: when the landing really IS a captcha, listing its
    query differences is noise — the useful fact is that nothing asked for was served."""
    out = landing_report("https://shop.test/listing?q=gpu&stan=used",
                         "https://shop.test/Captcha/Add?ReturnUrl=%2Flisting",
                         http_status=403)
    assert "BLOCKED — landed on a captcha challenge" in out
    assert "query parameter DROPPED" not in out


def test_an_unchanged_url_never_consults_the_interstitial_markers():
    # A login/consent page the agent asked for by name must not be called a wall.
    assert landing_report("https://x.test/consent", "https://x.test/consent") == ""
    assert landing_report("https://x.test/login", "https://x.test/login",
                          title="Verify you are human") == ""


def classify_interstitial(url, title=None, http_status=None):
    found = interstitial_evidence(url, title, http_status)
    return found[0] if found else None


def test_classify_interstitial_direct():
    assert classify_interstitial("https://x.test/Captcha/Add") == "captcha challenge"
    assert classify_interstitial("https://x.test/age-gate") == "age verification wall"
    assert classify_interstitial("https://x.test/cdn-cgi/challenge-platform/x") == \
        "captcha challenge"
    assert classify_interstitial("https://x.test/p", title="Nie jesteś robotem?") == \
        "captcha challenge"
    assert classify_interstitial("https://x.test/p", http_status=429) == "bot / access check"
    assert classify_interstitial("https://x.test/products/blocked-cables") is not None
    assert classify_interstitial("https://x.test/products", title="RTX 3090") is None
    assert classify_interstitial("https://x.test/p", http_status=404) is None
    assert classify_interstitial(None) is None


# ---- main-document HTTP status --------------------------------------------------------

def test_http_404_at_the_requested_url_reports_only_the_status():
    out = landing_report("https://x.test/offer/9", "https://x.test/offer/9", http_status=404)
    assert out == ("\n  main document HTTP 404 — this is the server's error response; "
                   "a 404/403 body often renders as a normal-looking page.")


@pytest.mark.parametrize("status", [None, 200, 204, 302, 304, 399])
def test_non_error_statuses_print_nothing(status):
    assert landing_report("https://x.test/a", "https://x.test/a", http_status=status) == ""


def test_back_navigation_has_no_requested_url_but_still_reports_the_status():
    # requested=None is how the hook calls this for back/forward/reload.
    out = landing_report(None, "https://x.test/a", op="back", http_status=500)
    assert out == ("\n  main document HTTP 500 — this is the server's error response; "
                   "a 404/403 body often renders as a normal-looking page.")


def test_missing_landed_url_is_not_reported_as_a_difference():
    assert landing_report("https://x.test/a", None) == ""
    assert landing_report("https://x.test/a", "") == ""


# ---- "the navigation never happened" --------------------------------------------------

def test_tab_that_never_left_the_previous_url():
    out = landing_report("https://x.test/target", "https://old.test/where-i-was",
                         url_before="https://old.test/where-i-was")
    assert out == ("\n  NAVIGATION DID NOT TAKE EFFECT — the tab is still on the URL it was "
                   "on before this call."
                   "\n    requested: https://x.test/target"
                   "\n    still on:  https://old.test/where-i-was")
    assert "LANDED ELSEWHERE" not in out         # it did not land elsewhere; it never left


def test_did_not_move_tolerates_benign_normalisation_of_the_previous_url():
    out = landing_report("https://x.test/target", "https://old.test/a/",
                         url_before="http://www.old.test/a")
    assert "NAVIGATION DID NOT TAKE EFFECT" in out


def test_url_before_matching_the_request_does_not_suppress_the_landing_warning():
    # The tab DID move — away from where it was asked to stay.
    out = landing_report("https://x.test/a", "https://other.test/b",
                         url_before="https://x.test/a")
    assert "LANDED ELSEWHERE" in out
    assert "DID NOT TAKE EFFECT" not in out


def test_did_not_move_rule_applies_only_to_goto():
    # For a reload the tab SHOULD still be where it was; that is success, not failure.
    out = landing_report("https://x.test/a", "https://old.test/b", op="reload",
                         url_before="https://old.test/b")
    assert "DID NOT TAKE EFFECT" not in out
    assert "LANDED ELSEWHERE" in out


# ---- clipping -------------------------------------------------------------------------

def test_long_urls_are_clipped_so_the_warning_stays_readable():
    # A different path, not an added parameter: a site-added parameter alone is now a quiet
    # note (T6) with no requested/landed lines, so it cannot carry the clipping check.
    long_url = "https://x.test/b?" + "p=" + ("z" * 900)
    out = landing_report("https://x.test/a", long_url)
    printed = [ln for ln in out.split("\n") if ln.strip().startswith("landed:")][0]
    value = printed.split("landed:", 1)[1].strip()
    assert value.endswith("…")
    assert len(value) == MAX_URL_CHARS + 1
    assert long_url[:80] in out                  # clipped, not replaced


# ---- helper-level pins ----------------------------------------------------------------

def test_normalize_host():
    assert normalize_host("WWW.Example.COM:443", "https") == "example.com"
    assert normalize_host("example.com:80", "http") == "example.com"
    assert normalize_host("example.com:8080", "http") == "example.com:8080"
    assert normalize_host("user:pw@example.com", "https") == "example.com"
    assert normalize_host("example.com.", "https") == "example.com"
    assert normalize_host("[::1]:8772", "http") == "[::1]:8772"


def test_registrable_domain():
    assert registrable_domain("m.allegro.pl") == "allegro.pl"
    assert registrable_domain("shop.example.co.uk") == "example.co.uk"
    assert registrable_domain("example.com") == "example.com"
    assert registrable_domain("127.0.0.1:5001") == "127.0.0.1"
    assert registrable_domain("a.b.c.example.com") == "example.com"


def test_is_ip_literal():
    assert is_ip_literal("127.0.0.1")
    assert is_ip_literal("127.0.0.1:5001")
    assert is_ip_literal("[::1]:8772")
    assert not is_ip_literal("example.com")
    assert not is_ip_literal("1.2.3.4.example.com")


def test_normalize_path():
    assert normalize_path("") == "/"
    assert normalize_path("/") == "/"
    assert normalize_path("/a/") == "/a"
    assert normalize_path("/%7Euser") == "/~user"
    assert normalize_path("/a%2fb") == "/a%2Fb"      # reserved: stays encoded, hex folded
    assert normalize_path("/Case") == "/Case"
    assert normalize_path("/a b") == "/a%20b"
    assert normalize_path("/wiki/Zażółć") == "/wiki/Za%C5%BC%C3%B3%C5%82%C4%87"
    # A '+' in a PATH is a literal plus, so it must NOT converge with %2B the way a
    # query value does — that is the Ceneo /;szukaj-Tesla+V100 case.
    assert normalize_path("/;szukaj-Tesla+V100") != normalize_path("/;szukaj-Tesla%2BV100")


def test_split_query_keeps_raw_and_decoded_in_step():
    decoded, raw = split_query("q=RTX+3090&stan=uzywane&flag")
    assert decoded == [("q", "RTX 3090"), ("stan", "uzywane"), ("flag", "")]
    assert raw == [("q", "RTX+3090"), ("stan", "uzywane"), ("flag", "")]


def test_diff_urls_exposes_the_change_structurally():
    diff = diff_urls(ALLEGRO_REQUESTED, ALLEGRO_LANDED)
    assert diff.significant() is True
    assert diff.host_changed is False and diff.path_changed is False
    assert [(c.key, c.kind) for c in diff.query_changes] == [
        ("stan", "dropped"), ("string", "reencoded")]
    reencoded = diff.query_changes[1]
    assert isinstance(reencoded, QueryChange)
    assert (reencoded.requested, reencoded.landed) == ("RTX 3090", "RTX+3090")
    assert (reencoded.requested_raw, reencoded.landed_raw) == ("RTX+3090", "RTX%2B3090")


def test_diff_urls_on_empty_input_claims_no_difference():
    assert diff_urls(None, "https://x.test/a").significant() is False
    assert diff_urls("https://x.test/a", "").significant() is False
    assert diff_urls(None, None).significant() is False
