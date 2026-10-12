"""BrowserPlug's own tool table: advertised schemas, argument validation, invocation.

The wire contract here is byte-exact: the `tools/list` shape is pinned by
`tests/unit/golden/tools_list.json`, and the argument-error texts that the harness and
agents read are pinned by `tests/unit/test_tool_registry.py`. The rules come from fastmcp
4.0.10, which served this contract before the move to the official SDK (see
.tmp/2026-10-01-mcp-sdk-migration.md). The module implements them on pydantic and the
stdlib, so the server can sit on the SDK's low-level `Server` without the SDK's own tool
layer, which differs on every point below (migration plan, D1/D2):

  * `ToolSpec.from_function` builds the input schema from pydantic's function schema,
    `compress_schema(prune_titles=True)`, parameter descriptions from the docstring's
    `Args:` (server/tools/docstrings.py) where no `Field` set one, hidden parameters
    pruned, and `$ref`s inlined at list time. The advertised `inputSchema` dict is
    pinned dict for dict, key order included.
  * Arguments are validated against the function's FULL signature: a hidden parameter is
    accepted, an unknown one is refused, and a string reaches the function verbatim (no
    JSON pre-parsing, so `code="null"` stays the four characters). A refusal raises
    `ToolArgumentError`, whose text is pydantic's, titled `call[<function name>]`.
  * `ToolRegistry` is ordered and copy-on-write. Readers take one immutable snapshot, so
    a `tools/list` served on uvicorn's thread never sees a half-applied change made on
    the WebSocket thread; `put` replaces a tool in place, keeping its position.

The error texts an agent reads outside the tool layer are here too, as functions, so the
dispatcher can match them: `unknown_tool_text`, `unexpected_error_text`.

Imports: stdlib, pydantic and `mcp_types` (the SDK's wire models, nothing else).
"""

import asyncio
import functools
import inspect
import json
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import (Annotated, Any, Callable, Dict, Iterable, List, Mapping, Optional,
                    Sequence, Tuple, get_args, get_origin, get_type_hints)

import mcp_types
from pydantic import Field, TypeAdapter, ValidationError

from server.tools.docstrings import parse_function_docstring

# =========================================================================================
# Errors, and the texts an agent reads (pinned by test_tool_registry)
# =========================================================================================


class ToolArgumentError(ValueError):
    """The call's arguments failed validation. `str(error)` is the client-facing text:
    pydantic's own report, e.g. "1 validation error for call[tabs]\\nbogus\\n  Unexpected
    keyword argument [...]" (tests/mcp_client_harness.py keys on `for call[`)."""

    def __init__(self, tool_name: str, error: ValidationError):
        super().__init__(str(error))
        self.tool_name = tool_name
        self.validation_error = error


class UnknownToolError(LookupError):
    """No tool by that name. `str(error)` is the client-facing text, `Unknown tool: 'x'`."""

    def __init__(self, name: str):
        super().__init__(unknown_tool_text(name))
        self.name = name


def unknown_tool_text(name: str) -> str:
    """The text for a tools/call on a name the registry does not have."""
    return f"Unknown tool: {name!r}"


def unexpected_error_text(name: str, exc: BaseException) -> str:
    """The text for a tool body that raised, with the exception detail unmasked:
    `Error calling tool 'tabs': <str(exc)>`.

    Only this one rule applies. The pre-SDK server had four exceptions to it (fixed texts
    for an httpx timeout and an HTTP 429, a ToolError passed through verbatim, an MCPError
    for a missing client capability re-raised as a JSON-RPC error), none reachable from
    BrowserPlug's tools. A pydantic ValidationError raised by a tool BODY used to become a
    JSON-RPC error (-32602 "Invalid request parameters"); whether to keep that is the
    dispatcher's decision."""
    return f"Error calling tool {name!r}: {exc}"


def error_result(text: str) -> mcp_types.CallToolResult:
    """An `isError` result carrying `text` alone, the shape every failure has on the wire:
    {"content": [{"type": "text", "text": ...}], "isError": true}, no `_meta`."""
    return mcp_types.CallToolResult(
        content=[mcp_types.TextContent(type="text", text=text)], is_error=True)


