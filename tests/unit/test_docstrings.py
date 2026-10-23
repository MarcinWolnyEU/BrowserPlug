"""Unit tests for server/tools/docstrings.py: tool text from a docstring.

The expected values are those of griffe's Google parser.
The real tool docstrings are cross-checked against the pinned
tools/list golden, which is where these bytes end up.
"""

import inspect
import json
from pathlib import Path

import pytest

import test_imports  # noqa: F401  (path setup)
from facade_double import FacadeDouble
from server.tools.docstrings import ParsedDocstring, parse_docstring, parse_function_docstring
from server.tools.mcp_tools import BrowserPlugTools

GOLDEN = Path(__file__).resolve().parent / "golden" / "tools_list.json"


def parse(doc):
    """Parse `doc` the way a tool docstring reaches the parser (through inspect.getdoc)."""
    def fn():
        pass
    fn.__doc__ = doc
    return parse_function_docstring(fn)


# =============================================================================
# The tool description
# =============================================================================

def test_no_docstring_gives_no_description():
    assert parse(None) == ParsedDocstring()
    assert parse_docstring("") == ParsedDocstring(description=None, parameters={})


def test_without_args_the_whole_cleaned_docstring_is_the_description():
    doc = """Summary.

        Body line one
        body line two.

        Returns:
            text
        """
    assert parse(doc) == ParsedDocstring(
        description="Summary.\n\nBody line one\nbody line two.\n\nReturns:\n    text")


def test_args_ends_the_description_and_text_after_it_is_dropped():
    doc = """Summary.

    More text.

    Args:
        a: the a

    Trailing paragraph.
    """
    assert parse(doc) == ParsedDocstring(description="Summary.\n\nMore text.",
                                         parameters={"a": "the a"})


def test_an_admonition_shaped_paragraph_ends_the_description():
    # page_read's "Other formats:" block: a `Word words:` line after a blank line with an
    # indented block under it is split off, and so is everything after it.
    doc = """Read a page.

    Default text.

    Other formats:
        text: full visible text
        html: page HTML source

    For a listing prefer page_snapshot.

    Args:
        tab_id: tab to read
    """
    assert parse(doc) == ParsedDocstring(description="Read a page.\n\nDefault text.",
                                         parameters={"tab_id": "tab to read"})


def test_a_colon_line_without_an_indented_block_stays_text():
    # A prose line holding a colon, such as a field name and what it means.
    doc = """Summary.

    Also this: that.
    next line

    Args:
        a: A
    """
    assert parse(doc).description == "Summary.\n\nAlso this: that.\nnext line"


def test_a_section_needs_a_blank_line_above_it():
    doc = """Summary.
    Args:
        a: A
    """
    assert parse(doc) == ParsedDocstring(description="Summary.\nArgs:\n    a: A")


def test_a_blank_line_between_title_and_body_is_not_a_section():
    doc = """Summary.

    Args:

        a: A
    """
    assert parse(doc) == ParsedDocstring(description="Summary.\n\nArgs:\n\n    a: A")


def test_a_docstring_that_opens_with_args_has_no_description():
    assert parse("Args:\n    a: A\n\nReturns:\n    x") == \
        ParsedDocstring(description=None, parameters={"a": "A"})
    # The first text section after it is the description.
    assert parse("Args:\n    a: A\nTail.") == \
        ParsedDocstring(description="Tail.", parameters={"a": "A"})


def test_the_docstring_is_cleaned_twice_as_griffe_did():
    # getdoc cleans once and griffe's Docstring cleans again. When every line after the
    # first is indented, the second pass dedents them, the items lose their indent and
    # no parameter is found; the description is then the ONCE-cleaned text.
    doc = """
        Args:
            a: A
        """
    assert parse(doc) == ParsedDocstring(description="Args:\n    a: A")


def test_section_titles_are_case_insensitive():
    assert parse("S.\n\nARGS:\n    a: A").parameters == {"a": "A"}
    assert parse("S.\n\nParameters:\n    a: A").parameters == {"a": "A"}
    assert parse("S.\n\nArguments:\n    a: A").parameters == {"a": "A"}


def test_code_fences_hide_section_titles():
    doc = "S.\n\n```\nArgs:\n    a: A\n```\n\nArgs:\n    b: B"
    assert parse(doc) == ParsedDocstring(description="S.\n\n```\nArgs:\n    a: A\n```",
                                         parameters={"b": "B"})


def test_other_sections_consume_their_block():
    doc = "S.\n\nExamples:\n    >>> 1\n\nMore.\n\nArgs:\n    a: A\n\nReturns:\n    r: x"
    assert parse(doc) == ParsedDocstring(description="S.", parameters={"a": "A"})


# =============================================================================
# Parameter descriptions
# =============================================================================

