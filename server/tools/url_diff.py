#!/usr/bin/env python3
"""Requested-vs-landed URL honesty for `navigate`.

Nothing on the server ever compared the URL the agent ASKED for with the URL the
browser actually LOADED, so three different failures all rendered as plain success:
a captcha/consent interstitial, a main document served 4xx, and (the one that
produced a wrong answer delivered to a user) a silently rewritten query string.

The live repro (Zen Browser, 2026-08-03):

    requested  https://allegro.pl/listing?string=RTX+3090&order=pd&stan=uzywane
    landed     https://allegro.pl/listing?string=RTX%2B3090&order=pd

Two independent degradations, neither flagged: the `+` (a SPACE in a query string)
came back as a literal `%2B`, so the site searched for the string "RTX+3090"; and
`stan=uzywane`, the used-condition filter, was dropped entirely. The agent read
15 offers off that page and priced them as if they were the requested filter set.

This module is the whole comparison: pure, stdlib-only, no I/O and no browser, so
it is trivially unit-pinned and hooks into `server/tools/navigate.py`'s navigate result
path in a couple of lines (`landing_report(...)`, which returns text already
indented for that renderer, or "" when there is nothing worth saying).

STAYING SILENT IS HALF THE FEATURE. A warning printed on ordinary navigations is
worse than no warning at all: an agent learns to skip it and the rewritten-query
case goes back to being invisible. So benign normalisation is classified and
swallowed: scheme upgrade, `www.`, trailing slash, default ports, host case,
fragments, parameter REORDER, tracking parameters the site appended, and (the
subtle one) equivalent percent-encoding of the SAME value. In a query string `+`
MEANS space, so "RTX+3090" and "RTX%203090" are the same value and say nothing,
while "RTX%2B3090" is a different value and must be named.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import SplitResult, parse_qsl, quote, urlsplit

from server.tools.url_norm import ascii_host, resolve_dot_segments

# Parameters a site/ad-network appends on its own. Their arrival says nothing about
# whether the agent got the document it asked for, so an ADDED note for them is pure
# noise. Deliberately biased toward silence: `ref`/`source`/`referrer` are sometimes
# semantic, but ADDED is only ever a quiet note, so suppressing it costs nothing while
# a false alarm costs the whole feature's credibility.
TRACKING_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "utm_id",
    "utm_source_platform", "gclid", "gbraid", "wbraid", "fbclid", "msclkid", "dclid",
    "yclid", "ttclid", "twclid", "igshid", "mc_cid", "mc_eid", "_ga", "_gl", "ref", "ref_src",
    "ref_url", "referrer", "source", "spm", "scm", "epik", "li_fat_id", "vero_id", "s_kwcid",
    "ef_id", "icid", "trk", "_openstat", "rb_clickid", "at_medium", "at_campaign",
})

# The subset of TRACKING_PARAMS whose DISAPPEARANCE or rewrite is also silent: click ids
# and campaign tags that sites strip on purpose (canonicalisation, privacy redirects).
# `ref` / `source` / `referrer` are left out: they are sometimes real filters, so losing
# one is still reported even though gaining one is not.
_AMBIGUOUS_TRACKING = frozenset({"ref", "source", "referrer", "ref_url", "ref_src"})
QUIET_WHEN_LOST = TRACKING_PARAMS - _AMBIGUOUS_TRACKING

# Path / hash-route segments that mean "an authentication page". Whole-segment match, so
# /blog/login-tips is not one.
AUTH_SEGMENTS = frozenset({
    "login", "log-in", "logon", "signin", "sign-in", "sign_in", "signon", "sso", "auth",
    "oauth", "oauth2", "authorize", "authenticate", "wp-login.php", "logowanie", "zaloguj",
})

# Multi-label public suffixes, enough to keep "different SITE" vs "different SUBDOMAIN"
# wording honest on the domains this project actually meets. Being wrong here only
# changes the WORDING; a host difference always warns either way.
MULTI_LABEL_SUFFIXES = frozenset({
    "co.uk", "org.uk", "gov.uk", "ac.uk", "com.pl", "net.pl", "org.pl", "com.au", "co.jp",
    "com.br", "com.tr", "co.in", "co.nz", "co.za", "com.mx", "com.ar",
})

# (lowercased path substring, label). First match wins, in this order. These are the
# landing pages that render as a perfectly normal document while containing none of the
# requested content; the Ceneo repro landed on /Captcha/Add?ReturnUrl=... and was
# reported as a clean success.
INTERSTITIAL_PATH_MARKERS = (
    ("/captcha",              "captcha challenge"),
    ("/distil_r_captcha",     "captcha challenge"),
    ("_incapsula_resource",   "captcha challenge"),
    ("/cdn-cgi/challenge",    "captcha challenge"),
    ("/challenge-platform",   "captcha challenge"),
    ("/sorry/index",          "captcha challenge"),
    ("/consent",              "cookie / consent wall"),
    ("/cookie-consent",       "cookie / consent wall"),
    ("/gdpr",                 "cookie / consent wall"),
    ("/age-verification",     "age verification wall"),
    ("/age-gate",             "age verification wall"),
    ("/agegate",              "age verification wall"),
    ("/access-denied",        "bot / access check"),
    ("/accessdenied",         "bot / access check"),
    ("/blocked",              "bot / access check"),
    ("/security-check",       "bot / access check"),
    ("/checkpoint",           "bot / access check"),
)
INTERSTITIAL_HOST_MARKERS = (
    ("consent.google.",   "cookie / consent wall"),
    ("consent.youtube.",  "cookie / consent wall"),
    ("guce.yahoo.",       "cookie / consent wall"),
)
# Lowercased <title> substrings, accent-folded before matching. SECONDARY: consulted
# only when host and path gave nothing, because a title is the weakest evidence here.
INTERSTITIAL_TITLE_MARKERS = (
    ("just a moment",           "bot / access check"),      # Cloudflare
    ("attention required",      "bot / access check"),      # Cloudflare
    ("pardon our interruption", "bot / access check"),      # Imperva/Distil
    ("one more step",           "bot / access check"),
    ("verify you are human",    "captcha challenge"),
    ("are you a robot",         "captcha challenge"),
    ("captcha",                 "captcha challenge"),
    ("access denied",           "bot / access check"),
    ("nie jestes robotem",      "captcha challenge"),       # PL, accent-folded
)

# Printed URLs are clipped like _digest_suffix clips its own: a 2 KB tracking URL in a
# warning buries the warning.
MAX_URL_CHARS = 300

_ESCAPE_RE = re.compile(r"%([0-9a-fA-F]{2})")
_IPV4_RE = re.compile(r"\d{1,3}(?:\.\d{1,3}){3}")
# RFC 3986 unreserved: decoding these in a path can never change what the server matches.
_UNRESERVED = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
# Characters a path may carry raw (pchar + "/"), plus "%" so existing escapes are not
# double-encoded. Everything else, a raw space, a Polish letter, "[", is what the
# browser itself percent-encodes on the way out, so both sides must be encoded before
# they can be compared. "+" is deliberately SAFE: in a path (unlike a query) it is a
# literal plus, and "+" vs "%2B" there is a real difference, not a re-encoding.
_PATH_SAFE = "/%:@!$&'()*+,;=~"

# Sort order for the rendered change lines. DROPPED first: a lost filter is the change
# that silently invalidates every conclusion drawn from the page.
_KIND_RANK = {"dropped": 0, "reencoded": 1, "changed": 2, "added": 3}


@dataclass(frozen=True)
class QueryChange:
    """One query-parameter difference. Values are carried BOTH decoded (what the server
    actually received) and raw (still percent-encoded), because the encoding IS the bug
    in the rewritten-query case and the decoded form alone cannot show it."""
    key: str
    kind: str                       # "dropped" | "added" | "reencoded" | "changed"
    requested: Optional[str]        # decoded
    landed: Optional[str]           # decoded
    requested_raw: Optional[str]
    landed_raw: Optional[str]


@dataclass(frozen=True)
class UrlDiff:
    """The classified difference between a requested and a landed URL.

    Every flag is already benign-filtered: `path_changed` is False for a trailing
    slash, `host_changed` is False for `www.`/case/default port, and `query_changes`
    never contains a re-ordering or an equivalent re-encoding. So `significant()` is
    exactly "is there something the agent needs to know".
    """
    requested: str
    landed: str
    requested_scheme: str
    landed_scheme: str
    requested_host: str             # normalized, including any non-default port
    landed_host: str
    requested_path: str             # normalized
    landed_path: str
    scheme_downgraded: bool         # scheme changed other than the benign http -> https
    host_changed: bool
    site_changed: bool              # different registrable domain, port, or IP
    is_origin_pair: bool            # either host is an IP literal -> "DIFFERENT ORIGIN"
    path_changed: bool
    query_changes: Tuple[QueryChange, ...]
    requested_route: Optional[str] = None   # client-side route in the fragment ("#/orders")
    landed_route: Optional[str] = None
    route_changed: bool = False
    auth_landing: bool = False              # landed on a login/auth path or route the request was not

    def significant(self) -> bool:
        """True when the landing differs in any reportable way, quiet notes included."""
        return bool(self.loud() or self.query_changes)

    def loud(self) -> bool:
        """True when the landing differs in a way that can change an answer. A parameter
        the site merely ADDED (page=1) is a note, not a different page."""
        return bool(self.scheme_downgraded or self.host_changed or self.path_changed
                    or self.route_changed
                    or any(c.kind != "added" for c in self.query_changes))


# ---- URL part normalisation ----------------------------------------------------------

def _upper_escapes(text: str) -> str:
    """`%2b` -> `%2B`. Percent-escape hex is case-insensitive, so comparing raw values
    without folding it would report a re-encoding that never happened."""
    return _ESCAPE_RE.sub(lambda m: "%" + m.group(1).upper(), text or "")


def _strip_port(host: str) -> str:
    if host.startswith("["):                      # bracketed IPv6 literal
        end = host.find("]")
        return host[:end + 1] if end != -1 else host
    return host.partition(":")[0]


def _port_of(host: str) -> str:
    if host.startswith("["):
        end = host.find("]")
        rest = host[end + 1:] if end != -1 else ""
        return rest[1:] if rest.startswith(":") else ""
    return host.partition(":")[2]


def is_ip_literal(host: str) -> bool:
    """True for a bare address literal (dotted IPv4 or bracketed IPv6), with or without
    a port. An IP host has no registrable domain, so two different IPs are never the
    same "site": that is what picks the DIFFERENT ORIGIN wording over DIFFERENT SITE."""
    h = _strip_port(host or "")
    if h.startswith("[") and h.endswith("]"):
        return True
    return bool(_IPV4_RE.fullmatch(h))


def registrable_domain(host: str) -> str:
    """The last two labels of `host`, three when the last two form a known multi-label
    public suffix (`example.co.uk`). An IP literal is returned whole."""
    h = _strip_port(host or "")
    if is_ip_literal(h):
        return h
    labels = h.split(".")
    if len(labels) <= 2:
        return h
    if ".".join(labels[-2:]) in MULTI_LABEL_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def normalize_host(netloc: str, scheme: str) -> str:
    """Host identity: lowercased, userinfo and the FQDN root dot dropped, `www.` dropped,
    the scheme's default port dropped. Everything removed here is a difference no server
    can observe: a redirect that only adds `www.` moved nothing."""
    host = (netloc or "").strip().lower()
    if "@" in host:
        host = host.rpartition("@")[2]
    hostpart, port = _strip_port(host), _port_of(host)
    if hostpart.endswith("."):
        hostpart = hostpart[:-1]
    # An IDN host is reported back by the browser in punycode; comparing the two
    # spellings raw would call `żółć.pl` and `xn--kda4b0koi.pl` different sites.
    hostpart = ascii_host(hostpart)
    if hostpart.startswith("www."):
        hostpart = hostpart[4:]
    if (scheme == "http" and port == "80") or (scheme == "https" and port == "443"):
        port = ""
    return f"{hostpart}:{port}" if port else hostpart


def normalize_path(path: str) -> str:
    """Path identity: decode ONLY unreserved escapes, percent-encode whatever a path may
    not carry raw, uppercase the hex of the rest, drop one trailing slash, empty -> "/".

    The encode half matters as much as the decode half: an agent may well write
    `/wiki/Zażółć` or `/a b`, and the browser will report back
    `/wiki/Za%C5%BC%C3%B3%C5%82%C4%87` / `/a%20b`. Those are the SAME path, and calling
    them a different one would be exactly the false alarm this module exists to avoid.

    Case is deliberately NOT folded: paths are case-sensitive to the server, and folding
    them would hide a real change. `%2F` stays encoded for the same reason: decoding it
    would merge a path segment with a separator.
    """
    def _decode(match: "re.Match[str]") -> str:
        ch = chr(int(match.group(1), 16))
        return ch if ch in _UNRESERVED else "%" + match.group(1).upper()

    # Dot segments go AFTER the unreserved decode (so `%2e%2e` is seen as `..`) and BEFORE
    # the encode: a browser resolves them before it sends the request, so `/a/../b`
    # reported back as `/b` is the same path, not a redirect.
    out = quote(resolve_dot_segments(_ESCAPE_RE.sub(_decode, path or "")), safe=_PATH_SAFE)
    if len(out) > 1 and out.endswith("/"):
        out = out[:-1]
    return out or "/"


# ---- query strings -------------------------------------------------------------------

def split_query(query: str) -> Tuple[List[Tuple[str, str]], List[Tuple[str, str]]]:
    """(decoded_pairs, raw_pairs), same order, same length.

    Decoded via `parse_qsl`, which turns `+` into a space; that is the whole point: it
    is what the SERVER sees, so it is the only fair basis for "did the value change".
    The raw pairs are kept alongside so the report can show HOW an unchanged-looking
    value was mangled (`RTX+3090` -> `RTX%2B3090`).
    """
    decoded = parse_qsl(query or "", keep_blank_values=True)
    raw: List[Tuple[str, str]] = []
    for pair in (query or "").split("&"):
        if not pair:                               # parse_qsl skips empty fields too
            continue
        key, _, value = pair.partition("=")
        raw.append((key, value))
    return decoded, raw


def _query_map(query: str) -> Dict[str, List[Tuple[str, str]]]:
    """key -> [(decoded value, raw value), ...] in document order. Repeated keys keep all
    their values, so `?a=1&a=2` is not silently collapsed."""
    decoded, raw = split_query(query)
    out: Dict[str, List[Tuple[str, str]]] = {}
    for i, (key, value) in enumerate(decoded):
        raw_value = raw[i][1] if i < len(raw) else value
        out.setdefault(key, []).append((value, raw_value))
    return out


def plus_literalized(requested_raw: str, landed_raw: str) -> bool:
    """True when the landed raw value is the requested one with every `+` replaced by a
    literal-plus escape, i.e. the exact allegro rewrite, `RTX+3090` -> `RTX%2B3090`.

    Worth its own kind because the remedy is specific and non-obvious: the space must be
    sent as `%20`, since the site's own round-trip will not preserve a `+`.
    """
    if not requested_raw or not landed_raw or "+" not in requested_raw:
        return False
    return _upper_escapes(requested_raw.replace("+", "%2B")) == _upper_escapes(landed_raw)


def _multiset_only(values: List[Tuple[str, str]],
                   other: List[Tuple[str, str]]) -> List[Tuple[str, str]]:
    """The entries of `values` with no counterpart in `other`, comparing DECODED values
    and counting duplicates. Matching as a multiset is what makes a re-ordered repeated
    key benign while a value that is really lost still surfaces."""
    remaining = Counter(value for value, _ in other)
    out: List[Tuple[str, str]] = []
    for value, raw in values:
        if remaining[value] > 0:
            remaining[value] -= 1
        else:
            out.append((value, raw))
    return out


def _query_changes(requested_query: str, landed_query: str) -> Tuple[QueryChange, ...]:
    requested = _query_map(requested_query)
    landed = _query_map(landed_query)
    changes: List[QueryChange] = []

    for key, req_values in requested.items():
        if key.lower() in QUIET_WHEN_LOST:
            continue                               # a site stripping/rewriting a tracker
        if key not in landed:
            for value, raw in req_values:
                changes.append(QueryChange(key, "dropped", value, None, raw, None))
            continue
        req_only = _multiset_only(req_values, landed[key])
        landed_only = _multiset_only(landed[key], req_values)
        if not req_only and not landed_only:
            continue                               # same values, any order; nothing to say
        for i in range(max(len(req_only), len(landed_only))):
            rq = req_only[i] if i < len(req_only) else None
            ld = landed_only[i] if i < len(landed_only) else None
            if ld is None:
                changes.append(QueryChange(key, "dropped", rq[0], None, rq[1], None))
            elif rq is None:
                if key.lower() not in TRACKING_PARAMS:
                    changes.append(QueryChange(key, "added", None, ld[0], None, ld[1]))
            else:
                kind = "reencoded" if plus_literalized(rq[1], ld[1]) else "changed"
                changes.append(QueryChange(key, kind, rq[0], ld[0], rq[1], ld[1]))

    for key, landed_values in landed.items():
        if key in requested or key.lower() in TRACKING_PARAMS:
            continue
        for value, raw in landed_values:
            changes.append(QueryChange(key, "added", None, value, None, raw))

    changes.sort(key=lambda c: (_KIND_RANK.get(c.kind, 9), c.key))
    return tuple(changes)


def hash_route(fragment: str) -> Optional[str]:
    """The client-side route in a fragment (`#/orders`, `#!/orders`), or None for a plain
    anchor (`#intro`). Single-page apps keep their whole "path" here, and the server
    never sees it, so it needs its own comparison."""
    frag = fragment or ""
    if frag.startswith("!"):
        frag = frag[1:]
    if not frag.startswith("/"):
        return None
    route = _upper_escapes(frag)
    return route[:-1] if len(route) > 1 and route.endswith("/") else route


def is_auth_location(path: str) -> bool:
    """True when a path (or hash route) has a whole segment naming an authentication page."""
    route = (path or "").split("?", 1)[0].lower()
    return any(seg in AUTH_SEGMENTS for seg in route.split("/"))


_SCHEME_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:(?!\d+(?:/|$))")


def _safe_urlsplit(text: str):
    """urlsplit, but a string it refuses (`[` or `http://[::1`: an unbalanced IPv6 bracket)
    is read as an opaque path instead of raising, so a malformed URL is still compared."""
    try:
        return urlsplit(text)
    except ValueError:
        return SplitResult("", "", text, "", "")


def _split(url: Optional[str]):
    """urlsplit, except that a scheme-less `example.org/docs` (what an agent often types,
    and what the browser completes to https) is read as a host, not as a path."""
    text = (url or "").strip()
    if text and "://" not in text and not text.startswith(("/", "#", "?")) \
            and not _SCHEME_RE.match(text):
        return _safe_urlsplit("//" + text)
    return _safe_urlsplit(text)


# ---- the comparison ------------------------------------------------------------------

def diff_urls(requested: Optional[str], landed: Optional[str]) -> UrlDiff:
    """Classify how `landed` differs from `requested`. Fragments are never compared:
    they are not sent to the server and cannot change what the page contains."""
    a, b = _split(requested), _split(landed)
    a_scheme, b_scheme = (a.scheme or "").lower(), (b.scheme or "").lower()
    a_host = normalize_host(a.netloc, a_scheme)
    b_host = normalize_host(b.netloc, b_scheme)
    a_path, b_path = normalize_path(a.path), normalize_path(b.path)

    if not requested or not landed:
        # Nothing to compare against: report no difference rather than inventing one from
        # an empty string. The caller decides whether a missing URL is itself a problem.
        return UrlDiff(requested=requested or "", landed=landed or "",
                       requested_scheme=a_scheme, landed_scheme=b_scheme,
                       requested_host=a_host, landed_host=b_host,
                       requested_path=a_path, landed_path=b_path,
                       scheme_downgraded=False, host_changed=False, site_changed=False,
                       is_origin_pair=False, path_changed=False, query_changes=())

    host_changed = a_host != b_host
    # http -> https is an upgrade every site performs; anything else is a real change.
    scheme_downgraded = (a_scheme != b_scheme and a_scheme != ""      # "" = agent typed none
                         and not (a_scheme == "http" and b_scheme == "https"))
    is_origin_pair = host_changed and (is_ip_literal(a_host) or is_ip_literal(b_host))
    site_changed = host_changed and (is_origin_pair
                                     or _port_of(a_host) != _port_of(b_host)
                                     or registrable_domain(a_host) != registrable_domain(b_host))

    a_route, b_route = hash_route(a.fragment), hash_route(b.fragment)
    # A route the request had must survive; a route it did not have is only news when it
    # is a login page (an app landing on its own #/home is ordinary).
    route_changed = (a_route is not None and a_route != b_route) or \
                    (a_route is None and b_route is not None and is_auth_location(b_route))
    a_auth = is_auth_location(a_path) or is_auth_location(a_route or "")
    b_auth = is_auth_location(b_path) or is_auth_location(b_route or "")
    return UrlDiff(requested=requested, landed=landed,
                   requested_scheme=a_scheme, landed_scheme=b_scheme,
                   requested_host=a_host, landed_host=b_host,
                   requested_path=a_path, landed_path=b_path,
                   scheme_downgraded=scheme_downgraded, host_changed=host_changed,
                   site_changed=site_changed, is_origin_pair=is_origin_pair,
                   path_changed=a_path != b_path,
                   query_changes=_query_changes(a.query, b.query),
                   requested_route=a_route, landed_route=b_route,
                   route_changed=route_changed, auth_landing=b_auth and not a_auth)


def state_fragment_changed(requested: Optional[str], landed: Optional[str]) -> bool:
    """True when either fragment carries app state (`#tab=billing`) and they differ.

    A plain anchor (`#reviews`) is a scroll position and a hash route (`#/orders`) has its
    own comparison (`route_changed`), so neither counts here. A key=value fragment is the
    third shape: single-page apps keep view state in it, and losing or rewriting it means
    the page shows something else.
    """
    def state(url):
        frag = _split(url).fragment
        return _upper_escapes(frag) if "=" in frag and hash_route(frag) is None else None

    a, b = state(requested), state(landed)
    return (a is not None or b is not None) and a != b


def landing_wall(requested: Optional[str], landed: Optional[str]) -> Optional[str]:
    """Why a landing on the SAME site is still not the page asked for, or None.

    The read guard's path-level rule: a login/auth path or route the request was not
    aimed at, a client-side route that changed, or a captcha / consent / access wall in
    the landed URL. Plain path changes (a slug appended, a canonical redirect) are NOT
    walls: they cost a warning in `landing_report`, but the page is still the page.
    """
    if not requested or not landed:
        return None
    diff = diff_urls(requested, landed)
    if diff.auth_landing:
        return "a login / sign-in page"
    if diff.route_changed:
        return (f"the in-page route {diff.landed_route or '(none)'}, not "
                f"{diff.requested_route or '(none)'}")
    if diff.path_changed:
        found = interstitial_evidence(landed)
        if found and found[1] == "url" and not interstitial_evidence(requested):
            return f"a {found[0]}"
    return None


def interstitial_evidence(url: Optional[str], title: Optional[str] = None,
                          http_status: Optional[int] = None):
    """(label, source) for a suspected wall, or None. source is "url" | "title" | "status".

    The SOURCE matters, not just the label. A wall identified from the landed URL or the
    document title is strong evidence that the requested document was never served, and the
    caller can say so. A bare 403/429 is much weaker: the URL still looks like the resource
    that was asked for, and plenty of sites answer 403 on a first hit and then render the
    real page anyway. The live allegro case is exactly that: HTTP 403 on the main document,
    and 8,202 characters of genuine listing on the page. Reporting THAT as "the requested
    content was NOT loaded" would be false, and worse, it would suppress the query diff that
    is the actual finding (a dropped `stan=uzywane` filter). So callers treat a status-only
    signal as an annotation, never as a reason to stop describing what else changed.
    """
    parts = _safe_urlsplit(url or "")
    host = (parts.netloc or "").lower()
    path = (parts.path or "").lower()
    for marker, label in INTERSTITIAL_HOST_MARKERS:
        if marker in host:
            return label, "url"
    for marker, label in INTERSTITIAL_PATH_MARKERS:
        if marker in path:
            return label, "url"
    folded = _fold(title)
    if folded:
        for marker, label in INTERSTITIAL_TITLE_MARKERS:
            if marker in folded:
                return label, "title"
    if http_status in (403, 429):
        return "bot / access check", "status"
    return None


def _fold(text: Optional[str]) -> str:
    """Lowercase and strip accents, so a Polish "Nie jesteś robotem" matches an ASCII
    marker without carrying a per-locale spelling table."""
    if not text:
        return ""
    stripped = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore")
    return stripped.decode("ascii").lower()


# ---- rendering -----------------------------------------------------------------------

def _clip(value: Optional[str], limit: int = MAX_URL_CHARS) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[:limit] + "…"


def _across(redirects: Optional[Sequence[Dict[str, Any]]]) -> str:
    """" (changed across a 308 redirect)": naming the hop tells the agent the rewrite
    was the SITE's doing, not a typo in its own request."""
    if not redirects:
        return ""
    first = redirects[0]
    status = first.get("status") if isinstance(first, dict) else None
    return f" (changed across a {status} redirect)" if status else ""


