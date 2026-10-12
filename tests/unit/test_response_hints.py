"""Unit tests for server/tools/response_hints.py — response-time steering.

The premise being tested is that a hint only works if it is BOTH specific and
rare: it must name the capability, the exact argument and the value to pass, and
it must stay silent on the near-miss, because a hint that fires on every call is
ignored within one session. So every hint here is tested in both directions —
the case that must fire, and the neighbouring case that must not.

No browser, no server: these are pure functions over the shapes page_act /
page_snapshot / content.frames already return.
"""

import test_imports  # noqa: F401  (path setup)
from test_imports import get_project_root

from server.tools.response_hints import (
    FRAMES_HEADER,
    FRAMES_LEAD_EMPTY_READ,
    FRAMES_LEAD_NO_MATCH,
    check_instead_of_click_hint,
    frames_hint,
    snapshot_root_hint,
    watch_hint,
)

# A hint is appended to a response an agent reads mid-task; two short lines is
# the whole budget. (frames_hint is exempt: it is a list, and page_snapshot
# already pays that cost for the same data.)
MAX_HINT_CHARS = 200


def click_on_toggle(step=1, ref="e5", role="checkbox", kind="native", **extra):
    """A page_act click result whose target resolved to a state-bearing control
    (automation.js `controlFor`) — the shape that must produce the check hint."""
    res = {
        "step": step, "stepAction": "click", "ok": True,
        "target": {"ref": ref, "role": role, "name": "In stock", "selector": "#filters > label"},
        "control": {"ref": ref, "kind": kind, "via": "label", "role": role,
                    "checkedBefore": False, "checked": True, "checkedChanged": True},
    }
    res.update(extra)
    return res


def click_on_button(step=1, selector="#login"):
    """An ordinary button click: automation.js attaches NO `control`."""
    return {"step": step, "stepAction": "click", "ok": True, "domChanged": True,
            "target": {"ref": "e9", "role": "button", "name": "Log in", "selector": selector}}


# ---------------------------------------------------------------------------
# check_instead_of_click_hint — the `check` step action, issued 0 times in 150
# ---------------------------------------------------------------------------

def test_check_hint_fires_for_a_click_that_landed_on_a_checkbox():
    hint = check_instead_of_click_hint([click_on_toggle()])
    assert hint, "a click resolving to a native checkbox must advertise `check`"
    # Names the capability, the exact argument shape, and the value to pass.
    assert '"action":"check"' in hint
    assert '"target":"e5"' in hint
    assert '"value":true' in hint
    assert "e5 is a checkbox" in hint
    # And says WHY click is the wrong tool, not just that check exists.
    assert "toggles" in hint
    assert len(hint) <= MAX_HINT_CHARS


def test_check_hint_fires_for_the_success_that_did_not_move_the_control():
    """The nine corpus clicks that reported ok while the control never moved are
    exactly the ones that needed this, so a failed/no-op click must fire too."""
    res = click_on_toggle(ok=False, checkedUnchanged=True)
    res["control"].update({"checked": False, "checkedChanged": False})
    assert check_instead_of_click_hint([res])


def test_check_hint_fires_for_an_aria_toggle_and_names_its_role():
    hint = check_instead_of_click_hint([click_on_toggle(ref="e12", role="switch", kind="aria")])
    assert "e12 is a switch" in hint
    assert '"target":"e12"' in hint


def test_check_hint_silent_for_an_ordinary_button_click():
    # The near miss: same step action, no control -> no hint. This is the case
    # that would make the hint wallpaper if it were wrong.
    assert check_instead_of_click_hint([click_on_button()]) == ""


def test_check_hint_silent_when_the_agent_already_used_check():
    step = click_on_toggle()
    step["stepAction"] = "check"
    assert check_instead_of_click_hint([step]) == ""


def test_check_hint_silent_when_the_control_is_not_a_toggle_kind():
    assert check_instead_of_click_hint([click_on_toggle(kind="text")]) == ""


def test_check_hint_silent_when_the_step_already_carried_a_value():
    assert check_instead_of_click_hint([click_on_toggle(value=True)]) == ""


def test_check_hint_silent_without_a_ref_to_put_in_the_example():
    """No ref means no pasteable example; a hint naming an unusable target is
    worse than no hint at all."""
    res = click_on_toggle()
    res["control"].pop("ref")
    res["target"].pop("ref")
    assert check_instead_of_click_hint([res]) == ""


def test_check_hint_emitted_once_per_batch():
    hint = check_instead_of_click_hint(
        [click_on_toggle(step=1, ref="e1"), click_on_toggle(step=2, ref="e2"),
         click_on_toggle(step=3, ref="e3")])
    assert hint.count("HINT:") == 1
    assert "e1" in hint and "e2" not in hint


