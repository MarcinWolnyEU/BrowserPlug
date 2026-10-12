"""Unit tests for server/tools/tool_registry.py: the tool contract, pinned byte for byte.

The expected schemas and texts are the ones clients saw before the SDK migration (see
.tmp/2026-10-01-mcp-sdk-migration.md); the argument-error texts come from the ORACLE
corpus. pydantic writes its docs URL into every validation error, with its
own major.minor in the path, so that one segment is derived from the installed pydantic.
"""

import contextvars
import json
import threading
from pathlib import Path
from typing import Annotated, Any, Dict, List, Literal, Optional, Union

import mcp_types
import pydantic
import pytest
from pydantic import BaseModel, Field

import test_imports  # noqa: F401  (path setup)
from facade_double import FacadeDouble
from server.tools.mcp_tools import BrowserPlugTools
from server.tools.tool_registry import (
    ToolArgumentError,
    ToolRegistry,
    ToolSpec,
    UnknownToolError,
    compress_schema,
    dereference_refs,
    error_result,
    to_call_result,
    unexpected_error_text,
    unknown_tool_text,
)

DOCS = "https://errors.pydantic.dev/{}/v/".format(".".join(pydantic.VERSION.split(".")[:2]))
GOLDEN = Path(__file__).resolve().parent / "golden" / "tools_list.json"


def wire(model):
    """A result or tool as the SDK writes it into a JSON-RPC frame."""
    return json.loads(model.model_dump_json(by_alias=True, exclude_unset=True))


# =============================================================================
# Input schemas
# =============================================================================

async def navigate(url: str, proxy: Optional[str] = None, wait: bool = True) -> str:
    """Go somewhere.

    Args:
        url: where to go
        proxy: a SOCKS proxy
        wait: wait for the load
            to settle
    """
    return f"url={url} proxy={proxy} wait={wait}"


class Step(BaseModel):
    """One step."""
    action: str = Field(description="what to do")
    nth: Optional[int] = Field(default=None, description="which match")


async def act(steps: List[Step], tab_id: Optional[int] = None) -> str:
    """Act.

    Args:
        steps: the steps
        tab_id: the tab
    """
    return f"{len(steps)} steps"


def test_schema_is_fastmcps_normalised_function_schema():
    spec = ToolSpec.from_function(navigate)
    assert spec.name == "navigate"
    assert spec.description == "Go somewhere."
    assert json.dumps(spec.parameters) == json.dumps({
        "additionalProperties": False,
        "properties": {
            "url": {"type": "string", "description": "where to go"},
            "proxy": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None,
                      "description": "a SOCKS proxy"},
            "wait": {"default": True, "type": "boolean",
                     "description": "wait for the load\nto settle"},
        },
        "required": ["url"],
        "type": "object",
    }), "key order included: docstring descriptions come last"


def test_a_nested_model_is_inlined_without_titles_or_defs():
    params = ToolSpec.from_function(act).parameters
    assert "$defs" not in params and "$ref" not in json.dumps(params)
    assert "title" not in json.dumps(params)
    assert params["properties"]["steps"] == {
        "items": {
            "description": "One step.",
            "properties": {
                "action": {"description": "what to do", "type": "string"},
                "nth": {"anyOf": [{"type": "integer"}, {"type": "null"}], "default": None,
                        "description": "which match"},
            },
            "required": ["action"],
            "type": "object",
        },
        "type": "array",
        "description": "the steps",
    }
    assert params["required"] == ["steps"]


async def described(a: Annotated[str, Field(description="from Field")] = "x",
                    b: Annotated[str, "from a bare string"] = "y",
                    c: Optional[Dict[str, Any]] = None,
                    d: Optional[Union[str, int]] = None,
                    e: Literal["one", "two"] = "one",
                    title: str = "t") -> str:
    """D.

    Args:
        a: from the docstring (loses to Field)
        c: options
        title: a parameter literally named title
    """
    return "ok"