# =========================================================================================
# Results
# =========================================================================================

_CONTENT_TYPES = tuple(get_args(mcp_types.ContentBlock))


def to_call_result(value: Any) -> mcp_types.CallToolResult:
    """Convert a tool's return value to the result that goes on the wire.

    `str` becomes one text block; a `CallToolResult` passes through (content, `_meta` and
    `isError` intact); a content block, or a list of blocks and strings, becomes the
    content. `isError` is always explicit. No `structuredContent` echo of the text is
    produced (plan D5). Anything else is a
    TypeError: no BrowserPlug tool returns another type, and a JSON dump would be a
    silent new wire shape."""
    if isinstance(value, mcp_types.CallToolResult):
        if "is_error" in value.model_fields_set:
            return value
        return value.model_copy(update={"is_error": value.is_error})
    if isinstance(value, (str, *_CONTENT_TYPES)):
        value = [value]
    if isinstance(value, (list, tuple)):
        content = []
        for item in value:
            if isinstance(item, str):
                content.append(mcp_types.TextContent(type="text", text=item))
            elif isinstance(item, _CONTENT_TYPES):
                content.append(item)
            else:
                raise TypeError(f"tool returned a {type(item).__name__} inside its content")
        return mcp_types.CallToolResult(content=content, is_error=False)
    raise TypeError(f"tool returned a {type(value).__name__}; return str, content blocks "
                    "or a CallToolResult")


# =========================================================================================
# Schema normalisation: title pruning, unused-`$defs` pruning, `$ref` inlining
# =========================================================================================

# A node holding any of these is a schema, so its string "title" is metadata.
_SCHEMA_KEYWORDS = frozenset({"type", "properties", "$ref", "items", "allOf", "oneOf",
                              "anyOf", "required"})
# A node holding ONLY these (pydantic's bare {"title": "X"} for Any) is a schema too.
_METADATA_KEYS = frozenset({"title", "description", "deprecated", "readOnly", "writeOnly"})
# Values are user data, never descended into: `default: {"title": "x"}` must survive.
_LITERAL_KEYWORDS = frozenset({"default", "const", "examples", "example", "enum"})
# Values are {user name: sub-schema}.
_SUBSCHEMA_MAP_KEYS = frozenset({"properties", "patternProperties", "$defs", "definitions",
                                 "dependentSchemas", "dependencies"})
# Values are one sub-schema.
_SUBSCHEMA_VALUE_KEYS = frozenset({"items", "additionalItems", "additionalProperties",
                                   "contains", "contentSchema", "propertyNames",
                                   "unevaluatedItems", "unevaluatedProperties", "if",
                                   "then", "else", "not"})
# Values are lists of sub-schemas.
_SUBSCHEMA_LIST_KEYS = frozenset({"allOf", "anyOf", "oneOf", "prefixItems"})
# Deeper than this, scanning stops and every $def is kept (the golden pins the result).
_MAX_DEPTH = 50
_DEFS_PREFIX = "#/$defs/"


