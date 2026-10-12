"""Unit tests for server/tools/eval_hints.py — the page_eval isolated-world blind-spot note.

Pure-function tests (no browser, no server): an EMPTY isolated-world read of a
page-JS expando / framework internal must produce a distinct, actionable NOTE,
while plain DOM code that legitimately returns undefined must stay noise-free.
"""

import test_imports  # noqa: F401  (path setup)
from server.tools.eval_hints import isolated_world_blindspot_note

DUNDER = "document.getElementById('carrier').__pageExpando"


def test_fires_for_dunder_access_with_none_result():
    note = isolated_world_blindspot_note("el.__vueParentComponent", "isolated", None)
    assert note, "expected the blind-spot note for a dunder read returning None"
    # It says invisible-from-this-world, names the mechanism, and gives the fix.
    assert "INVISIBLE FROM THE ISOLATED WORLD" in note
    assert "__vueParentComponent" in note          # names the mechanism/example
    assert 'world:"main"' in note                  # exact remedy
    assert "wrappedJSObject" in note               # Firefox alternative


def test_fires_for_bracket_dunder_access():
    note = isolated_world_blindspot_note("el['__reactProps$abc']", "isolated", None)
    assert note, "bracket-form dunder access should also fire"


def test_fires_for_empty_list_result():
    assert isolated_world_blindspot_note(DUNDER, "isolated", [])


def test_fires_for_empty_dict_result():
    assert isolated_world_blindspot_note(DUNDER, "isolated", {})


def test_fires_for_framework_marker_without_dunder_syntax():
    # $nuxt is a page global that does NOT match the dunder-access regex (leading
    # '$', not '__'); the framework-marker set must still catch it.
    assert not any(c == '_' for c in "$nuxt"[:2])  # sanity: no leading dunder
    note = isolated_world_blindspot_note("window.$nuxt", "isolated", None)
    assert note, "a known framework marker ($nuxt) should fire even without dunder syntax"


def test_suppressed_when_code_uses_wrappedJSObject():
    # The author already knows the cross-world trick — don't nag.
    assert isolated_world_blindspot_note(
        "window.wrappedJSObject.__vueThing", "isolated", None) == ""


def test_suppressed_for_main_world():
    assert isolated_world_blindspot_note(DUNDER, "main", None) == ""


def test_suppressed_when_result_is_non_empty():
    assert isolated_world_blindspot_note(DUNDER, "isolated", {"n": 42}) == ""
    assert isolated_world_blindspot_note(DUNDER, "isolated", [1]) == ""
    assert isolated_world_blindspot_note(DUNDER, "isolated", "value") == ""


def test_empty_string_and_zero_are_not_empty():
    # "": a legitimate scalar return (e.g. document.title on an untitled page),
    # 0 / False: legitimate scalars. None of these should trip the note.
    assert isolated_world_blindspot_note(DUNDER, "isolated", "") == ""
    assert isolated_world_blindspot_note(DUNDER, "isolated", 0) == ""
    assert isolated_world_blindspot_note(DUNDER, "isolated", False) == ""


def test_suppressed_for_plain_dom_code_returning_none():
    # A .click() legitimately returns undefined -> None, with NO dunder/marker.
    assert isolated_world_blindspot_note(
        "document.querySelector('button').click()", "isolated", None) == ""


def test_suppressed_for_plain_dom_read_returning_empty_list():
    assert isolated_world_blindspot_note(
        "Array.from(document.querySelectorAll('.missing'))", "isolated", []) == ""


def test_suppressed_when_no_code():
    assert isolated_world_blindspot_note("", "isolated", None) == ""
    assert isolated_world_blindspot_note(None, "isolated", None) == ""


# ---- script-error hints: arrow IIFE shape and the top-level return ------------------

from server.tools.eval_hints import arrow_iife_hint, top_level_return_hint  # noqa: E402

_ARROW_CALLED_IN_PARENS = "(() => { const el = document.querySelector('#editor'); return el }())"


def test_arrow_called_inside_its_own_parentheses_gets_the_working_shape():
    for err in ("SyntaxError: missing ) in parenthetical", "SyntaxError: Unexpected token ')'"):
        hint = arrow_iife_hint(err, _ARROW_CALLED_IN_PARENS)
        assert "(() => { ... })()" in hint
        assert "cannot be called inside its own parentheses" in hint
        assert "\n" not in hint


def test_async_and_parameterised_arrows_are_recognised_too():
    for code in ("(async () => { await 1; return 2 }());", "(x => { return x }(1))"):
        assert arrow_iife_hint("SyntaxError: missing ) in parenthetical", code)


def test_arrow_hint_is_silent_for_the_correct_shape_and_for_other_errors():
    err = "SyntaxError: missing ) in parenthetical"
    assert arrow_iife_hint(err, "(() => { return 1 })()") == ""
    assert arrow_iife_hint(err, "document.title.split(") == ""
    assert arrow_iife_hint("TypeError: el is null", _ARROW_CALLED_IN_PARENS) == ""
    assert arrow_iife_hint("", _ARROW_CALLED_IN_PARENS) == ""
    assert arrow_iife_hint(err, "") == ""


def test_top_level_return_hint_names_the_wrapper_and_where_its_call_goes():
    for err in ("SyntaxError: return not in function", "SyntaxError: Illegal return statement"):
        hint = top_level_return_hint(err)
        assert "(() => { ...; return value; })()" in hint
        assert "after the closing parenthesis" in hint


def test_script_hints_are_silent_for_an_unrelated_error():
    err = "TypeError: window.Quill is not a constructor"
    assert top_level_return_hint(err) == ""
    assert arrow_iife_hint(err, _ARROW_CALLED_IN_PARENS) == ""