def test_field_descriptions_win_and_other_types_keep_pydantics_shape():
    props = ToolSpec.from_function(described).parameters["properties"]
    assert props["a"] == {"default": "x", "description": "from Field", "type": "string"}
    assert props["b"] == {"default": "y", "description": "from a bare string", "type": "string"}
    # An optional object or array is advertised as that type alone (plain_structured_params).
    assert props["c"] == {"additionalProperties": True, "type": "object",
                          "description": "options"}
    assert props["d"] == {"anyOf": [{"type": "string"}, {"type": "integer"}, {"type": "null"}],
                          "default": None}
    assert props["e"] == {"default": "one", "enum": ["one", "two"], "type": "string"}
    # A PROPERTY named title survives; only schema metadata titles go.
    assert props["title"] == {"default": "t", "type": "string",
                              "description": "a parameter literally named title"}


async def structured(items: Optional[List[str]] = None, opts: Optional[Dict[str, Any]] = None,
                     name: Optional[str] = None, n: Optional[int] = None,
                     need: List[str] = ()) -> str:
    return f"{items} {opts} {name} {n}"


def test_an_optional_array_or_object_is_advertised_without_the_null_wrapper():
    """Together's Qwen parser sent a value under anyOf[array, null] as text, 36 of 39 times
    (tool-economy.md, iteration 6). Scalars keep their wrapper: no such failure was seen."""
    props = ToolSpec.from_function(structured).parameters["properties"]
    assert props["items"] == {"items": {"type": "string"}, "type": "array"}
    assert props["opts"] == {"additionalProperties": True, "type": "object"}
    assert props["name"]["anyOf"] == [{"type": "string"}, {"type": "null"}]
    assert props["n"]["anyOf"] == [{"type": "integer"}, {"type": "null"}]
    assert props["need"]["type"] == "array"


@pytest.mark.asyncio
async def test_a_null_for_such_a_parameter_still_reaches_the_function():
    # Only the advertised schema changed; the call is validated against the signature.
    spec = ToolSpec.from_function(structured)
    result = await spec.call({"items": None, "opts": None})
    assert result.content[0].text == "None None None None"


def test_an_explicit_description_is_used_verbatim_and_params_still_come_from_args():
    spec = ToolSpec.from_function(navigate, name="go", description="Go.\n\nArgs:\n    url: x")
    assert spec.name == "go"
    assert spec.description == "Go.\n\nArgs:\n    url: x"
    assert spec.parameters["properties"]["url"]["description"] == "where to go"


def test_a_function_without_a_docstring_has_no_description():
    async def bare(x: int = 0) -> str:
        return ""
    spec = ToolSpec.from_function(bare)
    assert spec.description is None
    assert spec.to_wire() == {"name": "bare", "inputSchema": {
        "additionalProperties": False,
        "properties": {"x": {"default": 0, "type": "integer"}}, "type": "object"}}


class Secret(BaseModel):
    key: str


async def hides(x: int = 0, secret: Optional[Secret] = None,
                flag: Optional[str] = None) -> str:
    return f"x={x} secret={secret} flag={flag}"


def test_hidden_parameters_leave_the_schema_and_their_defs_with_them():
    params = ToolSpec.from_function(hides, hidden=("secret", "flag")).parameters
    assert params == {"additionalProperties": False,
                      "properties": {"x": {"default": 0, "type": "integer"}},
                      "type": "object"}


def test_a_hidden_parameter_must_exist_and_have_a_default():
    with pytest.raises(ValueError, match=r"^navigate: hidden parameter 'nope' is not in the "
                                         r"signature$"):
        ToolSpec.from_function(navigate, hidden=("nope",))
    with pytest.raises(ValueError, match=r"^navigate: hidden parameter 'url' has no default$"):
        ToolSpec.from_function(navigate, hidden=("url",))


@pytest.mark.parametrize("source,message", [
    ("async def f(a, /): pass", "positional-only parameters"),
    ("async def f(*a): pass", r"\*args"),
    ("async def f(**k): pass", r"\*\*kwargs"),
])
def test_signatures_mcp_cannot_call_by_name_are_refused(source, message):
    namespace = {}
    exec(source, namespace)
    with pytest.raises(ValueError, match=message):
        ToolSpec.from_function(namespace["f"])


