"""Display-safe forms of MCP tool arguments and of URLs.

Tool arguments are shown on /health, in the connection map and in every connected
browser's popup (a remote phone included). Typed text, eval code and query strings are
routinely secrets, so the recording point (call_telemetry.format_args) keeps only what
explains a call's shape: tool and action names, tab ids, flags, counts, character lengths
and the host part of a URL. A value is never masked by looking at its content, because a
secret is unrecognizable on its own; every string not on the short enum list below is
replaced by its length.

Pure stdlib, no state. Redacting an already redacted value returns it unchanged, so a
record that is redacted at recording time can pass through the report builders again.
"""

import re
from urllib.parse import urlsplit

#: String arguments that name a mode or an id, never user content. Everything else that is
#: a string becomes "<text: N chars>".
ENUM_KEYS = frozenset({
    "action", "mode", "read", "format", "wait", "world", "state", "read_scope_kind",
    "target_kind", "type", "kind", "monitor_id", "request_id", "script_name",
})
#: Keys whose string is a URL (or a proxy address): kept as scheme://host[:port]/path.
URL_KEYS = frozenset({"url", "proxy"})
#: Numbers and booleans under these keys are content, not shape.
CONTENT_KEYS = frozenset({"value", "text", "code", "script", "script_args", "password",
                          "filter", "selector", "root", "scope", "item_selector"})
#: Keys of a list element that name its kind, in order of preference.
_KIND_KEYS = ("action", "type", "kind")

#: Longest path shown; a long path is more likely an embedded token than a page name.
_PATH_LIMIT = 80
_ENUM_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,40}$")
# The body is long enough for the longest placeholder redact_value builds (20 dict keys of 30
# characters, or 20 item kinds of 40), so redacting a redacted value returns it unchanged.
_PLACEHOLDER_RE = re.compile(r"^<(?:[a-z0-9 ]+: [^<>]{1,1000}|number)>$")
# A dict key is shown inside a placeholder; an angle bracket would end it early.
_ANGLE_RE = re.compile(r"[<>]")
_URL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://\S+$")
_URL_IN_TEXT_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s\"'<>]+")


def redact_url(text):
    """scheme://host[:port]/path without userinfo, query string or fragment.

    The path stays because it names the page; a token that lives in the path is the
    one leak left, and cutting paths would make every URL look alike. A string that does
    not parse as a URL is returned as a placeholder, never as it came.
    """
    s = str(text)
    if not _URL_RE.match(s):
        return f"<text: {len(s)} chars>"
    try:
        parts = urlsplit(s)
        host = parts.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        port = f":{parts.port}" if parts.port else ""
    except ValueError:
        return f"<url: {len(s)} chars>"
    path = parts.path if len(parts.path) <= _PATH_LIMIT else parts.path[:_PATH_LIMIT] + "…"
    return f"{parts.scheme}://{host}{port}{path}"


def redact_urls_in_text(text):
    """Free text (a command line) with every embedded URL run through redact_url."""
    return _URL_IN_TEXT_RE.sub(lambda m: redact_url(m.group(0)), text)


def _kind_name(item):
    for key in _KIND_KEYS:
        v = item.get(key)
        if isinstance(v, str) and _ENUM_RE.match(v):
            return v
    return "?"


def redact_value(key, v):
    """One argument value in display form."""
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, (int, float)):
        return "<number>" if key in CONTENT_KEYS else v
    if isinstance(v, str):
        if _PLACEHOLDER_RE.match(v):
            return v
        if key in URL_KEYS:
            return redact_url(v)
        if key in ENUM_KEYS and _ENUM_RE.match(v):
            return v
        return f"<text: {len(v)} chars>"
    if isinstance(v, (list, tuple)):
        if v and all(isinstance(i, dict) for i in v):
            return f"<{len(v)} items: " + ", ".join(_kind_name(i) for i in v[:20]) + (
                ", ..." if len(v) > 20 else "") + ">"
        return f"<list: {len(v)} items>"
    if isinstance(v, dict):
        return "<object: keys " + ", ".join(
            _ANGLE_RE.sub("?", str(k)[:30]) for k in list(v)[:20]) + ">"
    return f"<{type(v).__name__}>"


def redact_args(args):
    """{key: display value} for a tool's arguments; {} for anything else."""
    if not args or not isinstance(args, dict):
        return {}
    return {k: redact_value(k, v) for k, v in args.items()}