def test_check_hint_tolerates_empty_and_malformed_results():
    assert check_instead_of_click_hint([]) == ""
    assert check_instead_of_click_hint(None) == ""
    assert check_instead_of_click_hint(["not a dict", None, 7]) == ""


# ---------------------------------------------------------------------------
# watch_hint — `watch` passed 0 times in 150 steps
# ---------------------------------------------------------------------------

def test_watch_hint_fires_for_a_state_changing_batch_that_did_not_watch():
    steps = [{"action": "fill", "target": "#q", "value": "ssd"},
             {"action": "click", "target": "Search"}]
    hint = watch_hint(steps, [click_on_button(selector="#q"), click_on_button(selector="#go")])
    assert hint, "a click/fill batch with no watch and no wait must advertise watch"
    assert '"watch":' in hint                     # names the exact argument
    assert "no second call" in hint               # and what it saves
    assert "visible/enabled/value" in hint        # and what it reports
    assert len(hint) <= MAX_HINT_CHARS


def test_watch_hint_uses_selectors_the_results_actually_reported():
    steps = [{"action": "click", "target": "Add to cart"}]
    hint = watch_hint(steps, [click_on_button(selector="#cart-count"),
                              click_on_button(selector="#basket")])
    # Pasteable: the example is built from selectors this page really has.
    assert '"watch":["#cart-count","#basket"]' in hint


def test_watch_hint_falls_back_to_a_generic_pair_when_no_selector_is_usable():
    steps = [{"action": "click", "target": "Search"}]
    hint = watch_hint(steps, [{"step": 1, "stepAction": "click", "ok": True}])
    assert '"watch":["#result","button[type=submit]"]' in hint


def test_watch_hint_skips_an_over_long_selector_rather_than_truncating_it():
    """A clipped selector is a broken example, and a 200-char one blows the
    one-or-two-line budget — so it is skipped, not shortened."""
    steps = [{"action": "click", "target": "Buy"}]
    long_sel = "div:nth-of-type(3) > " * 12 + "button"
    hint = watch_hint(steps, [click_on_button(selector=long_sel)])
    assert long_sel not in hint
    assert '"watch":["#result","button[type=submit]"]' in hint
    assert len(hint) <= MAX_HINT_CHARS


def test_watch_hint_silent_when_a_step_already_watched():
    steps = [{"action": "click", "target": "Log in", "watch": ["#error-banner"]}]
    assert watch_hint(steps, [click_on_button()]) == ""


def test_watch_hint_silent_when_the_batch_contains_a_wait():
    # A wait IS the verification; hinting here is the noise case.
    steps = [{"action": "click", "target": "Search"},
             {"action": "wait", "target": "#results"}]
    assert watch_hint(steps, [click_on_button()]) == ""


def test_watch_hint_silent_for_a_batch_that_changes_nothing():
    for action in ("press", "hover", "scroll", "read_clipboard"):
        steps = [{"action": action, "target": "body"}]
        assert watch_hint(steps, []) == "", f"{action}-only batch must not hint"


def test_watch_hint_silent_for_an_empty_or_malformed_batch():
    assert watch_hint([], []) == ""
    assert watch_hint(None, None) == ""
    assert watch_hint(["click"], []) == ""


# ---------------------------------------------------------------------------
# snapshot_root_hint — the truncation line, naming values instead of parameters
# ---------------------------------------------------------------------------

def elements_under(root_id, n, role="link", start=0):
    return [{"ref": f"e{start + i}", "role": role, "name": f"item {i}",
             "selector": f"#{root_id} > div:nth-of-type({i + 1}) > a"} for i in range(n)]


def test_root_hint_tier1_names_a_concrete_selector_when_one_container_dominates():
    els = elements_under("results", 118) + elements_under("nav", 32, start=200)
    hint = snapshot_root_hint(els, 150, 150)
    assert 'root="#results"' in hint, hint
    assert "118 of them are under #results" in hint
    assert "max_elements=300" in hint          # a value, not "raise max_elements"
    assert "#nav" not in hint                  # the loser is not offered


def test_root_hint_tier2_refuses_to_invent_a_selector_when_no_container_dominates():
    # 50% share — under the 60% gate. The near miss that decides whether this
    # hint is trustworthy: a wrong root= silently snapshots the wrong subtree.
    els = elements_under("left", 75) + elements_under("right", 75, start=200)
    hint = snapshot_root_hint(els, 150, 150)
    assert 'root="' not in hint, f"must not invent a root at 50% share: {hint}"
    assert "no single container holds most of them" in hint
    assert "max_elements=300" in hint
    assert 'mode="records"' in hint


