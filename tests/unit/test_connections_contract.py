"""The REAL get_connections_info() against the cross-language payload contract.

This is the Python half of a loop that had no Python half at all. The options page's
connection map and the popup's Connections panel are both rendered from
`get_connections_info()`, and both are unit-tested in JS (tests/js/options-map.test.mjs,
tests/js/popup.test.mjs) against payloads FROZEN BY HAND. A hand-frozen payload cannot
notice that the server renamed a key: the JS tests stay green — they are asserting against
their own copy — while the real map loses its launcher hop and the real panel loses its
rows. Nothing anywhere compared the two languages.

tests/fixtures/connections_info_contract.json is now the one description of the payload.
Here we build a server with a mocked registry / client table, call the REAL emitter, and
assert its ACTUAL output conforms. The JS side validates its fixtures against the same
file (tests/js/contract.mjs). So:

  * rename a key in server.py -> this test fails (the contract still wants the old name);
  * fix the contract to match  -> the JS tests fail (their fixtures still spell it the old
    way, and an unknown key is fatal there).

There is no longer a way to rename a payload key and have every suite pass without
touching the renderers' fixtures.
"""

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from server.reporting import client_identity
from server.browsers.browser_identity import ExtBrowser
from server.server import BrowserPlugServer

CONTRACT = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures" / "connections_info_contract.json")
    .read_text(encoding="utf-8")
)


# --- the contract's type vocabulary, Python side -------------------------------------
# bool is an int in Python; a payload that says `"pid": True` must NOT pass as an int.
_CHECKS = {
    "string": lambda v: isinstance(v, str),
    "int": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "bool": lambda v: isinstance(v, bool),
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "null": lambda v: v is None,
}


def _describe(v):
    return "null" if v is None else type(v).__name__


def conform(value, object_name, path=None, require_all=True):
    """Assert `value` conforms to contract.objects[object_name]. Returns the keys checked.

    require_all: every `required` key must be present (what we hold the real emitter to).
    Unknown keys are always fatal — an emitter that grew a key nobody documented is
    exactly as invisible to the JS side as one that renamed a key.
    """
    path = path or object_name
    spec = CONTRACT["objects"].get(object_name)
    assert spec, f"no {object_name!r} object in the contract"
    assert isinstance(value, dict), f"{path}: expected an object, got {_describe(value)}"

    required = spec.get("required", {})
    optional = spec.get("optional", {})
    known = {**required, **optional}

    for key in value:
        assert key in known, (
            f"{path}.{key} is emitted but the contract does not know it. Either add it to "
            f"tests/fixtures/connections_info_contract.json ({object_name}) and to the JS "
            f"fixtures that render it, or it should not be on the wire."
        )

    for key, allowed in known.items():
        if key not in value:
            assert not (require_all and key in required), (
                f"{path}.{key} is missing from the real payload — the contract requires it "
                f"of every {object_name}. If server.py renamed or dropped it, the options "
                f"map / popup fixtures need the same edit."
            )
            continue
        for t in allowed:
            assert t in _CHECKS, f"contract uses unknown type {t!r}"
        assert any(_CHECKS[t](value[key]) for t in allowed), (
            f"{path}.{key} is {_describe(value[key])} ({value[key]!r}); "
            f"the contract allows {' | '.join(allowed)}"
        )

    for key, arr_spec in spec.get("arrays", {}).items():
        items = value.get(key)
        if not isinstance(items, list):
            continue
        for i, item in enumerate(items):
            item_path = f"{path}.{key}[{i}]"
            target = arr_spec["of"]
            if "discriminator" in arr_spec:
                tag = item.get(arr_spec["discriminator"]) if isinstance(item, dict) else None
                assert tag in target, (
                    f"{item_path}: {arr_spec['discriminator']}={tag!r} is not a kind the "
                    f"contract knows ({', '.join(target)})"
                )
                target = target[tag]
            conform(item, target, item_path, require_all)

    for key, child in spec.get("children", {}).items():
        if value.get(key) is None:
            continue
        conform(value[key], child, f"{path}.{key}", require_all)

    return set(known)


# --- a server with a mocked registry / client table ----------------------------------

class FakeWS:
    """A live socket: ExtBrowser.connected is `close_code is None`."""

    def __init__(self, peer=("127.0.0.1", 5555)):
        self.remote_address = peer
        self.close_code = None