def test_a_lambda_needs_a_name():
    with pytest.raises(ValueError, match="You must provide a name for lambda functions"):
        ToolSpec.from_function(lambda: "")


class Node(BaseModel):
    children: List["Node"] = []


async def tree(root: Optional[Node] = None) -> str:
    return ""


def test_a_recursive_model_keeps_its_defs_as_fastmcp_did():
    params = ToolSpec.from_function(tree).parameters
    assert params["$defs"] == {"Node": {"properties": {"children": {
        "default": [], "items": {"$ref": "#/$defs/Node"}, "type": "array"}}, "type": "object"}}
    assert params["properties"]["root"] == {
        "anyOf": [{"$ref": "#/$defs/Node"}, {"type": "null"}], "default": None}


# ---- the normalisation helpers on their own ------------------------------------------

def test_compress_prunes_metadata_titles_but_not_data_or_extensions():
    schema = {
        "title": "Root", "type": "object",
        "properties": {
            "a": {"title": "A", "type": "string", "default": {"title": "data"}},
            "b": {"title": "Any-typed"},
            "c": {"type": "string", "x-ui": {"title": "extension payload"}},
        },
        "$defs": {"Unused": {"title": "U", "type": "string"}},
    }
    assert compress_schema(schema, prune_titles=True) == {
        "type": "object",
        "properties": {
            "a": {"type": "string", "default": {"title": "data"}},
            "b": {},
            "c": {"type": "string", "x-ui": {"title": "extension payload"}},
        },
    }
    assert schema["title"] == "Root", "the input is not modified"


def test_compress_prunes_a_param_from_properties_and_required():
    schema = {"type": "object", "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
              "required": ["a", "b"]}
    assert compress_schema(schema, prune_params=["a"]) == {
        "type": "object", "properties": {"b": {"type": "string"}}, "required": ["b"]}
    assert compress_schema(schema, prune_params=["a", "b"]) == {
        "type": "object", "properties": {}}


def test_compress_keeps_defs_referenced_only_from_other_defs_in_use():
    schema = {"type": "object", "properties": {"p": {"$ref": "#/$defs/A"}},
              "$defs": {"A": {"properties": {"b": {"$ref": "#/$defs/B"}}},
                        "B": {"type": "string"}, "C": {"type": "string"}}}
    assert set(compress_schema(schema)["$defs"]) == {"A", "B"}


def test_dereference_lays_ref_siblings_over_the_definition():
    schema = {"type": "object",
              "properties": {"p": {"$ref": "#/$defs/A", "description": "outer", "default": 1}},
              "$defs": {"A": {"description": "inner", "type": "integer"}}}
    assert dereference_refs(schema) == {"type": "object", "properties": {
        "p": {"description": "outer", "type": "integer", "default": 1}}}


def test_dereference_refuses_what_it_cannot_resolve():
    with pytest.raises(ValueError, match="only local"):
        dereference_refs({"type": "object", "properties": {"p": {"$ref": "http://x/y"}}})


def test_dereference_drops_a_discriminator_and_keeps_its_tag_required():
    schema = {"type": "object", "properties": {"p": {
        "discriminator": {"propertyName": "kind", "mapping": {"a": "#/$defs/A"}},
        "oneOf": [{"$ref": "#/$defs/A"}, {"$ref": "#/$defs/B"}]}},
        "$defs": {"A": {"properties": {"kind": {"const": "a"}}, "type": "object"},
                  "B": {"properties": {"kind": {"const": "b"}}, "required": ["kind"],
                        "type": "object"}}}
    assert dereference_refs(schema)["properties"]["p"] == {"oneOf": [
        {"properties": {"kind": {"const": "a"}}, "type": "object", "required": ["kind"]},
        {"properties": {"kind": {"const": "b"}}, "required": ["kind"], "type": "object"}]}


# =============================================================================
# Argument validation
# =============================================================================

async def tabs(action: str = "list", tab_id: Optional[int] = None,
               proxy: Optional[str] = None) -> str:
    return f"{action} {tab_id} {proxy}"


