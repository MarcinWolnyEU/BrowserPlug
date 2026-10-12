"""Anti-bot and captcha pages: recognise one, and hand it to the user.

Pure and stdlib-only apart from `url_diff`'s interstitial markers. A page that asks for
a human check is the user's to answer. During the eval of 2026-10-08 a shop's anti-bot
service started challenging the user's own browser profile, and two models wrote page_eval
scripts to find and drag the challenge's slider in that live browser. Automating a
challenge risks the user's account and their standing with the site, and it does not
belong to the task, so every result that meets one says to stop and ask the user.

Evidence, strongest first:
- the URL of the page, or of one of its frames, is a challenge path or host
  (`url_diff.INTERSTITIAL_PATH_MARKERS`: /captcha, /cdn-cgi/challenge, ...). A frame
  counts because some services serve the whole challenge in an iframe over the requested
  URL, which keeps its address;
- the title is one a challenge page uses. Only phrases that do not name an ordinary page
  qualify here, so an article titled "CAPTCHA" is not one (url_diff still uses the wider
  list where a changed URL corroborates it).

Widget hosts that sit inside ordinary forms (a reCAPTCHA or hCaptcha box on a sign-up
page) are not evidence: such a page is still the requested one.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Iterable, Optional
from urllib.parse import urlsplit

from server.tools.url_diff import interstitial_evidence

CHALLENGE_LABELS = ("captcha challenge", "bot / access check")

# Lowercased, accent-folded: (phrase, must the title start with it?, label).
_STRONG_TITLES = (
    ("just a moment", True, "bot / access check"),
    ("attention required", True, "bot / access check"),
    ("one more step", True, "bot / access check"),
    ("pardon our interruption", False, "bot / access check"),
    ("you have been blocked", False, "bot / access check"),
    ("verify you are human", False, "captcha challenge"),
    ("are you a robot", False, "captcha challenge"),
    ("nie jestes robotem", False, "captcha challenge"),
)

# What may follow a title-initial phrase: punctuation, then optionally "| Site Name".
_TITLE_TAIL = re.compile(r"[\s.!?:]*([|\-].*)?")

# Captcha widgets embedded in ordinary pages; their frames are not a wall.
_WIDGET_HOSTS = ("hcaptcha.com", "recaptcha.net", "google.com", "gstatic.com")

STOP_TEXT = ("STOP: this tab shows a {label}, a check meant for a person, not the page "
             "asked for. Ask the user to solve it in their browser, then retry. Do not "
             "click, drag, type into or script it with page_act or page_eval.")


def _fold(text: Optional[str]) -> str:
    if not text:
        return ""
    return unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode("ascii").lower().strip()


def _url_label(url: Optional[str]) -> Optional[str]:
    found = interstitial_evidence(url)
    if found and found[1] == "url" and found[0] in CHALLENGE_LABELS:
        return found[0]
    return None


def challenge_label(url: Optional[str], title: Optional[str] = None,
                    frame_urls: Iterable[Optional[str]] = ()) -> Optional[str]:
    """"captcha challenge" / "bot / access check" when the page is one, else None."""
    label = _url_label(url)
    if label:
        return label
    folded = _fold(title)
    for phrase, at_start, title_label in _STRONG_TITLES:
        if at_start:
            hit = folded.startswith(phrase) and _TITLE_TAIL.fullmatch(folded[len(phrase):])
        else:
            hit = phrase in folded
        if hit:
            return title_label
    for frame_url in frame_urls or ():
        host = (urlsplit(frame_url or "").netloc or "").lower()
        if any(host == w or host.endswith("." + w) for w in _WIDGET_HOSTS):
            continue
        label = _url_label(frame_url)
        if label:
            return label
    return None


def challenge_note(url: Optional[str], title: Optional[str] = None,
                   frame_urls: Iterable[Optional[str]] = (), indent: str = "\n") -> str:
    """The stop-and-ask line, prefixed with `indent`, or "" for an ordinary page."""
    label = challenge_label(url, title, frame_urls)
    return f"{indent}{STOP_TEXT.format(label=label)}" if label else ""