def _browser(slot, instance_id, *, remote=False, **fields):
    ws = FakeWS()
    b = ExtBrowser(ws, instance_id, slot,
                   remote_addr="192.168.0.7" if remote else "127.0.0.1",
                   is_remote=remote)
    b.provisional = False
    for k, v in fields.items():
        setattr(b, k, v)
    return b


@pytest.fixture
def server():
    """A server whose whole connected world is mocked: two browsers (one local, one
    remote), one MCP client behind a launcher with two sessions, and a completed call.

    describe_pid is the only psutil dependency on this path; stub it so the row is
    deterministic and the test never depends on what the runner's own process looks like.
    """
    srv = BrowserPlugServer(host="localhost", port=8772, start_mcp=False)
    srv.mcp_port = 2830

    now = datetime.now()
    srv.browsers = {
        "ff-local": _browser(
            1, "ff-local",
            version="1.4.20", browser_pid=111,
            exe_path=r"C:\Program Files\Firefox Developer Edition\firefox.exe",
            proc_started=now - timedelta(hours=1),
            brand="Firefox", friendly="Firefox Developer Edition",
            last_action="tabs.list", last_activity=now, last_seen=now,
            browser_info={"name": "Firefox", "version": "152.0"},
        ),
        # A REMOTE browser: no pid, no exe, no process start — every one of
        # those is a nullable the contract has to allow, and the row still has to be whole.
        "ff-remote": _browser(2, "ff-remote", remote=True, connect_count=3),
    }

    srv.http_clients = {
        "abc12345-session": {
            "pid": 777,
            "friendly": "Claude Code",
            "cwd": r"C:\dr\browserplug",
            "exe_path": r"C:\claude\claude.exe",
            "start_time": now - timedelta(minutes=30),
            "launcher_pid": 888,
            "launcher_start_time": now - timedelta(minutes=30, seconds=2),
            "last_activity": now,
            "history": [{"time": now, "tool": "tabs", "args": {"action": "list"}}],
        },
        # Same process, second session — get_connections_info collapses these into ONE row.
        "def67890-session": {
            "pid": 777,
            "friendly": "Claude Code",
            "cwd": r"C:\dr\browserplug",
            "exe_path": r"C:\claude\claude.exe",
            "start_time": now - timedelta(minutes=30),
            "launcher_pid": 888,
            "launcher_start_time": now - timedelta(minutes=30, seconds=2),
            "last_activity": now - timedelta(seconds=1),
            "history": [],
        },
    }

    with patch.object(client_identity, "describe_pid", return_value={
            "pid": os.getpid(), "friendly": "python", "exe_name": "python.exe",
            "exe_path": r"C:\dr\browserplug\.venv\Scripts\python.exe",
            "cwd": r"C:\dr\browserplug", "start_time": now - timedelta(hours=1)}):
        yield srv


def _browser_call(server, *, acked=True):
    """The state the server holds after a tools/call that DID drive a browser."""
    t0 = datetime.now()
    server.telemetry.last_command = {
        "tool": "tabs", "action": "tabs.list", "params": {"action": "list"},
        "at": t0, "client_pid": 777, "session": "abc12345",
        "acknowledged": acked,
        "acknowledged_at": t0 + timedelta(milliseconds=85) if acked else None,
    }
    server.telemetry.mcp_call = {
        "tool": "tabs", "params": {"action": "list"}, "session": "abc12345",
        "client_pid": 777, "launcher_pid": 888,
        "recv_at": t0, "recv_epoch": t0.timestamp(),
        "ws_sent_at": t0 + timedelta(milliseconds=3),
        "ws_ack_at": t0 + timedelta(milliseconds=88),
        "http_done_at": t0 + timedelta(milliseconds=90),
        "browser_slot": 1,
        "launcher": {"launcher_pid": 888, "total_ms": 132.4},
    }


def _server_side_call(server):
    """A tools/call that never touched a browser (browsers, server_info): the OTHER
    last_command shape — action/ack null, browser-leg timings null."""
    t0 = datetime.now()
    server.telemetry.last_command = None
    server.telemetry.mcp_call = {
        "tool": "browsers", "params": {"action": "list"}, "session": "abc12345",
        "client_pid": 777, "launcher_pid": 888,
        "recv_at": t0, "recv_epoch": t0.timestamp(),
        "ws_sent_at": None, "ws_ack_at": None,
        "http_done_at": t0 + timedelta(milliseconds=2, microseconds=500),
        "browser_slot": None,
        "launcher": {"launcher_pid": 888, "total_ms": 9.0},
    }


