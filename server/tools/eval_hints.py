#!/usr/bin/env python3
"""page_eval heuristics: an isolated-world BLIND SPOT, and the caller's own script errors.

Agent-experience feedback, 2026-07-23: an agent walked Vue framework internals
(`el.__vueParentComponent`) with page_eval in the DEFAULT world ("isolated") and
got an empty result, indistinguishable from "there is no Vue here". The isolated
(content-script) world cannot see page-JS expando properties or framework
internals that the page's own scripts set on DOM nodes and globals; only
world="main" (or, on Firefox, `window.wrappedJSObject`) can reach them. This
module turns that silent empty into a DISTINCT, practical signal.

`isolated_world_blindspot_note(code, world, result)` is a pure, stdlib-only
function (regex only) so it is trivially unit-tested and hooks into
server/tools/page.py's page_eval result path in a couple of lines. It returns a
one-paragraph NOTE, or "" when the situation is not that pattern, so plain
DOM code that legitimately returns undefined stays noise-free.

`script_error_text`, `top_level_return_hint` and `arrow_iife_hint` cover the failed call: an
error in the caller's script is named as one, never as an injection failure, and a top-level
`return` or an arrow function called inside its own parentheses gets the mechanical fix.
"""

import re

# Property access that READS a dunder-prefixed key: `.__foo`, `["__foo"]`,
# `[ '__foo' ]`. This is the shape of a framework-internals walk
# (el.__vueParentComponent, node.__reactFiber$xyz, ...). Plain DOM code such as
# document.querySelector('button').click() has no such access and never matches.
_DUNDER_ACCESS_RE = re.compile(r"""[.\[]\s*['"]?__\w+""")

# Known page-world framework markers that may appear WITHOUT dunder-access syntax
# (e.g. `window.$nuxt`). Matched as case-sensitive substrings: these names are
# conventional page globals/keys, so exact spelling is the signal.
_FRAMEWORK_MARKERS = (
    "$nuxt", "__NUXT__", "__NEXT_DATA__",
    "_reactFiber", "__reactFiber", "__reactProps", "__vue",
)

_NOTE = (
    "NOTE: this EMPTY result may mean the value is INVISIBLE FROM THE ISOLATED "
    "WORLD, not that it is absent. The isolated (content-script) world cannot see "
    "page-JS expando properties or framework internals that the page's own scripts "
    "set on DOM nodes or globals (e.g. el.__vueParentComponent, React fiber keys, "
    "$nuxt) — they read as undefined/empty here. Retry with world:\"main\" to run "
    "in the page's own world (on Firefox you can also read them via "
    "window.wrappedJSObject.<name>)."
)


# Agent-experience feedback, 2026-08-03. `document.cookie` NEVER includes HttpOnly
# cookies, which is essentially every cookie that matters for authentication, and the
# result carries no indication that anything was withheld. In the observed incident an
# agent read document.cookie, got three cosmetic cookies back, noticed something was off
# ("usually these include auth_id, sess..."), had no way to resolve the doubt, matched an
# unrelated cookie on a value coincidence, and OVERWROTE a working credential file with
# the three useless values. The task failed and the pre-existing working state was
# destroyed.
#
# This is the same class as the blind-spot note above: an operation that silently drops
# data reporting unqualified success, so it gets the same treatment. Fires on a READ of
# document.cookie in either world (HttpOnly is invisible to page JS too: this is a browser
# rule, not a content-script boundary), and NOT on a write.
# \b so `document.cookies_are_fine` (a page's own property) is not mistaken for the real
# one; the negative lookahead so an assignment `document.cookie = "..."` is a WRITE, which
# withholds nothing and needs no warning.
_COOKIE_READ_RE = re.compile(r"document\s*\.\s*cookie\b(?!\s*=[^=])")

_COOKIE_NOTE = (
    "NOTE: document.cookie CANNOT see HttpOnly cookies, and this result gives no sign that "
    "any were withheld. Session/auth cookies are almost always HttpOnly, so a complete-"
    "looking answer here may be missing exactly the values you are after — do NOT treat "
    "this as the full cookie set, and do not overwrite a stored credential from it. To see "
    "whether the page is actually SENDING an auth cookie, use requests(): start a monitor, "
    "trigger one of the page's own requests, then get its content — the Cookie: header is "
    "reported there (values redacted, but PRESENCE is the fact you need)."
)


def http_only_cookie_note(code: str) -> str:
    """Warn that a document.cookie read silently excludes HttpOnly cookies.

    Independent of world and of the result: the omission is structural, so it applies even
    when the read returned plenty of cookies, that is precisely the case that misled an
    agent into believing it had the whole set.
    """
    if not code or not _COOKIE_READ_RE.search(code):
        return ""
    return _COOKIE_NOTE