async def page_act(steps: List[Step], tab_id: Optional[int] = None) -> str:
    return "ok"


def refusal(spec, arguments):
    with pytest.raises(ToolArgumentError) as info:
        spec.bind(arguments)
    return str(info.value)


def test_an_unknown_argument_is_refused_in_fastmcps_words():
    assert refusal(ToolSpec.from_function(tabs), {"bogus": 1}) == (
        "1 validation error for call[tabs]\nbogus\n  Unexpected keyword argument "
        "[type=unexpected_keyword_argument, input_value=1, input_type=int]\n"
        f"    For further information visit {DOCS}unexpected_keyword_argument")


def test_the_nested_params_agent_mistake_is_refused_naming_params():
    assert refusal(ToolSpec.from_function(tabs), {"params": "{}"}) == (
        "1 validation error for call[tabs]\nparams\n  Unexpected keyword argument "
        "[type=unexpected_keyword_argument, input_value='{}', input_type=str]\n"
        f"    For further information visit {DOCS}unexpected_keyword_argument")


def test_a_missing_required_argument():
    spec = ToolSpec.from_function(page_act)
    want = ("1 validation error for call[page_act]\nsteps\n  Missing required argument "
            "[type=missing_argument, input_value={}, input_type=dict]\n"
            f"    For further information visit {DOCS}missing_argument")
    assert refusal(spec, {}) == want
    assert refusal(spec, None) == want, "no arguments (or null) is {}"


def test_a_wrong_type_and_several_errors_at_once():
    spec = ToolSpec.from_function(tabs)
    assert refusal(spec, {"tab_id": "abc", "bogus": 1}) == (
        "2 validation errors for call[tabs]\ntab_id\n  Input should be a valid integer, "
        "unable to parse string as an integer [type=int_parsing, input_value='abc', "
        f"input_type=str]\n    For further information visit {DOCS}int_parsing\n"
        "bogus\n  Unexpected keyword argument [type=unexpected_keyword_argument, "
        f"input_value=1, input_type=int]\n    For further information visit "
        f"{DOCS}unexpected_keyword_argument")


def test_a_list_given_as_a_json_string_is_refused_not_parsed():
    text = refusal(ToolSpec.from_function(page_act), {"steps": '[{"action": "click"}]'})
    assert text == (
        "1 validation error for call[page_act]\nsteps\n  Input should be a valid list "
        "[type=list_type, input_value='[{\"action\": \"click\"}]', input_type=str]\n"
        f"    For further information visit {DOCS}list_type")


def test_the_report_is_titled_by_the_function_not_the_tool_name():
    text = refusal(ToolSpec.from_function(tabs, name="renamed"), {"bogus": 1})
    assert text.startswith("1 validation error for call[tabs]\n")


@pytest.mark.parametrize("value", ["[1,2,3]", "{}", "null", "true", "12"])
def test_json_looking_strings_reach_the_function_verbatim(value):
    args, kwargs = ToolSpec.from_function(tabs).bind({"action": value, "proxy": value})
    bound = {**dict(zip(("action", "tab_id", "proxy"), args)), **kwargs}
    assert bound["action"] == value and bound["proxy"] == value


def test_hidden_arguments_are_accepted_and_lax_coercion_still_applies():
    spec = ToolSpec.from_function(tabs, hidden=("proxy",))
    assert "proxy" not in spec.parameters["properties"]
    args, kwargs = spec.bind({"proxy": "socks5://127.0.0.1:9050", "tab_id": "5"})
    bound = {**dict(zip(("action", "tab_id", "proxy"), args)), **kwargs}
    assert bound["proxy"] == "socks5://127.0.0.1:9050" and bound["tab_id"] == 5


def test_unknown_keys_inside_a_nested_model_are_ignored():
    args, kwargs = ToolSpec.from_function(page_act).bind(
        {"steps": [{"action": "click", "bogus": 1}]})
    steps = kwargs.get("steps", args[0] if args else None)
    assert steps == [Step(action="click")]


