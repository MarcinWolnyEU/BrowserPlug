"""What a refused call is told after pydantic's report: the argument it meant and the shape to send.

Pure and stdlib-only. The dispatcher (`mcp_server._on_call_tool`) appends `refusal_hints`
to the text of a `ToolArgumentError`; pydantic's own report stays first and unchanged, so
everything that keys on `N validation error(s) for call[tool]` still matches.

Two refusals are common on small models (the schema A/B loop of 2026-10-08,
`.tmp/tool-eval/lib/schema-ab/`):

- An argument the tool does not take, usually one another tool does take: `frame_id` on
  page_screenshot, `read_scope` (navigate's) on page_read. The bare "Unexpected keyword
  argument" names neither the tool's real parameter nor where the name belongs, and the
  call was retried unchanged.
- An array or object sent as a JSON string (`steps="[{...}]"`). The server does not parse
  strings into structures (a string reaches the tool unchanged), so the fix is on the
  caller's side, and the text says what to send instead.
"""

from __future__ import annotations

import difflib
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

# One complete value per structured parameter, shown when it arrived as a string.
_STRUCTURED_EXAMPLES = {
    "steps": '[{"action": "click", "target": "Save"}]',
    "url_patterns": '["*/api/*"]',
    "options": '{"ttl_seconds": 600}',
    "watch": '["#result"]',
    "count": '[".item"]',
}


def _join(names: Sequence[str]) -> str:
    names = list(names)
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def _unknown_argument(tool: str, name: str, params: Mapping[str, Sequence[str]],
                      item_fields: Mapping[str, Mapping[str, Sequence[str]]]) -> str:
    own = list(params.get(tool, ()))
    line = f"{tool} has no {name} argument."
    # watch, nth or settle_ms sent beside steps: a field of each step, not of the call.
    for param, fields in (item_fields.get(tool) or {}).items():
        if name in fields:
            return line + f" {name} is a field of each item in {param}: {param}=[{{..., \"{name}\": ...}}]."
    elsewhere = [t for t, ps in params.items() if t != tool and name in ps]
    close = difflib.get_close_matches(name, own, n=1, cutoff=0.6)
    if not close:
        # read_scope -> scope: a name that ends in one of this tool's parameters.
        close = [p for p in own if name.endswith("_" + p) or name.startswith(p + "_")][:1]
    if close:
        line += f" Did you mean {close[0]}?"
    if elsewhere:
        line += f" {name} belongs to {_join(elsewhere)}."
    return line


def _string_for_structure(name: str, kind: str) -> str:
    what = "a JSON array" if kind == "list_type" else "a JSON object"
    example = _STRUCTURED_EXAMPLES.get(name)
    line = f"{name} must be {what}, not a string that holds one."
    if example:
        line += f" Send it as the value itself: {name}={example}"
    return line


def refusal_hints(tool: str, errors: Iterable[Mapping[str, Any]],
                  params: Mapping[str, Sequence[str]],
                  item_fields: Optional[Mapping[str, Mapping[str, Sequence[str]]]] = None) -> str:
    """Lines to append to pydantic's report for `tool`, or "" when none applies.

    `errors` is `ValidationError.errors()`; `params` maps every listed tool to its
    advertised parameter names, in schema order; `item_fields` maps a tool to the fields of
    its array-of-object parameters (page_act's steps).
    """
    lines: List[str] = []
    unknown = False
    item_fields = item_fields or {}
    for err in errors:
        loc = err.get("loc") or ()
        name = str(loc[-1]) if loc else ""
        kind = err.get("type")
        if kind == "unexpected_keyword_argument" and len(loc) == 1:
            lines.append(_unknown_argument(tool, name, params, item_fields))
            unknown = True
        elif kind in ("list_type", "dict_type") and len(loc) == 1 and isinstance(err.get("input"), str):
            lines.append(_string_for_structure(name, kind))
    if unknown and params.get(tool):
        lines.append(f"{tool} takes: {', '.join(params[tool])}.")
    return ("\n" + "\n".join(lines)) if lines else ""


def tool_parameters(specs: Iterable[Any]) -> Dict[str, List[str]]:
    """{tool name: advertised parameter names} from the registry's specs."""
    return {s.name: list((s.parameters or {}).get("properties", {})) for s in specs}


def tool_item_fields(specs: Iterable[Any]) -> Dict[str, Dict[str, List[str]]]:
    """{tool name: {array parameter: the fields of its items}}, for arrays of objects."""
    out: Dict[str, Dict[str, List[str]]] = {}
    for s in specs:
        for name, schema in ((s.parameters or {}).get("properties") or {}).items():
            items = schema.get("items") if isinstance(schema, dict) else None
            fields = list((items or {}).get("properties") or {}) if isinstance(items, dict) else []
            if fields:
                out.setdefault(s.name, {})[name] = fields
    return out