def test_continuation_lines_are_dedented_and_joined_with_newlines():
    doc = """S.

    Args:
        wait: "settled" (default) | "load" |
            "none" (return immediately)
        options: capture overrides;
            ttl_seconds: lifetime
                1000..1000000 nested deeper
    """
    assert parse(doc).parameters == {
        "wait": '"settled" (default) | "load" |\n"none" (return immediately)',
        "options": "capture overrides;\nttl_seconds: lifetime\n    1000..1000000 nested deeper",
    }


def test_a_continuation_between_the_two_indents_loses_its_indentation():
    assert parse("S.\n\nArgs:\n    a: first\n      odd\n").parameters == {"a": "first\nodd"}


def test_a_blank_line_inside_an_item_is_kept_but_not_at_the_end():
    assert parse("S.\n\nArgs:\n    a: one\n\n        two\n\n").parameters == {"a": "one\n\ntwo"}


def test_typed_names_drop_the_type():
    doc = "S.\n\nArgs:\n    a (int): A\n    b (int, optional): B"
    assert parse(doc).parameters == {"a": "A", "b": "B"}


def test_a_space_before_the_colon_stays_in_the_name():
    # griffe does not strip it, so "url " matches no property and describes nothing.
    assert parse("S.\n\nArgs:\n    url : spaced").parameters == {"url ": "spaced"}


def test_an_item_without_a_colon_is_skipped():
    assert parse("S.\n\nArgs:\n    no colon here\n    a: A").parameters == {"a": "A"}


def test_an_empty_description_is_kept():
    assert parse("S.\n\nArgs:\n    q:\n    a: A").parameters == {"q": "", "a": "A"}


def test_a_lower_indent_ends_the_section():
    doc = "S.\n\nArgs:\n    a: A\n  b: not an item\n"
    assert parse(doc) == ParsedDocstring(description="S.", parameters={"a": "A"})


def test_repeated_args_sections_merge_and_the_last_wins():
    doc = "S.\n\nArgs:\n    a: first\n    b: B\n\nArgs:\n    a: second"
    assert parse(doc).parameters == {"a": "second", "b": "B"}


def test_keyword_args_are_not_parameters():
    assert parse("S.\n\nKeyword Args:\n    a: A") == \
        ParsedDocstring(description="S.\n\nKeyword Args:\n    a: A")


# =============================================================================
# Styles that another griffe parser (NumPy, Sphinx) would have parsed: refused instead
# =============================================================================

@pytest.mark.parametrize("doc", [
    "S.\n\n:param a: the a",
    "S.\n\n:arg a: the a",
    "S.\n\nParameters\n----------\na : int\n    the a",
])
def test_numpy_and_sphinx_parameters_are_refused(doc):
    with pytest.raises(ValueError):
        parse(doc)


def test_google_parameters_win_before_any_other_style_is_considered():
    assert parse("S.\n\nArgs:\n    a: A\n\n:param b: B").parameters == {"a": "A"}


# =============================================================================
# The real tool docstrings, against the pinned tools/list golden
# =============================================================================

class _Server(FacadeDouble):
    async def send_request_and_wait(self, request, timeout=30.0, browser=None):
        return {}


def _golden_tools():
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    return {t["name"]: t for t in golden["default"]["tools"]}


def _schema(entry):
    return entry.get("input_schema") or entry.get("inputSchema")


# The registry reads these descriptions from the docstring; navigate, page_eval and tabs
# pass theirs explicitly.
DECORATOR_REGISTERED = ("page_read", "page_snapshot", "page_act", "page_screenshot",
                        "requests", "server_info")


@pytest.mark.parametrize("name", DECORATOR_REGISTERED)
async def test_the_description_of_a_real_tool_matches_the_golden(name):
    fn = (await BrowserPlugTools(_Server()).mcp.get_tool(name)).fn
    assert parse_function_docstring(fn).description == _golden_tools()[name]["description"]


@pytest.mark.parametrize("name", DECORATOR_REGISTERED + ("navigate", "page_eval"))
async def test_the_parameter_descriptions_of_a_real_tool_match_the_golden(name):
    fn = (await BrowserPlugTools(_Server()).mcp.get_tool(name)).fn
    parsed = parse_function_docstring(fn)
    properties = _schema(_golden_tools()[name])["properties"]
    assert parsed.parameters, "every one of these documents its parameters in Args:"
    for param, text in parsed.parameters.items():
        if param in properties:
            assert properties[param]["description"] == text, param


async def test_the_explicit_description_tools_send_their_args_block_once():
    # navigate and page_eval pass an explicit description (it gains a paragraph while
    # the server runs), built from the docstring's first section. Their Args: entries
    # reach the client once, as parameter descriptions, not again in the description.
    tools = BrowserPlugTools(_Server())
    golden = _golden_tools()
    for name in ("navigate", "page_eval"):
        fn = (await tools.mcp.get_tool(name)).fn
        assert "\nArgs:\n" in inspect.getdoc(fn)
        assert "Args:" not in golden[name]["description"]
        assert golden[name]["description"] == parse_function_docstring(fn).description
