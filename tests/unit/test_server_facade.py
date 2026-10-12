"""The tools-to-server contract (review A3): one Protocol, held to from both ends.

`server/tools/` used to reach the server through `getattr(server, name, default)` calls, so
a rename in `server.py` silently turned a feature off and the test doubles only had to
implement whatever their own tests touched. Now `server_facade.ServerFacade` lists the
members, the tools call them directly, and these tests keep the four parties in step:

  * the Protocol and `FACADE_MEMBERS` name the same members;
  * the real `BrowserPlugServer` and the tests' doubles both satisfy the Protocol;
  * the tools use nothing the Protocol does not list, and every listed member is used
    (so the list stays "exactly what the tools use", not a wish list);
  * no tool module reaches the server with `getattr` again.

A rename fails here, loudly, in the double and in the real server alike.
"""

import ast
import types
from pathlib import Path

import pytest

import test_imports  # noqa: F401
from facade_double import FacadeDouble
from server.server import BrowserPlugServer
from server.tools.server_facade import FACADE_MEMBERS, ServerFacade

SERVER_DIR = Path(__file__).resolve().parents[2] / "server"
TOOL_FILES = sorted((SERVER_DIR / "tools").glob("*.py")) + [SERVER_DIR / "tools" / "mcp_tools.py"]
SERVER_NAMES = {"server", "websocket_server"}


def _protocol_members():
    # The names declared on the protocol. `__protocol_attrs__` is a private attribute that
    # only exists from Python 3.12, so read the class body and annotations instead and drop
    # the machinery typing and abc add (dunders, `_is_protocol`, `_abc_impl`, ...).
    machinery = {"_is_protocol", "_is_runtime_protocol"}
    return {name for name in (*vars(ServerFacade), *getattr(ServerFacade, "__annotations__", {}))
            if not (name.startswith("__") and name.endswith("__"))
            and not name.startswith("_abc_") and name not in machinery}


def test_the_member_tuple_and_the_protocol_agree():
    assert _protocol_members() == set(FACADE_MEMBERS)
    assert len(FACADE_MEMBERS) == len(set(FACADE_MEMBERS))


def test_the_real_server_satisfies_the_protocol():
    server = BrowserPlugServer(host="localhost", port=8772, start_mcp=False)
    missing = [name for name in FACADE_MEMBERS if not hasattr(server, name)]
    assert not missing, f"BrowserPlugServer lost facade members: {missing}"
    assert isinstance(server, ServerFacade)


def test_the_shared_double_satisfies_the_protocol():
    double = FacadeDouble()
    missing = [name for name in FACADE_MEMBERS if not hasattr(double, name)]
    assert not missing, f"FacadeDouble lost facade members: {missing}"
    assert isinstance(double, ServerFacade)


def test_the_page_tool_mock_server_satisfies_the_protocol():
    # The 98-call-site double from test_page_tools: it must implement the same protocol.
    from test_page_tools import MockServer
    double = MockServer()
    missing = [name for name in FACADE_MEMBERS if not hasattr(double, name)]
    assert not missing, f"test_page_tools.MockServer lost facade members: {missing}"
    assert isinstance(double, ServerFacade)


@pytest.mark.parametrize("member", FACADE_MEMBERS)
def test_an_object_missing_one_member_is_not_a_facade(member):
    """The negative control: a rename on one side must fail the isinstance check."""
    double = FacadeDouble()
    everything = {name: getattr(double, name) for name in FACADE_MEMBERS}
    assert isinstance(types.SimpleNamespace(**everything), ServerFacade)
    everything.pop(member)
    assert not isinstance(types.SimpleNamespace(**everything), ServerFacade)


def _server_accesses(tree):
    """(attribute name, line) for every `<x>.server.<name>` / `<x>.websocket_server.<name>`."""
    found = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Attribute)
                and node.value.attr in SERVER_NAMES):
            found.append((node.attr, node.lineno))
    return found


def test_the_tools_use_only_facade_members_and_all_of_them():
    used = {}
    for path in TOOL_FILES:
        for name, line in _server_accesses(ast.parse(path.read_text(encoding="utf-8"))):
            used.setdefault(name, []).append(f"{path.name}:{line}")
    outside = {n: where for n, where in used.items() if n not in FACADE_MEMBERS}
    assert not outside, f"tools use server members that are not in ServerFacade: {outside}"
    unused = set(FACADE_MEMBERS) - set(used)
    assert not unused, f"ServerFacade lists members no tool uses: {sorted(unused)}"


def test_no_tool_module_reads_the_server_with_getattr():
    offenders = []
    for path in TOOL_FILES:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id in ("getattr", "hasattr") and node.args
                    and isinstance(node.args[0], ast.Attribute)
                    and node.args[0].attr in SERVER_NAMES):
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, f"getattr on the server object came back: {offenders}"


# =============================================================================
# The doubles' send_request_and_wait is held to the real one (review Q4)
# =============================================================================

class _Answering(FacadeDouble):
    async def send_request_and_wait(self, request, timeout=30.0, browser=None):
        return {"type": "response", "data": {"ok": True}}


def _request(action, request_id="r1"):
    return {"id": request_id, "type": "request", "action": action, "data": {}}


@pytest.mark.asyncio
async def test_a_fake_answers_a_handled_action_with_an_id():
    assert (await _Answering().send_request_and_wait(_request("tabs.list")))["data"]["ok"]


@pytest.mark.asyncio
async def test_a_fake_rejects_an_action_no_extension_handles():
    with pytest.raises(AssertionError, match="tabs.lsit"):
        await _Answering().send_request_and_wait(_request("tabs.lsit"))


@pytest.mark.asyncio
@pytest.mark.parametrize("request_id", [None, ""])
async def test_a_fake_requires_the_request_id_the_real_method_requires(request_id):
    with pytest.raises(ValueError, match="must have an ID"):
        await _Answering().send_request_and_wait(_request("tabs.list", request_id))


def test_a_fake_with_a_different_signature_fails_at_class_definition():
    with pytest.raises(TypeError, match="send_request_and_wait takes"):
        class _Drifted(FacadeDouble):  # noqa: F841
            async def send_request_and_wait(self, request, timeout=30.0, **kwargs):
                return {}
