"""Tool descriptions and parameter descriptions, read from a tool function's docstring.

Every byte this module produces is re-sent on each `tools/list`, and the pinned golden
(`tests/unit/golden/tools_list.json`) pins those bytes. The rules are those of the griffe
parse that fastmcp 4.0.10 applied before the SDK migration. This module reproduces that
parse with the stdlib, so a docstring change moves a description only where a test says
it should:

  * the tool description is the FIRST text section of `inspect.getdoc(fn)`. An `Args:`
    block ends it, and so does any admonition-shaped paragraph (a `Word words:` line
    after a blank line, followed by an indented block), which griffe splits off;
  * each Google-style `Args:` entry becomes that parameter's description, its
    continuation lines dedented and joined with "\n";
  * when no `Args:` entry is found, the description is the whole cleaned docstring.

A wrapped source line therefore reaches every client as a "\n" escape, about one token
each, so the tool docstrings keep each paragraph and each `Args:` entry on one line. A
blank line does not end the description, so nothing meant for maintainers goes in it.

Only the Google style is parsed. A docstring that finds no Google parameters but carries a
NumPy `Parameters` / `---` section or a Sphinx `:param` field is refused with ValueError
instead of being parsed differently in silence.

Pure and stdlib-only; `tests/unit/test_docstrings.py` pins it.
"""

import inspect
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

# griffe's Google section titles (griffe/_internal/docstrings/google.py `_section_kind`).
# Only "parameters" kinds feed the schema; the rest matter because reading them consumes
# lines, which decides where the next text section starts.
_PARAMETER_SECTIONS = frozenset({"args", "arguments", "params", "parameters"})
_OTHER_SECTIONS = frozenset({
    "keyword args", "keyword arguments", "other args", "other arguments", "other params",
    "other parameters", "type args", "type arguments", "type params", "type parameters",
    "raises", "exceptions", "returns", "yields", "receives", "attributes", "functions",
    "methods", "classes", "type aliases", "modules", "warns", "warnings",
})
# Read as one block rather than as items, which consumes lines differently.
_EXAMPLES_SECTION = "examples"

_RE_ADMONITION = re.compile(r"^(?P<type>[\w][\s\w-]*):(\s+(?P<title>[^\s].*))?\s*$",
                            re.IGNORECASE)

# What a NumPy or Sphinx parser would have found parameters in.
_NUMPY_PARAMETER_TITLE = "parameters"
_SPHINX_PARAMETER_PREFIXES = (":param", ":parameter", ":arg", ":argument", ":key", ":keyword")


@dataclass(frozen=True)
class ParsedDocstring:
    """The tool description and the per-parameter descriptions of one docstring."""

    description: Optional[str] = None
    parameters: Dict[str, str] = field(default_factory=dict)


def parse_function_docstring(fn: Callable[..., Any]) -> ParsedDocstring:
    """The description and parameter descriptions derived from `fn`'s docstring
    (`inspect.getdoc`)."""
    return parse_docstring(inspect.getdoc(fn))


def parse_docstring(doc: Optional[str]) -> ParsedDocstring:
    """Parse an already-`inspect.getdoc`-cleaned docstring. Empty or None: no description."""
    if not doc:
        return ParsedDocstring()
    lines = inspect.cleandoc(doc.rstrip()).split("\n")
    description, parameters = _parse_google(lines)
    if parameters:
        return ParsedDocstring(description=description, parameters=parameters)
    _refuse_other_styles(lines)
    # No parameters found: the description is the getdoc text itself, not the cleaned one
    # (the golden pins the difference).
    return ParsedDocstring(description=doc)


def _is_empty_line(line: str) -> bool:
    return not line or line.isspace()