def _change_lines(diff: UrlDiff) -> List[str]:
    lines: List[str] = []
    # A landing on a non-web scheme (about:, data:, chrome:, view-source:) has no host to
    # compare, so the host/scheme lines would read as nonsense: "DIFFERENT SITE: shop.test
    # -> " and "SCHEME DOWNGRADED: https -> about". Say the one true thing instead. This is
    # normally pre-empted by the navigate result's own aboutBlank flag, but the wording has
    # to be right whenever it is not.
    if not diff.landed_host and diff.landed_scheme not in ("http", "https", ""):
        return [f"the tab is on {diff.landed_scheme}: — not a web document at all, so "
                f"nothing was loaded from {diff.requested_host or 'the requested URL'}"]
    if diff.host_changed:
        if diff.is_origin_pair:
            lines.append(f"DIFFERENT ORIGIN: {diff.requested_host} -> {diff.landed_host}")
        elif diff.site_changed:
            lines.append(f"DIFFERENT SITE: {diff.requested_host} -> {diff.landed_host}")
        else:
            lines.append(f"DIFFERENT HOST (same site): {diff.requested_host} -> {diff.landed_host}")
    if diff.scheme_downgraded:
        if diff.requested_scheme == "https":
            lines.append(f"SCHEME DOWNGRADED: {diff.requested_scheme} -> {diff.landed_scheme}")
        else:
            lines.append(f"SCHEME CHANGED: {diff.requested_scheme} -> {diff.landed_scheme}")
    if diff.host_changed:
        # A different site has a different path and different parameters by definition;
        # listing them buries the one fact that matters.
        return lines
    if diff.path_changed:
        lines.append(f"DIFFERENT PATH: {diff.requested_path} -> {diff.landed_path}")
    if diff.route_changed:
        lines.append(f"DIFFERENT HASH ROUTE: #{diff.requested_route or '(none)'} -> "
                     f"#{diff.landed_route or '(none)'} (client-side; the server never saw it)")
    if diff.auth_landing:
        lines.append("the landed address looks like a login / sign-in page")
    for change in diff.query_changes:
        if change.kind == "dropped":
            lines.append(f'query parameter DROPPED: {change.key}="{change.requested}" — a '
                         f"dropped filter changes what every result on this page means")
        elif change.kind == "reencoded":
            lines.append(f'query parameter RE-ENCODED: {change.key} — you sent '
                         f'"{change.requested}", the page loaded "{change.landed}" (a literal '
                         f"+ arrived instead of a space; send %20 for a space)")
        elif change.kind == "changed":
            lines.append(f'query parameter CHANGED: {change.key} — you sent '
                         f'"{change.requested}", the page loaded "{change.landed}"')
        else:
            lines.append(f"query parameter ADDED by the site: {change.key}={change.landed}")
    return lines