# =============================================================================
# Invocation and results
# =============================================================================

async def test_an_async_tool_runs_on_the_calling_loop_and_returns_text():
    result = await ToolSpec.from_function(navigate).call({"url": "u", "proxy": "p"})
    assert wire(result) == {"content": [{"type": "text", "text": "url=u proxy=p wait=True"}],
                            "isError": False}


CALLER = contextvars.ContextVar("caller", default=None)


async def test_a_sync_tool_runs_on_a_worker_thread_with_the_callers_context():
    # tool_outcome's recorders rely on this: a sync tool runs in a worker thread holding a
    # COPY of the caller's context.
    loop_thread = threading.get_ident()

    def sync_tool(tag: str = "x") -> str:
        return f"{tag} {CALLER.get()} {threading.get_ident() != loop_thread}"
    CALLER.set("me")
    result = await ToolSpec.from_function(sync_tool).call({"tag": "t"})
    assert result.content[0].text == "t me True"


async def test_a_validation_error_raised_by_the_body_is_not_an_argument_error():
    async def model_builder(n: int = 1) -> str:
        return str(pydantic.TypeAdapter(int).validate_python("not a number"))
    with pytest.raises(pydantic.ValidationError) as info:
        await ToolSpec.from_function(model_builder).call({})
    assert not isinstance(info.value, ToolArgumentError)


async def test_other_body_exceptions_propagate_unchanged():
    async def broken() -> str:
        raise RuntimeError("kaboom <x>")
    with pytest.raises(RuntimeError, match="kaboom"):
        await ToolSpec.from_function(broken).call(None)


async def test_generators_are_materialised():
    def lines():
        yield "a"
        yield "b"
    result = await ToolSpec.from_function(lines).call({})
    assert [b.text for b in result.content] == ["a", "b"]


def test_result_conversion():
    image = mcp_types.ImageContent(type="image", data="AA==", mimeType="image/png")
    assert wire(to_call_result("hi")) == {"content": [{"type": "text", "text": "hi"}],
                                          "isError": False}
    assert wire(to_call_result(["a", image])) == {"content": [
        {"type": "text", "text": "a"},
        {"type": "image", "data": "AA==", "mimeType": "image/png"}], "isError": False}
    # A CallToolResult passes through, _meta included, and gains an explicit isError.
    passed = to_call_result(mcp_types.CallToolResult(
        content=[image], _meta={"browserplug/degraded": [{"kind": "truncated"}]}))
    assert wire(passed) == {"_meta": {"browserplug/degraded": [{"kind": "truncated"}]},
                            "content": [{"type": "image", "data": "AA==",
                                         "mimeType": "image/png"}],
                            "isError": False}
    failed = mcp_types.CallToolResult(content=[], is_error=True)
    assert to_call_result(failed) is failed
    for bad in (None, 5, {"a": 1}, [object()]):
        with pytest.raises(TypeError):
            to_call_result(bad)


def test_the_texts_fastmcp_used_outside_the_tool_layer():
    assert unknown_tool_text("nope") == "Unknown tool: 'nope'"
    assert str(UnknownToolError("nope")) == "Unknown tool: 'nope'"
    assert unexpected_error_text("tabs", RuntimeError("kaboom <x>")) == \
        "Error calling tool 'tabs': kaboom <x>"
    assert unexpected_error_text("tabs", KeyError("missing")) == \
        "Error calling tool 'tabs': 'missing'"
    assert unexpected_error_text("tabs", ValueError()) == "Error calling tool 'tabs': "
    assert wire(error_result("Error listing tabs: boom")) == {
        "content": [{"type": "text", "text": "Error listing tabs: boom"}], "isError": True}


# =============================================================================
# ToolSpec surface (what tests read off `tools.mcp.get_tool(name)`)
# =============================================================================

def test_parameters_is_a_fresh_copy_every_time():
    spec = ToolSpec.from_function(navigate)
    spec.parameters["properties"].clear()
    assert set(spec.parameters["properties"]) == {"url", "proxy", "wait"}
    assert spec.parameters is not spec.parameters