# Eval of 2026-10-08: a Firefox extension build without the eval-wrapper fix reported a
# script's own throw inside the retry loop's injection wording ("content script injection
# failed after 4 attempt(s): return not in function. The tab may have been navigating;
# retry; ..."). Seven calls by two models; each blamed the tab and invited a retry of code
# that could never run. The sentence around the inner error is matched exactly, so a real
# injection failure keeps its text.
_INJECTION_WRAPPED_RE = re.compile(
    r"content script injection failed after \d+ attempt\(s\): (?P<inner>.*?)\. The tab may "
    r"have been navigating; retry; if it persists the tab may be a privileged page "
    r"\(page_screenshot / requests work there; page_read / page_eval do not\)\.", re.S)

# Error shapes only a script's own code produces: the error classes, and the SpiderMonkey
# and V8 phrasings of the usual runtime and syntax errors. No injection failure ("No window
# matching", "Frame not found", "Missing host permission", "Invalid frame IDs") has any.
_SCRIPT_ERROR_RE = re.compile(
    r"\b(?:SyntaxError|TypeError|ReferenceError|RangeError|URIError)\b"
    r"|not in function|is not a function|is not defined|is null\b|is undefined\b"
    r"|can't access property|Cannot read propert|Illegal return|Unexpected token"
    r"|unexpected token|missing \) |is not iterable|has no properties|is not a constructor")

_TOP_LEVEL_RETURN_RE = re.compile(r"return not in function|Illegal return statement")


def script_error_text(err: str) -> str:
    """`err` with the injection-failure sentence taken off a script's own error, or `err`
    unchanged. Only the wording of older Firefox builds needs it; current ones report the
    script's error as SCRIPT_ERROR themselves."""
    if not err:
        return err
    m = _INJECTION_WRAPPED_RE.search(err)
    if not m or not _SCRIPT_ERROR_RE.search(m.group("inner")):
        return err
    return err[:m.start()] + m.group("inner") + err[m.end():]


def top_level_return_hint(err: str) -> str:
    """The fix for a top-level `return`, the commonest page_eval syntax error, or "".

    Both engines run the code the same way, as a script through an indirect eval: statements
    are fine, the completion value (the last expression) is the result, and a returned
    promise is awaited. Nothing wraps it in a function, so `return` has nothing to leave."""
    if not err or not _TOP_LEVEL_RETURN_RE.search(err):
        return ""
    lead = " " if err.rstrip().endswith(".") else ". "
    return (lead + "page_eval returns the value of the code's last expression, so a top-level "
            "return is a syntax error: drop it (document.title, not return document.title), "
            "or wrap the code as (() => { ...; return value; })(), with the () after the "
            "closing parenthesis")


# An arrow function wrapped and called the way a function expression is: `(() => { ... }())`.
# That is a syntax error (an arrow cannot be called inside its own parentheses); the working
# shapes are `(() => { ... })()` and `(function () { ... }())`. The error alone does not say
# which of the two the caller meant, so the code is matched too.
_ARROW_PAREN_ERROR_RE = re.compile(r"missing \) in parenthetical|Unexpected token", re.I)
_ARROW_PAREN_CALL_RE = re.compile(
    r"^\s*\(\s*(?:async\s+)?(?:\([^()]*\)|[A-Za-z_$][\w$]*)\s*=>\s*\{.*\}\s*\([^()]*\)\s*\)\s*;?\s*$",
    re.S)


def arrow_iife_hint(err: str, code: str) -> str:
    """The right shape for an arrow function called inside its own parentheses, or ""."""
    if not err or not code or not _ARROW_PAREN_ERROR_RE.search(err):
        return ""
    if not _ARROW_PAREN_CALL_RE.match(code):
        return ""
    lead = " " if err.rstrip().endswith(".") else ". "
    return (lead + "An arrow function cannot be called inside its own parentheses: write "
            "(() => { ... })() with the call after the closing parenthesis, not "
            "(() => { ... }())")


def _looks_empty(result) -> bool:
    """Empty as the note defines it: None, [] or {} (NOT an empty string / 0 / False,
    which are legitimate scalar returns and must not trip the note)."""
    return result is None or result == [] or result == {}


def _reads_page_world_internal(code: str) -> bool:
    if _DUNDER_ACCESS_RE.search(code):
        return True
    return any(marker in code for marker in _FRAMEWORK_MARKERS)


def isolated_world_blindspot_note(code: str, world: str, result) -> str:
    """Return a one-paragraph NOTE (or "") warning that an empty isolated-world
    result is likely an isolated-world blind spot rather than a real absence.

    Fires ONLY when ALL of these hold:
      * world == "isolated" (the main world already sees page state);
      * the author has not already reached across via `wrappedJSObject`;
      * the code textually reads a dunder property (`.__foo` / `["__foo"]`) OR
        references a known framework page-global marker ($nuxt, __NUXT__, ...);
      * the result is empty (None / [] / {}).

    Plain DOM code that returns undefined (e.g. an element `.click()`) never
    matches, so the note is signal, not noise.
    """
    if world != "isolated":
        return ""
    if not code or "wrappedJSObject" in code:
        return ""
    if not _looks_empty(result):
        return ""
    if not _reads_page_world_internal(code):
        return ""
    return _NOTE