def landing_report(requested: Optional[str],
                   landed: Optional[str],
                   *,
                   title: Optional[str] = None,
                   http_status: Optional[int] = None,
                   url_before: Optional[str] = None,
                   redirects: Optional[Sequence[Dict[str, Any]]] = None,
                   op: str = "goto") -> str:
    """The navigate landing warning, or "" when the landing was honest.

    Returns text already indented for navigate's renderer (every line starts "\\n  " or
    "\\n    "), so the hook is `line += landing_report(...)`.

    requested: the URL the agent asked for; None for back/forward/reload, where there is
        no requested URL and only the HTTP-status section can apply.
    landed:    the post-settle tab URL.
    title:     post-settle document title (weak interstitial evidence).
    http_status: the MAIN DOCUMENT's HTTP status, when the extension observed one.
        ABSENT is not 200: bfcache, data:/about: URLs and a settle that outran
        onCompleted all legitimately have none, so nothing is printed for them.
    url_before: the tab's URL before the navigation was issued; lets "the navigation
        never took effect" be distinguished from "arrived successfully".
    redirects: the hop chain, only read for the first hop's status.
    """
    out: List[str] = []
    diff = diff_urls(requested, landed) if (requested and landed) else None

    if diff is not None and diff.loud():
        if op == "goto" and url_before and not diff_urls(url_before, landed).significant():
            # The tab is exactly where it started: whatever happened, it was not this
            # navigation. Saying "landed elsewhere" here would misdirect the retry.
            out.append("\n  NAVIGATION DID NOT TAKE EFFECT — the tab is still on the URL it "
                       "was on before this call.")
            out.append(f"\n    requested: {_clip(requested)}")
            out.append(f"\n    still on:  {_clip(url_before)}")
        else:
            found = interstitial_evidence(landed, title, http_status)
            # A status-only suspicion does NOT get to claim the document was never served,
            # and must not suppress the change lines below; see interstitial_evidence.
            # The bare status is still reported, by section D.
            label = found[0] if (found and found[1] != "status") else None
            if label:
                out.append(f"\n  BLOCKED — landed on a {label}, not the requested document.")
                out.append(f"\n    requested: {_clip(requested)}")
                out.append(f"\n    landed:    {_clip(landed)}")
                out.append(f"\n    The requested content was NOT loaded; anything read from "
                           f"this tab describes the {label}.")
            else:
                out.append(f"\n  LANDED ELSEWHERE — the loaded URL is not the one "
                           f"requested{_across(redirects)}.")
                out.append(f"\n    requested: {_clip(requested)}")
                out.append(f"\n    landed:    {_clip(landed)}")
                for line in _change_lines(diff):
                    out.append(f"\n    - {line}")
                out.append("\n    Answers read from this page describe the LOADED url; "
                           "re-issue with the exact url if the difference matters.")

    elif diff is not None and diff.query_changes:
        # Only parameters the site ADDED (page=1): the same page, so a note and no header.
        out.append("\n  note: the loaded URL is the requested one plus parameters the site added.")
        for line in _change_lines(diff):
            out.append(f"\n    - {line}")

    # Independent of everything above: the URL can match exactly and still be an error
    # page, and back/forward/reload (requested=None) needs this just as much.
    if isinstance(http_status, int) and not isinstance(http_status, bool) and http_status >= 400:
        out.append(f"\n  main document HTTP {http_status} — this is the server's error "
                   f"response; a 404/403 body often renders as a normal-looking page.")
    return "".join(out)
