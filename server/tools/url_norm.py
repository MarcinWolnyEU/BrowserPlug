#!/usr/bin/env python3
"""URL identity helpers shared by `url_diff` (what changed?) and `fused_read` (may the
read run?). Pure, stdlib only, no I/O.

url_diff and fused_read need one identity for a URL, and normalising separately made them disagree: url_diff punycoded an
IDN host, fused_read compared it raw, so `https://żółć.pl/` was "landed elsewhere" for
the read guard and fine for the landing report. One owner for host and dot-segment
identity removes that class of drift.

Host mapping follows what browsers do (UTS-46 non-transitional), not Python's `idna`
codec, which is IDNA 2003: it folds `ß` to `ss`, so `faß.de` and the `xn--fa-hia.de`
a browser reports back compare unequal. The `idna` package is not a declared
dependency, so this is a stdlib approximation: NFKC, lowercase, dot variants to ".",
invisible joiners dropped, then per-label punycode. LIMITATION: no UTS-46 validity
checks (bidi, CONTEXTJ, disallowed code points) and no full case-folding tables; a host
the browser would reject is simply encoded. That can only make two spellings compare
equal or unequal as written, never crash, which is the right failure for a comparison.
"""

from __future__ import annotations

import re
import unicodedata
from typing import List

# Ideographic, fullwidth and halfwidth full stops: UTS-46 maps all three to ".".
_DOT_VARIANTS = re.compile("[。．｡]")
# Soft hyphen and zero-width characters UTS-46 ignores.
_IGNORED = re.compile("[­​‌‍⁠﻿]")
_ESCAPE = re.compile(r"%([0-9a-fA-F]{2})")


def ascii_host(host: str) -> str:
    """Comparable ASCII form of a host name: lowercase, IDN labels as `xn--` punycode.

    Takes a bare host (no port, no userinfo). ASCII input, IP literals and an already
    punycoded label pass through lowercased; a label that cannot be encoded is kept as
    written. The trailing root dot is preserved, callers decide about it.
    """
    text = host or ""
    if text.isascii():
        return text.lower()
    text = unicodedata.normalize("NFKC", text)
    text = _DOT_VARIANTS.sub(".", _IGNORED.sub("", text))
    # str.lower() leaves ß and final sigma alone: the non-transitional behaviour.
    text = unicodedata.normalize("NFC", text.lower())
    labels: List[str] = []
    for label in text.split("."):
        if label.isascii():
            labels.append(label)
            continue
        try:
            labels.append("xn--" + label.encode("punycode").decode("ascii"))
        except UnicodeError:
            labels.append(label)
    return ".".join(labels)


def resolve_dot_segments(path: str) -> str:
    """RFC 3986 section 5.2.4 for an absolute path: `.` and `..` segments removed, as a
    browser does before it sends the request. `%2e` spellings count as dots (browsers
    treat them so); other escapes are left untouched. `..` above the root is dropped."""
    if not path:
        return path
    out: List[str] = []
    segments = path.split("/")
    for i, seg in enumerate(segments):
        plain = _ESCAPE.sub(lambda m: "." if m.group(1) in ("2e", "2E") else m.group(0), seg)
        last = i == len(segments) - 1
        if plain == ".":
            if last:
                out.append("")
        elif plain == "..":
            if len(out) > 1:
                out.pop()
            if last:
                out.append("")
        else:
            out.append(seg)
    result = "/".join(out)
    if path.startswith("/") and not result.startswith("/"):
        result = "/" + result
    return result