# --- the contract itself is not allowed to be vacuous --------------------------------

def test_the_contract_describes_every_row_kind_the_emitter_can_produce():
    """A contract with no teeth passes everything. Pin what it must actually cover, so
    deleting an object from it (rather than fixing a rename) fails here first."""
    objects = CONTRACT["objects"]
    for name in ("payload", "server_row", "extension_row", "mcp_client_row",
                 "session", "command", "extension_instance", "last_command", "timings"):
        assert name in objects, f"the contract lost its {name!r} object"
        assert objects[name].get("required"), f"{name} declares no required keys"

    kinds = objects["payload"]["arrays"]["connections"]["of"]
    assert set(kinds) == {"server", "extension", "mcp_client"}, (
        "get_connections_info() emits exactly these three row kinds"
    )


# --- the real emitter, held to it ------------------------------------------------------

def test_real_payload_conforms_with_a_browser_command_in_flight_history(server):
    """The full shape: server row + 2 browsers + 1 collapsed client + a WS-backed
    last_command carrying its timing breakdown."""
    _browser_call(server)
    payload = server.get_connections_info()

    conform(payload, "payload")

    # The conformance walk is only as honest as the payload it walked: prove it really
    # saw every branch (an empty connections list would "conform" vacuously).
    kinds = [c["kind"] for c in payload["connections"]]
    assert kinds.count("server") == 1
    assert kinds.count("extension") == 2, "one local + one remote browser"
    assert kinds.count("mcp_client") == 1, "two sessions of one process collapse to one row"
    assert payload["count"] == len(payload["connections"]) == 4
    assert len(payload["extension_instances"]) == 2
    assert payload["last_command"]["timings"]["complete"] is True
    assert payload["last_command"]["browser_slot"] == 1


def test_real_payload_conforms_for_a_server_side_only_call(server):
    """The other last_command branch: no WS command, so action/ack and the browser-leg
    timings are null — the contract must allow that WITHOUT the keys going missing."""
    _server_side_call(server)
    payload = server.get_connections_info()

    conform(payload, "payload")

    lc = payload["last_command"]
    assert lc["tool"] == "browsers"
    assert lc["action"] is None and lc["acknowledged_by_extension"] is None
    assert lc["timings"]["browser_ms"] is None, "a call with no browser leg reports null"
    assert lc["timings"]["server_dispatch_ms"] is None
    assert lc["timings"]["server_ms"] is not None, "its whole server stay is still measured"


def test_real_payload_conforms_with_nothing_connected():
    """The empty server: no browsers, no clients, no command. Every top-level key must
    still be there (the map reads them unconditionally) — `last_command` NULL, not absent."""
    srv = BrowserPlugServer(host="localhost", port=8772, start_mcp=False)
    with patch.object(client_identity, "describe_pid", return_value={
            "pid": 1, "friendly": None, "exe_name": None, "exe_path": None,
            "cwd": None, "start_time": None}):
        payload = srv.get_connections_info()

    conform(payload, "payload")
    assert [c["kind"] for c in payload["connections"]] == ["server"]
    assert payload["last_command"] is None
    assert payload["extension_instances"] == []


def test_a_disconnected_browser_leaves_the_rows_but_stays_in_the_registry(server):
    """extension_instances is the registry (currently_connected says which are live);
    `connections` is only the live ones. Both shapes still have to conform."""
    server.browsers["ff-remote"].ws.close_code = 1006
    payload = server.get_connections_info()

    conform(payload, "payload")
    assert [c["slot"] for c in payload["connections"] if c["kind"] == "extension"] == [1]
    assert [i["currently_connected"] for i in payload["extension_instances"]] == [True, False]


def test_kilo_provider_model_are_optional_extras_not_a_shape_change(server):
    """The Kilo-only provider/model badge is additive: present when detected, ABSENT on a
    miss. Both have to conform — an optional key must never become a required one."""
    server.http_clients["abc12345-session"]["friendly"] = "Kilo CLI"
    server.http_clients["def67890-session"]["friendly"] = "Kilo CLI"

    # Stand the detector in for it rather than depending on Kilo's real logs being on
    # this box. The facade calls it through the module attribute, so patching it there
    # is seen by every caller.
    from server.usage_panels import kilocode_data, usage_panels
    usage_panels._reset()
    with patch.object(kilocode_data, "detect_provider_model",
                      lambda pid: {"provider": "anthropic", "model": "claude-opus-4-8"}):
        payload = server.get_connections_info(kilo_details=True)
        conform(payload, "payload")
        client = next(c for c in payload["connections"] if c["kind"] == "mcp_client")
        assert client["provider"] == "anthropic" and client["model"] == "claude-opus-4-8"

        # …and with the option off, the keys are simply not there.
        payload = server.get_connections_info(kilo_details=False)
        conform(payload, "payload")
        client = next(c for c in payload["connections"] if c["kind"] == "mcp_client")
        assert "provider" not in client and "model" not in client