def test_the_wire_entry_has_exactly_name_description_and_input_schema():
    spec = ToolSpec.from_function(navigate)
    assert list(spec.to_wire()) == ["name", "description", "inputSchema"]
    assert wire(spec.to_mcp_tool()) == spec.to_wire()
    assert spec.fn is navigate


# =============================================================================
# The registry
# =============================================================================

def make(name, description="d"):
    async def fn() -> str:
        return name
    return ToolSpec.from_function(fn, name=name, description=description)


async def test_order_is_registration_order_and_put_replaces_in_place():
    reg = ToolRegistry([make("tabs"), make("navigate"), make("server_info")])
    assert reg.names() == ("tabs", "navigate", "server_info")
    assert reg.put(make("tabs", "new")) is True, "replaced"
    assert reg.names() == ("tabs", "navigate", "server_info")
    assert reg.get("tabs").description == "new"
    assert reg.put(make("browsers")) is False, "added"
    assert reg.names() == ("tabs", "navigate", "server_info", "browsers")
    assert reg.remove("browsers") is True and reg.remove("browsers") is False
    reg.put(make("browsers"))
    assert reg.names()[-1] == "browsers", "a tool that comes back is appended last"


async def test_strict_add_and_replace():
    reg = ToolRegistry([make("tabs")])
    with pytest.raises(ValueError):
        reg.add(make("tabs"))
    with pytest.raises(KeyError):
        reg.replace(make("nope"))
    reg.replace(make("tabs", "v2"))
    assert reg.get("tabs").description == "v2" and len(reg) == 1 and "tabs" in reg


async def test_the_fastmcp_named_accessors_and_dispatch():
    reg = ToolRegistry([ToolSpec.from_function(navigate)])
    assert (await reg.get_tool("navigate")).fn is navigate
    assert await reg.get_tool("nope") is None
    assert [t.name for t in await reg.list_tools()] == ["navigate"]
    assert reg.wire_tools() == [ToolSpec.from_function(navigate).to_wire()]
    assert [wire(t) for t in reg.wire_models()] == reg.wire_tools()
    result = await reg.call_tool("navigate", {"url": "u"})
    assert result.content[0].text == "url=u proxy=None wait=True"
    with pytest.raises(UnknownToolError, match=r"^Unknown tool: 'nope'$"):
        await reg.call_tool("nope", {})


def test_readers_never_see_a_half_applied_replace():
    # The WebSocket thread re-registers tabs/navigate while uvicorn's thread serves
    # tools/list: every snapshot must hold every tool, in the same order, each one a
    # complete version.
    names = ("tabs", "navigate", "page_read", "server_info")
    versions = {n: (make(n, f"{n} v1"), make(n, f"{n} v2")) for n in names}
    reg = ToolRegistry([versions[n][0] for n in names])
    stop = threading.Event()
    problems = []

    def writer():
        i = 0
        while not stop.is_set():
            reg.put(versions["tabs"][i % 2])
            reg.put(versions["navigate"][(i + 1) % 2])
            i += 1

    def reader():
        while not stop.is_set():
            snap = reg.snapshot()
            if tuple(s.name for s in snap) != names:
                problems.append(tuple(s.name for s in snap))
            if any(s not in versions[s.name] for s in snap):
                problems.append("foreign spec")
            if reg.get("tabs") is None or reg.get("navigate") is None:
                problems.append("gap")

    threads = [threading.Thread(target=writer) for _ in range(2)] + \
              [threading.Thread(target=reader) for _ in range(4)]
    for t in threads:
        t.start()
    threading.Event().wait(0.5)
    stop.set()
    for t in threads:
        t.join()
    assert problems == []
    assert reg.names() == names


# =============================================================================
# The real tool surface, against the pinned tools/list golden
# =============================================================================

class _Server(FacadeDouble):
    async def send_request_and_wait(self, request, timeout=30.0, browser=None):
        return {}


# How mcp_tools.py registers each tool in the default surface: an explicit description
# (its docstring's first section) plus hidden parameters, or neither.
EXPLICIT = {"tabs": ("proxy",), "navigate": ("proxy",), "page_eval": ("script_name", "script_args")}


