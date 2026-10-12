"""A page that asks for a human check is handed to the user (server/tools/challenge.py).

Recognition is pure and tested on its own: the URL of the page or of a frame, and the few
titles no ordinary page carries. Widget hosts inside ordinary forms and a page merely
titled "CAPTCHA" must stay silent. The hooks are tested where they print, because the
helper tests alone pass with a hook deleted: the navigate report, the page_read header
and the page_snapshot header.
"""

import pytest

import test_imports  # noqa: F401
from server.tools import tool_outcome
from server.tools.challenge import STOP_TEXT, challenge_label, challenge_note
from server.tools.navigate import render_navigation
from server.tools.page import format_snapshot, render_read_header

STOP = "STOP: this tab shows a "


@pytest.mark.parametrize("url, title, frames, label", [
    ("https://shop.test/Captcha/Add?ReturnUrl=%2F", None, (), "captcha challenge"),
    ("https://shop.test/cdn-cgi/challenge-platform/h/b", None, (), "captcha challenge"),
    ("https://shop.test/listing", "Just a moment...", (), "bot / access check"),
    ("https://shop.test/listing", "Attention Required! | Cloudflare", (), "bot / access check"),
    ("https://shop.test/listing", "Pardon Our Interruption", (), "bot / access check"),
    ("https://shop.test/listing", "Nie jesteś robotem", (), "captcha challenge"),
    ("https://shop.test/listing", "Verify you are human", (), "captcha challenge"),
    ("https://shop.test/listing", "Shop", ("https://shop.test/distil_r_captcha.html",),
     "captcha challenge"),
])
def test_a_challenge_is_recognised_from_its_url_its_title_or_a_frame(url, title, frames, label):
    assert challenge_label(url, title, frames) == label


@pytest.mark.parametrize("url, title, frames", [
    ("https://example.org/", "Example Domain", ()),
    ("https://blog.test/posts/history", "CAPTCHA: a history", ()),
    ("https://blog.test/a", "Just a moment of silence in the Senate", ()),
    ("https://shop.test/signup", "Sign up", ("https://www.google.com/recaptcha/api2/anchor",)),
    ("https://shop.test/signup", "Sign up", ("https://newassets.hcaptcha.com/captcha/v1/x",)),
    ("https://shop.test/consent", "Cookies", ()),
    (None, None, ()),
])
def test_an_ordinary_page_is_left_alone(url, title, frames):
    assert challenge_label(url, title, frames) is None
    assert challenge_note(url, title, frames) == ""


def test_the_note_says_stop_ask_the_user_and_never_automate():
    note = challenge_note("https://shop.test/Captcha/Add", None)
    assert note == "\n" + STOP_TEXT.format(label="captcha challenge")
    assert note.startswith("\n" + STOP)
    assert "Ask the user to solve it" in note
    assert "Do not click, drag, type into or script it" in note


def test_the_note_takes_the_prefix_the_caller_needs():
    assert challenge_note("https://shop.test/captcha", indent="").startswith(STOP)


def test_a_navigation_onto_a_challenge_ends_with_the_stop_line():
    tool_outcome.arm()
    r = {"ok": True, "tabId": 7, "url": "https://shop.test/listing", "title": "Just a moment...",
         "settled": True, "status": "complete"}
    out = render_navigation(r, action="goto", url="https://shop.test/listing", tab_id=7,
                            wait="settled")
    assert "\n" + STOP + "bot / access check" in out


def test_a_navigation_to_an_ordinary_page_carries_no_stop_line():
    tool_outcome.arm()
    r = {"ok": True, "tabId": 7, "url": "https://shop.test/listing", "title": "Listing",
         "settled": True, "status": "complete"}
    out = render_navigation(r, action="goto", url="https://shop.test/listing", tab_id=7,
                            wait="settled")
    assert "STOP:" not in out


def test_a_read_of_a_challenge_tab_puts_the_stop_line_in_the_header():
    r = {"title": "Attention Required! | Cloudflare", "url": "https://shop.test/x",
         "format": "markdown", "content": "Please verify", "totalChars": 13}
    header, _ = render_read_header(r, 0)
    assert STOP + "bot / access check" in header


def test_a_snapshot_of_a_challenge_tab_puts_the_stop_line_under_the_page_line():
    r = {"mode": "interactive", "count": 0, "elements": [],
         "pageState": {"title": "Verify you are human", "url": "https://shop.test/x"}}
    lines = format_snapshot(r, 150)
    assert lines[0].startswith("Page: Verify you are human")
    assert STOP + "captcha challenge" in lines[1]


def test_the_server_instructions_hand_a_challenge_to_the_user():
    from server.tools.instructions import instructions
    text = instructions()
    assert "A captcha or bot check is the user's to solve: stop and ask; never script it." in text