def test_copilot_details_are_an_optional_extra_that_conforms(server, tmp_path, monkeypatch):
    """GitHub Copilot Chat rows gain `copilot`, the provider/model badge and the window's
    workspace, under the same gate as Kilo's. The nested object is the REAL detector's
    output over a fake extension host, so a key it grows or drops fails here against
    copilot_session rather than in the popup."""
    from server.usage_panels import copilot_data, handle_scan

    for session in server.http_clients.values():
        session["friendly"] = "GitHub Copilot Chat"
    log = tmp_path / "GitHub Copilot Chat.log"
    log.write_bytes(b"2026-09-29 16:52:10.468 [info] ccreq:0000eeee.copilotmd | success | "
                    b"chat-model-b | 4342ms | [panel/editAgent]\n")

    # The extension host's open files, as handle_scan would have scanned them.
    scan = handle_scan.Scan((str(log),), r"c:\work\proj")
    monkeypatch.setattr(handle_scan, "lookup", lambda pid, workspace=False: scan)
    from server.usage_panels import usage_panels
    usage_panels._reset()
    copilot_data._cache.clear()
    try:
        payload = server.get_connections_info(kilo_details=True)
        conform(payload, "payload")
        client = next(c for c in payload["connections"] if c["kind"] == "mcp_client")
        assert set(client["copilot"]) == set(
            CONTRACT["objects"]["copilot_session"]["required"])
        assert client["copilot"]["requests"] == 1
        assert client["provider"] == "github-copilot" and client["model"] == "chat-model-b"
        assert client["cwd"] == r"c:\work\proj"

        payload = server.get_connections_info(kilo_details=False)
        conform(payload, "payload")
        client = next(c for c in payload["connections"] if c["kind"] == "mcp_client")
        assert "copilot" not in client and "provider" not in client
    finally:
        copilot_data._cache.clear()


def test_requester_slot_is_the_relays_addition_not_the_emitters(server):
    """The options map's `.self` marker. get_connections_info() must NOT invent it —
    only the WS relay (server.py's `server.get_connections` handler) knows which socket
    asked, and the /health fallback has to synthesize it as null. The contract lists it
    optional for exactly that reason; pin the split so it cannot drift."""
    payload = server.get_connections_info()
    assert "requester_slot" not in payload
    assert "requester_slot" in CONTRACT["objects"]["payload"]["optional"], (
        "the JS fixtures carry requester_slot — the contract must know the key"
    )


# --- does the contract actually bite? --------------------------------------------------

def test_the_contract_catches_a_renamed_key(server):
    """The whole point, exercised. This is the blind spot that existed until now: the JS
    tests could not see a Python rename. Simulate one (on the emitter's OUTPUT — never on
    disk) and prove conform() fails, naming the key both ways."""
    _browser_call(server)
    payload = server.get_connections_info()
    conform(payload, "payload")  # precondition: it passes untouched

    client = next(c for c in payload["connections"] if c["kind"] == "mcp_client")
    client["proxy_pid"] = client.pop("launcher_pid")   # as if server.py had renamed it

    with pytest.raises(AssertionError) as e:
        conform(payload, "payload")
    assert "proxy_pid" in str(e.value), "the unknown new name is reported"

    # And the mirror image: a key silently dropped.
    payload = server.get_connections_info()
    payload["connections"][0].pop("activity_epoch")
    with pytest.raises(AssertionError, match="activity_epoch"):
        conform(payload, "payload")


def test_the_contract_catches_a_retyped_key(server):
    """A rename is not the only silent break: /health and the map do epoch arithmetic in
    server clock, so an epoch that turned into a formatted string is just as fatal."""
    _browser_call(server)
    payload = server.get_connections_info()
    payload["generated_epoch"] = "12:00:02"
    with pytest.raises(AssertionError, match="generated_epoch"):
        conform(payload, "payload")

    payload = server.get_connections_info()
    payload["last_command"]["timings"]["complete"] = "yes"
    with pytest.raises(AssertionError, match="complete"):
        conform(payload, "payload")