async def _real_registry():
    from server.tools.docstrings import parse_function_docstring
    tools = BrowserPlugTools(_Server())
    registry = ToolRegistry()
    for listed in await tools.mcp.list_tools():
        fn = listed.fn
        if listed.name in EXPLICIT:
            spec = ToolSpec.from_function(fn, name=listed.name,
                                          description=parse_function_docstring(fn).description,
                                          hidden=EXPLICIT[listed.name])
        else:
            spec = ToolSpec.from_function(fn, name=listed.name)
        registry.put(spec)
    return registry


async def test_the_real_surface_matches_the_golden_name_description_and_schema():
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))["default"]
    registry = await _real_registry()
    assert list(registry.names()) == golden["order"]
    for entry in golden["tools"]:
        spec = registry.get(entry["name"])
        assert spec.description == entry["description"], entry["name"]
        assert spec.parameters == (entry.get("input_schema") or entry.get("inputSchema")), \
            entry["name"]
    signature = BrowserPlugTools._compute_tools_signature(
        registry.get("tabs").description, registry.get("navigate").description, False, None)
    assert signature == golden["signature"]


@pytest.mark.parametrize("name,arguments,text", [
    ("tabs", {"bogus": 1},
     "1 validation error for call[tabs]\nbogus\n  Unexpected keyword argument "
     "[type=unexpected_keyword_argument, input_value=1, input_type=int]\n"
     "    For further information visit {DOCS}unexpected_keyword_argument"),
    ("server_info", {"params": "{}"},
     "1 validation error for call[server_info]\nparams\n  Unexpected keyword argument "
     "[type=unexpected_keyword_argument, input_value='{}', input_type=str]\n"
     "    For further information visit {DOCS}unexpected_keyword_argument"),
    ("page_read", {"max_chars": "abc"},
     "1 validation error for call[page_read]\nmax_chars\n  Input should be a valid integer, "
     "unable to parse string as an integer [type=int_parsing, input_value='abc', "
     "input_type=str]\n    For further information visit {DOCS}int_parsing"),
    ("page_act", {},
     "1 validation error for call[page_act]\nsteps\n  Missing required argument "
     "[type=missing_argument, input_value={}, input_type=dict]\n"
     "    For further information visit {DOCS}missing_argument"),
    ("page_act", {"steps": [{"target": "#x"}]},
     "1 validation error for call[page_act]\nsteps.0.action\n  Field required "
     "[type=missing, input_value={'target': '#x'}, input_type=dict]\n"
     "    For further information visit {DOCS}missing"),
    ("requests", {"action": "start", "options": "{}"},
     "1 validation error for call[requests]\noptions\n  Input should be a valid dictionary "
     "[type=dict_type, input_value='{}', input_type=str]\n"
     "    For further information visit {DOCS}dict_type"),
    ("navigate", {"url": "https://example.org/", "timeout_ms": 1.5},
     "1 validation error for call[navigate]\ntimeout_ms\n  Input should be a valid integer, "
     "got a number with a fractional part [type=int_from_float, input_value=1.5, "
     "input_type=float]\n    For further information visit {DOCS}int_from_float"),
])
async def test_real_tools_refuse_arguments_with_the_captured_text(name, arguments, text):
    spec = (await _real_registry()).get(name)
    assert refusal(spec, arguments) == text.replace("{DOCS}", DOCS)


@pytest.mark.parametrize("name,arguments", [
    ("tabs", {"action": "open", "url": "https://example.org", "proxy": "socks5://h:1"}),
    ("navigate", {"url": "null", "proxy": "socks5://h:1"}),
    ("page_eval", {"code": "{}", "script_name": "x"}),
    ("page_eval", {"code": "null"}),
    ("page_read", {"max_chars": 100.0}),
    ("page_screenshot", {"full_page": "true"}),
    ("server_info", None),
])
async def test_real_tools_accept_what_fastmcp_accepted(name, arguments):
    (await _real_registry()).get(name).bind(arguments)