def test_root_hint_tier2_when_nothing_is_id_anchored():
    els = [{"ref": f"e{i}", "role": "link", "name": "x",
            "selector": "body > div > ul > li"} for i in range(150)]
    hint = snapshot_root_hint(els, 150, 150)
    assert 'root="' not in hint
    assert "no single container holds most of them" in hint


def test_root_hint_tier2_counts_form_controls_and_offers_forms_mode():
    els = ([{"ref": f"e{i}", "role": "textbox", "name": "f", "selector": "form > input"}
            for i in range(24)]
           + [{"ref": f"g{i}", "role": "link", "name": "l", "selector": "nav > a"}
              for i in range(126)])
    hint = snapshot_root_hint(els, 150, 150)
    assert 'mode="forms" (24 inputs/buttons here)' in hint


def test_root_hint_omits_forms_mode_when_the_page_has_no_form_controls():
    els = [{"ref": f"e{i}", "role": "link", "name": "l", "selector": "nav > a"}
           for i in range(150)]
    hint = snapshot_root_hint(els, 150, 150)
    assert "forms" not in hint
    assert 'mode="records"' in hint


def test_root_hint_scales_the_suggested_cap_to_the_cap_that_was_used():
    hint = snapshot_root_hint(elements_under("results", 300), 300, 300)
    assert "max_elements=600" in hint
    assert "TRUNCATED at 300" in hint


def test_root_hint_never_suggests_a_root_holding_a_single_element():
    # One element trivially satisfies "100% share"; a container of one is not a
    # container, and suggesting it would read as authoritative scoping advice.
    hint = snapshot_root_hint([{"ref": "e0", "role": "link", "selector": "#only > a"}], 1, 1)
    assert 'root="' not in hint


def test_root_hint_always_reports_the_truncation_itself():
    """It REPLACES the TRUNCATED notice, so it must never render empty — losing
    the notice would hide that the snapshot is partial."""
    for els in ([], None, [{"bogus": 1}]):
        out = snapshot_root_hint(els, 150, 150)
        assert out.startswith("TRUNCATED at 150"), out


# ---------------------------------------------------------------------------
# frames_hint — the parameter page_read/page_act never mention
# ---------------------------------------------------------------------------

FRAMES = [{"frameId": 0, "url": "https://shop.example/checkout"},
          {"frameId": 3, "url": "https://pay.example/embed"},
          {"frameId": 7, "url": "https://widget.example/chat"}]


def test_frames_hint_is_byte_identical_to_what_page_snapshot_emits():
    """The two surfaces must be word-for-word the same: an agent that learned
    frame_id from a snapshot has to recognize it in a page_read response."""
    expected = ("Frames (pass frame_id to page_* tools to reach inside):\n"
                "  frame_id=3 https://pay.example/embed\n"
                "  frame_id=7 https://widget.example/chat")
    assert frames_hint(FRAMES) == expected


def test_frames_header_still_matches_page_snapshot_in_tools_page():
    """Drift guard against the live source: page_snapshot must either still
    carry this literal, or have been switched over to this module."""
    src = (get_project_root() / "server" / "tools" / "page.py").read_text(encoding="utf-8")
    assert FRAMES_HEADER in src or "frames_hint" in src, (
        "page_snapshot's frame header drifted from response_hints.FRAMES_HEADER")


def test_frames_hint_silent_when_the_page_has_only_the_main_frame():
    assert frames_hint([{"frameId": 0, "url": "https://example.org/"}]) == ""
    assert frames_hint([]) == ""
    assert frames_hint(None) == ""


def test_frames_hint_tolerates_malformed_frame_entries():
    assert frames_hint(["nope", None, {"url": "no frameId"}]) == ""


def test_frames_lead_names_the_cause_for_an_empty_read():
    out = frames_hint(FRAMES, lead=FRAMES_LEAD_EMPTY_READ)
    assert out.splitlines()[0] == FRAMES_LEAD_EMPTY_READ
    assert out.splitlines()[1] == FRAMES_HEADER
    assert "iframe" in FRAMES_LEAD_EMPTY_READ          # the cause
    assert "page_read takes frame_id" in FRAMES_LEAD_EMPTY_READ  # the next action


def test_frames_lead_names_the_cause_for_a_target_that_matched_nothing():
    out = frames_hint(FRAMES, lead=FRAMES_LEAD_NO_MATCH)
    assert out.splitlines()[0] == FRAMES_LEAD_NO_MATCH
    assert "page_act takes frame_id" in FRAMES_LEAD_NO_MATCH


def test_frames_lead_adds_nothing_when_there_are_no_subframes():
    # The lead must never appear on its own — it would announce a frame list
    # that does not exist.
    assert frames_hint([{"frameId": 0, "url": "https://example.org/"}],
                       lead=FRAMES_LEAD_EMPTY_READ) == ""