def _copy(node: Any) -> Any:
    """A deep copy of a JSON value (key order kept)."""
    if isinstance(node, dict):
        return {k: _copy(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_copy(v) for v in node]
    return node


def compress_schema(schema: Dict[str, Any], prune_params: Iterable[str] = (),
                    prune_titles: bool = False) -> Dict[str, Any]:
    """Prune parameters, optionally prune titles, and drop unused `$defs`. Does not prune
    `additionalProperties` and does not inline `$ref`s (`dereference_refs` does that at
    list time). Returns a new dict; `schema` is not modified."""
    schema = resolve_root_ref(schema)
    for param in prune_params:
        schema = _prune_param(schema, param)
    return _optimize(schema, prune_titles=prune_titles)


def resolve_root_ref(schema: Dict[str, Any]) -> Dict[str, Any]:
    """Inline a root-level `$ref` (MCP requires `type: object` at the root)."""
    if "$ref" in schema and "$defs" in schema and "type" not in schema:
        ref = schema["$ref"]
        if isinstance(ref, str) and ref.startswith(_DEFS_PREFIX):
            defs = schema["$defs"]
            name = ref.split("/")[-1]
            if name in defs:
                resolved = dict(defs[name])
                resolved.update({k: v for k, v in schema.items() if k not in ("$ref", "$defs")})
                resolved["$defs"] = defs
                return resolved
    return schema


def _prune_param(schema: Dict[str, Any], param: str) -> Dict[str, Any]:
    schema = _copy(schema)
    props = schema.get("properties", {})
    if props.pop(param, None) is None:
        return schema
    schema["properties"] = props
    if param in schema.get("required", []):
        schema["required"].remove(param)
        if not schema["required"]:
            schema.pop("required")
    return schema


def _optimize(schema: Dict[str, Any], prune_titles: bool) -> Dict[str, Any]:
    """Drop schema titles (when asked) and every `$defs` entry nothing references."""
    schema = _copy(schema)
    root_refs: set = set()
    def_dependencies: Dict[str, List[str]] = defaultdict(list)
    defs = schema.get("$defs")
    truncated = False

    def visit(node: Any, def_name: Optional[str] = None, skip_defs: bool = False,
              depth: int = 0, in_schema: bool = True) -> None:
        nonlocal truncated
        if depth > _MAX_DEPTH:
            truncated = True
            return
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith(_DEFS_PREFIX):
                target = ref.split("/")[-1]
                if def_name:
                    def_dependencies[target].append(def_name)
                else:
                    root_refs.add(target)
            if in_schema and prune_titles and isinstance(node.get("title"), str) and (
                    any(k in node for k in _SCHEMA_KEYWORDS)
                    or all(k in _METADATA_KEYS for k in node)):
                node.pop("title")
            for key, value in node.items():
                if skip_defs and key == "$defs":
                    continue
                if not in_schema:
                    visit(value, def_name, depth=depth + 1, in_schema=False)
                elif key in _SUBSCHEMA_MAP_KEYS and isinstance(value, dict):
                    for sub in value.values():
                        visit(sub, def_name, depth=depth + 1)
                elif key in _LITERAL_KEYWORDS:
                    continue
                elif key in _SUBSCHEMA_LIST_KEYS and isinstance(value, list):
                    for item in value:
                        visit(item, def_name, depth=depth + 1)
                elif key in _SUBSCHEMA_VALUE_KEYS:
                    visit(value, def_name, depth=depth + 1)
                else:
                    # A key we do not know (an x- extension): collect its refs, never
                    # clean inside it.
                    visit(value, def_name, depth=depth + 1, in_schema=False)
        elif isinstance(node, list):
            for item in node:
                visit(item, def_name, depth=depth + 1, in_schema=in_schema)

    visit(schema, skip_defs=True)
    if defs:
        for name, definition in defs.items():
            visit(definition, def_name=name)
        if truncated:
            return schema

        def used(name: str, visiting: frozenset = frozenset()) -> bool:
            if name in root_refs:
                return True
            if name in visiting:
                return False
            visiting = visiting | {name}
            return any(ref not in visiting and used(ref, visiting)
                       for ref in def_dependencies.get(name, []))

        for name in list(defs):
            if not used(name):
                defs.pop(name)
        if not defs:
            schema.pop("$defs", None)
    return schema


def has_ref(schema: Any) -> bool:
    """Whether a `$ref` appears anywhere, in dicts and in lists of dicts."""
    if isinstance(schema, dict):
        if "$ref" in schema:
            return True
        return any(has_ref(v) for v in schema.values() if isinstance(v, (dict, list)))
    if isinstance(schema, list):
        return any(has_ref(v) for v in schema if isinstance(v, dict))
    return False


def dereference_refs(schema: Dict[str, Any]) -> Dict[str, Any]:
    """Inline every `#/$defs/` reference (some clients, VS Code Copilot among them,
    cannot follow `$ref`).

    A referenced definition replaces the `$ref` node and the node's own sibling keywords
    (a `description`, a `default`) are laid over it, taking precedence. Circular
    definitions cannot be inlined; then only a root `$ref` is resolved and `$defs` stays."""
    defs = schema.get("$defs", {})
    if _defs_have_cycles(defs):
        return resolve_root_ref(schema)

    def inline(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                ref = node["$ref"]
                if not (isinstance(ref, str) and ref.startswith(_DEFS_PREFIX)
                        and ref[len(_DEFS_PREFIX):] in defs):
                    raise ValueError(f"cannot inline $ref {ref!r}: only local #/$defs/ "
                                     "references are supported")
                target = inline(defs[ref[len(_DEFS_PREFIX):]])
                siblings = {k: _copy(v) for k, v in node.items() if k not in ("$ref", "$defs")}
                if siblings:
                    merged = dict(target)
                    merged.update(siblings)
                    return merged
                return target
            return {k: inline(v) for k, v in node.items()}
        if isinstance(node, list):
            return [inline(v) for v in node]
        return node

    result = {k: inline(v) for k, v in schema.items() if k != "$defs"}
    return _strip_discriminator(result)


def _defs_have_cycles(defs: Dict[str, Any]) -> bool:
    edges: Dict[str, set] = defaultdict(set)

    def collect(node: Any, source: str) -> None:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith(_DEFS_PREFIX):
                edges[source].add(ref.split("/")[-1])
            for v in node.values():
                collect(v, source)
        elif isinstance(node, list):
            for v in node:
                collect(v, source)

    for name, definition in defs.items():
        collect(definition, name)
    state: Dict[str, int] = defaultdict(int)  # 0 unvisited, 1 on the stack, 2 done

    def cyclic(node: str) -> bool:
        state[node] = 1
        for nxt in edges.get(node, ()):
            if nxt not in defs:
                continue
            if state[nxt] == 1 or (state[nxt] == 0 and cyclic(nxt)):
                return True
        state[node] = 2
        return False

    return any(state[name] == 0 and cyclic(name) for name in defs)


def _strip_discriminator(node: Any) -> Any:
    """Drop OpenAPI `discriminator` keys next to anyOf/oneOf (their `mapping` points at
    the `$defs` just removed), keeping the tag property required in every variant."""
    if isinstance(node, dict):
        skip = "discriminator" in node and ("anyOf" in node or "oneOf" in node)
        if skip:
            node = _require_discriminator_property(node)
        return {k: (v if k in ("default", "const", "examples", "enum")
                    else _strip_discriminator(v))
                for k, v in node.items() if not (k == "discriminator" and skip)}
    if isinstance(node, list):
        return [_strip_discriminator(v) for v in node]
    return node


def _require_discriminator_property(schema: Dict[str, Any]) -> Dict[str, Any]:
    discriminator = schema.get("discriminator")
    prop = discriminator.get("propertyName") if isinstance(discriminator, dict) else None
    if not isinstance(prop, str):
        return schema
    result = dict(schema)
    for key in ("anyOf", "oneOf"):
        variants = result.get(key)
        if not isinstance(variants, list):
            continue
        fixed = []
        for variant in variants:
            if isinstance(variant, dict):
                required = variant.get("required")
                if required is None:
                    variant = {**variant, "required": [prop]}
                elif isinstance(required, list) and prop not in required:
                    variant = {**variant, "required": [*required, prop]}
            fixed.append(variant)
        result[key] = fixed
    return result


def plain_structured_params(schema: Dict[str, Any]) -> Dict[str, Any]:
    """Advertise an optional array or object parameter as that type alone.

    Pydantic writes `Optional[List[str]] = None` as `anyOf: [array, null]` with
    `default: null`. Together's Qwen tool-call parser leaves a value under such an `anyOf`
    as text: 36 of 39 `requests` starts sent `url_patterns` as the string `["*"]`, and
    every one was refused by the client's schema check. With the array alone it sent 0 of 6
    as text (schema A/B loop, iteration 6, .tmp/dev-docs/tool-economy.md). Only top-level
    parameters matter: nested fields travel inside one JSON value that is parsed whole.
    Validation of a call is unchanged (the full signature still accepts None); a client
    that checks the advertised schema refuses an explicit null for these parameters.
    """
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return schema
    for name, prop in list(properties.items()):
        branches = prop.get("anyOf") if isinstance(prop, dict) else None
        if not (isinstance(branches, list) and len(branches) == 2):
            continue
        kinds = [b.get("type") if isinstance(b, dict) else None for b in branches]
        if "null" not in kinds:
            continue
        inner = branches[1 - kinds.index("null")]
        if not isinstance(inner, dict) or inner.get("type") not in ("array", "object"):
            continue
        flat = dict(inner)
        for key, value in prop.items():
            if key not in ("anyOf", "default"):
                flat[key] = value
        properties[name] = flat
    return schema


# =========================================================================================
# Signatures
# =========================================================================================


def _check_signature(fn: Callable[..., Any]) -> inspect.Signature:
    """Refuse signatures MCP cannot call: arguments arrive by name only."""
    sig = inspect.signature(fn)
    for param in sig.parameters.values():
        if param.kind == inspect.Parameter.POSITIONAL_ONLY:
            raise ValueError(
                "Functions with positional-only parameters are not supported as tools "
                "because MCP passes tool arguments by name. Replace them with standard "
                "parameters that can be passed as keywords.")
        if param.kind == inspect.Parameter.VAR_POSITIONAL:
            raise ValueError("Functions with *args are not supported as tools")
        if param.kind == inspect.Parameter.VAR_KEYWORD:
            raise ValueError("Functions with **kwargs are not supported as tools")
    return sig


def _check_hidden(name: str, sig: inspect.Signature, hidden: Sequence[str]) -> None:
    """A typo would hide nothing, and a required hidden parameter would leave the tool
    uncallable."""
    for arg in hidden:
        if arg not in sig.parameters:
            raise ValueError(f"{name}: hidden parameter {arg!r} is not in the signature")
        if sig.parameters[arg].default is inspect.Parameter.empty:
            raise ValueError(f"{name}: hidden parameter {arg!r} has no default")


def _argument_hints(fn: Callable[..., Any]) -> Dict[str, Any]:
    """Parameter annotations as pydantic should see them: forward references resolved,
    and `Annotated[T, "text"]` read as `Annotated[T, Field(description="text")]`."""
    try:
        hints = get_type_hints(fn, include_extras=True)
    except Exception:
        hints = dict(getattr(fn, "__annotations__", {}) or {})
    hints.pop("return", None)
    for name, hint in list(hints.items()):
        args = get_args(hint)
        if get_origin(hint) is Annotated and len(args) == 2 and isinstance(args[1], str):
            hints[name] = Annotated[args[0], Field(description=args[1])]
    return hints


def _binder(fn: Callable[..., Any], sig: inspect.Signature) -> Callable[..., Any]:
    """A stand-in with `fn`'s name, parameters and annotations that returns the validated
    arguments instead of running anything.

    pydantic validates a call through the function it was built from, so a TypeAdapter
    over `fn` itself would run the body inside validation and a ValidationError raised BY
    the body would pass for an argument error. Validating through this binder keeps the
    two apart. Its `__name__` is `fn`'s because pydantic titles the report
    `call[<__name__>]`, which is the text agents and the test harness read."""
    hints = _argument_hints(fn)

    def binder(*args: Any, **kwargs: Any) -> Tuple[Tuple[Any, ...], Dict[str, Any]]:
        return args, kwargs

    params = [p.replace(annotation=hints.get(p.name, p.annotation))
              for p in sig.parameters.values()]
    binder.__signature__ = sig.replace(parameters=params,
                                       return_annotation=inspect.Signature.empty)
    binder.__annotations__ = {p.name: p.annotation for p in params
                              if p.annotation is not inspect.Parameter.empty}
    binder.__name__ = getattr(fn, "__name__", "binder")
    binder.__qualname__ = getattr(fn, "__qualname__", binder.__name__)
    binder.__module__ = getattr(fn, "__module__", binder.__module__)
    return binder


# =========================================================================================
# ToolSpec
# =========================================================================================


@dataclass(frozen=True, eq=False)
class ToolSpec:
    """One registered tool. Immutable: a change is a new ToolSpec put into the registry.

    `.fn`, `.name`, `.description` and `.parameters` are the attributes the unit tests
    read (`(await tools.mcp.get_tool(name)).fn`).
    """

    name: str
    description: Optional[str]
    fn: Callable[..., Any]
    hidden: Tuple[str, ...] = ()
    # The advertised input schema as canonical JSON: every reader gets a fresh copy, so
    # nothing a consumer does to its dict can reach the registry or another thread.
    schema_json: str = field(default="", repr=False)
    _adapter: Any = field(default=None, repr=False)
    _is_async: bool = field(default=True, repr=False)

    @classmethod
    def from_function(cls, fn: Callable[..., Any], *, name: Optional[str] = None,
                      description: Optional[str] = None,
                      hidden: Iterable[str] = ()) -> "ToolSpec":
        """Build a tool from `fn`: schema, description and hidden-argument pruning.

        `description` None: taken from the docstring (docstrings.py). Given: used verbatim;
        parameter descriptions still come from the docstring's `Args:`. `hidden`: left out
        of the advertised schema, still accepted on a call."""
        sig = _check_signature(fn)
        tool_name = name or getattr(fn, "__name__", None) or type(fn).__name__
        if tool_name == "<lambda>":
            raise ValueError("You must provide a name for lambda functions")
        hidden = tuple(hidden)
        _check_hidden(tool_name, sig, hidden)

        parsed = parse_function_docstring(fn)
        adapter = TypeAdapter(_binder(fn, sig))
        schema = compress_schema(adapter.json_schema(), prune_titles=True)
        properties = schema.get("properties", {})
        for param, text in parsed.parameters.items():
            if param in properties and "description" not in properties[param]:
                properties[param]["description"] = text
        schema = plain_structured_params(schema)
        if hidden:
            schema = compress_schema(schema, prune_params=hidden, prune_titles=True)
        if "$defs" in schema or has_ref(schema):
            schema = dereference_refs(schema)
        return cls(name=tool_name,
                   description=description if description is not None else parsed.description,
                   fn=fn, hidden=hidden,
                   schema_json=json.dumps(schema, ensure_ascii=False),
                   _adapter=adapter,
                   _is_async=inspect.iscoroutinefunction(fn))

    @property
    def parameters(self) -> Dict[str, Any]:
        """The advertised input schema (a fresh dict on every read)."""
        return json.loads(self.schema_json)

    def to_wire(self) -> Dict[str, Any]:
        """This tool's `tools/list` entry: name, description (when there is one) and
        inputSchema, nothing else (no title, outputSchema or _meta: plan D5)."""
        entry: Dict[str, Any] = {"name": self.name}
        if self.description is not None:
            entry["description"] = self.description
        entry["inputSchema"] = self.parameters
        return entry

    def to_mcp_tool(self) -> mcp_types.Tool:
        """The same entry as the SDK's wire model."""
        return mcp_types.Tool.model_validate(self.to_wire())

    def bind(self, arguments: Optional[Mapping[str, Any]]) -> Tuple[Tuple[Any, ...], Dict[str, Any]]:
        """Validate `arguments` (None is `{}`) against the full signature and return the
        (args, kwargs) to call `fn` with. Raises ToolArgumentError."""
        try:
            return self._adapter.validate_python(dict(arguments or {}))
        except ValidationError as e:
            raise ToolArgumentError(self.name, e) from None

    async def run(self, arguments: Optional[Mapping[str, Any]]) -> Any:
        """Validate, then run `fn` and return what it returned. An async fn is awaited on
        the calling loop; a sync one runs on a worker thread with the caller's contextvars
        (asyncio.to_thread copies them), which tool_outcome's recorders rely on. Exceptions
        from the body propagate unchanged."""
        args, kwargs = self.bind(arguments)
        if self._is_async:
            result = await self.fn(*args, **kwargs)
        else:
            result = await asyncio.to_thread(functools.partial(self.fn, *args, **kwargs))
            if inspect.isawaitable(result):
                result = await result
        if inspect.isasyncgen(result):
            result = [item async for item in result]
        elif inspect.isgenerator(result):
            result = list(result)
        return result

    async def call(self, arguments: Optional[Mapping[str, Any]]) -> mcp_types.CallToolResult:
        """`run` + `to_call_result`."""
        return to_call_result(await self.run(arguments))


# =========================================================================================
# The registry
# =========================================================================================


@dataclass(frozen=True)
class _Table:
    order: Tuple[ToolSpec, ...]
    by_name: Mapping[str, ToolSpec]


def _table(order: Sequence[ToolSpec]) -> _Table:
    return _Table(tuple(order), MappingProxyType({spec.name: spec for spec in order}))


class ToolRegistry:
    """The ordered tool table. Writers serialise on a lock and publish a whole new table
    in one reference assignment; readers never lock and always see one consistent table,
    whichever thread they run on."""

    def __init__(self, specs: Iterable[ToolSpec] = ()):
        self._write_lock = threading.Lock()
        self._table = _table(())
        for spec in specs:
            self.add(spec)

    # ---- writes -------------------------------------------------------------------------

    def put(self, spec: ToolSpec) -> bool:
        """Add `spec`, or replace the tool of the same name IN PLACE (same position, no
        window without it). Returns True if it replaced."""
        with self._write_lock:
            order = list(self._table.order)
            for index, existing in enumerate(order):
                if existing.name == spec.name:
                    order[index] = spec
                    self._table = _table(order)
                    return True
            order.append(spec)
            self._table = _table(order)
            return False

    def add(self, spec: ToolSpec) -> None:
        """Append a tool that must not exist yet."""
        with self._write_lock:
            if spec.name in self._table.by_name:
                raise ValueError(f"tool {spec.name!r} is already registered")
            self._table = _table([*self._table.order, spec])

    def replace(self, spec: ToolSpec) -> None:
        """Replace a tool that must exist, keeping its position."""
        with self._write_lock:
            if spec.name not in self._table.by_name:
                raise KeyError(spec.name)
            self._table = _table([spec if s.name == spec.name else s
                                  for s in self._table.order])

    def remove(self, name: str) -> bool:
        """Drop a tool; False (not an error) when it was not registered."""
        with self._write_lock:
            if name not in self._table.by_name:
                return False
            self._table = _table([s for s in self._table.order if s.name != name])
            return True

    # ---- reads --------------------------------------------------------------------------

    def snapshot(self) -> Tuple[ToolSpec, ...]:
        """Every tool, in registration order, as of one instant."""
        return self._table.order

    def get(self, name: str) -> Optional[ToolSpec]:
        return self._table.by_name.get(name)

    def __contains__(self, name: object) -> bool:
        return name in self._table.by_name

    def __len__(self) -> int:
        return len(self._table.order)

    def names(self) -> Tuple[str, ...]:
        return tuple(spec.name for spec in self._table.order)

    def wire_tools(self) -> List[Dict[str, Any]]:
        """The `tools` array of a tools/list result, from one snapshot."""
        return [spec.to_wire() for spec in self.snapshot()]

    def wire_models(self) -> List[mcp_types.Tool]:
        """`wire_tools` as the SDK's models, for a low-level `Server` list handler."""
        return [spec.to_mcp_tool() for spec in self.snapshot()]

    async def call_tool(self, name: str,
                        arguments: Optional[Mapping[str, Any]]) -> mcp_types.CallToolResult:
        """Resolve, validate and run one call. Raises UnknownToolError, ToolArgumentError,
        or whatever the tool body raised; turning those into results is the dispatcher's
        job, because the rename hints and the outcome policy live there."""
        spec = self.get(name)
        if spec is None:
            raise UnknownToolError(name)
        return await spec.call(arguments)

    # ---- the names the unit tests call on `tools.mcp` ------------------------------------

    async def get_tool(self, name: str) -> Optional[ToolSpec]:
        """The tool by name, or None."""
        return self.get(name)

    async def list_tools(self) -> List[ToolSpec]:
        """Every tool, in order."""
        return list(self.snapshot())