def _parse_google(lines: List[str]) -> Tuple[Optional[str], Dict[str, str]]:
    """griffe's `parse_google` main loop, keeping only text sections and parameters."""
    texts: List[str] = []
    parameters: Dict[str, str] = {}
    current: List[str] = []
    in_code_block = False
    offset = 0

    def flush() -> None:
        nonlocal current
        if current:
            if any(current):
                texts.append("\n".join(current).rstrip("\n"))
            current = []

    while offset < len(lines):
        line = lines[offset]
        is_code_fence = line.lstrip(" ").startswith("```")

        if in_code_block:
            if is_code_fence:
                in_code_block = False
            current.append(line)

        elif is_code_fence:
            in_code_block = True
            current.append(line)

        elif ":" in line and (match := _RE_ADMONITION.match(line)):
            kind = match.group("type").lower()
            is_section = kind in _PARAMETER_SECTIONS or kind in _OTHER_SECTIONS \
                or kind == _EXAMPLES_SECTION
            blank_line_above = offset == 0 or _is_empty_line(lines[offset - 1])
            has_next_line = offset < len(lines) - 1
            has_next_lines = offset < len(lines) - 2
            blank_line_below = has_next_line and _is_empty_line(lines[offset + 1])
            blank_lines_below = has_next_lines and _is_empty_line(lines[offset + 2])
            indented_line_below = (has_next_line and not blank_line_below
                                   and lines[offset + 1].startswith(" "))
            indented_lines_below = (has_next_lines and not blank_lines_below
                                    and lines[offset + 2].startswith(" "))
            if not (indented_line_below or indented_lines_below):
                # Nothing indented follows: an ordinary line that happens to hold a colon.
                current.append(line)
                offset += 1
                continue
            if not blank_line_above or (indented_lines_below and blank_line_below):
                # griffe skips a would-be section with no blank line above it, or with a
                # blank line between its title and its body, and keeps it as text.
                current.append(line)
                offset += 1
                continue

            if is_section:
                flush()
                if kind == _EXAMPLES_SECTION:
                    _, offset = _read_block(lines, offset + 1)
                else:
                    items, offset = _read_block_items(lines, offset + 1)
                    if kind in _PARAMETER_SECTIONS:
                        parameters.update(_parameters_from_items(items))
            else:
                contents, offset = _read_block(lines, offset + 1)
                if contents:
                    flush()
                else:
                    current.append(lines[offset])
        else:
            current.append(line)

        offset += 1

    if current and any(current):
        texts.append("\n".join(current).rstrip("\n"))
    return (texts[0] if texts else None), parameters


def _read_block_items(lines: List[str], offset: int) -> Tuple[List[List[str]], int]:
    """griffe's `_read_block_items`: indented items; returns the LAST line consumed."""
    if offset >= len(lines):
        return [], offset
    new_offset = offset
    items: List[List[str]] = []
    while _is_empty_line(lines[new_offset]):
        new_offset += 1
    indent = len(lines[new_offset]) - len(lines[new_offset].lstrip())
    if indent == 0:
        return [], new_offset - 1

    current = [lines[new_offset][indent:]]
    new_offset += 1
    while new_offset < len(lines):
        line = lines[new_offset]
        if _is_empty_line(line):
            current.append("")
        elif line.startswith(indent * 2 * " "):
            current.append(line[indent * 2:])
        elif line.startswith((indent + 1) * " "):
            # Between the item indent and the continuation indent: griffe keeps the line,
            # stripped of all its indentation, and warns.
            current.append(line[len(line) - len(line.lstrip()):])
        elif line.startswith(indent * " "):
            items.append(current)
            current = [line[indent:]]
        else:
            break
        new_offset += 1
    items.append(current)
    return items, new_offset - 1


def _read_block(lines: List[str], offset: int) -> Tuple[str, int]:
    """griffe's `_read_block`: one indented block as text; returns the LAST line consumed."""
    if offset >= len(lines):
        return "", offset - 1
    new_offset = offset
    block: List[str] = []
    while _is_empty_line(lines[new_offset]):
        new_offset += 1
    indent = len(lines[new_offset]) - len(lines[new_offset].lstrip())
    if indent == 0:
        return "", offset - 1
    block.append(lines[new_offset].lstrip())
    new_offset += 1
    while new_offset < len(lines) and (lines[new_offset].startswith(indent * " ")
                                       or _is_empty_line(lines[new_offset])):
        block.append(lines[new_offset][indent:])
        new_offset += 1
    return "\n".join(block).rstrip("\n"), new_offset - 1


def _parameters_from_items(items: List[List[str]]) -> Dict[str, str]:
    """griffe's `_read_parameters`, reduced to name -> description."""
    found: Dict[str, str] = {}
    for item in items:
        head, sep, description = item[0].partition(":")
        if not sep:
            continue  # griffe warns "Failed to get 'name: description' pair" and skips it
        description = "\n".join([description.lstrip(), *item[1:]]).rstrip("\n")
        if "(" in head and head.endswith(")"):
            name = head.split("(", 1)[0].strip()
        else:
            # Not stripped, as in griffe: "url : x" names a parameter "url ", which then
            # matches no property and describes nothing.
            name = head
        found[name] = description
    return found


def _refuse_other_styles(lines: List[str]) -> None:
    """Raise where a NumPy or Sphinx parse could have found parameters.

    Broader than either parser on purpose (code fences are not exempt): a false alarm
    fails loudly at registration, a miss would change a description in silence."""
    for index, line in enumerate(lines):
        if line.startswith(_SPHINX_PARAMETER_PREFIXES):
            raise ValueError(f"Sphinx-style parameter field in a tool docstring: {line!r}; "
                             "document parameters in a Google-style 'Args:' section")
        nxt = lines[index + 1] if index + 1 < len(lines) else ""
        if (line.strip().lower() == _NUMPY_PARAMETER_TITLE and "-" in nxt
                and _is_empty_line(nxt.replace("-", ""))):
            raise ValueError("NumPy-style 'Parameters' section in a tool docstring; "
                             "document parameters in a Google-style 'Args:' section")
