"""Hardening of the extension RPC and the browser registry (review C10-C13).

C10  a frame from a socket that is no longer registered is not answered on the default
     browser's socket and does not run server.* actions.
C11  a browser's identity lives in several structures; register/promote/displace/remove
     must move or drop all of them (no orphan `pending-N` keys anywhere).
C12  swallowed errors are logged with a traceback, a failed request-bearing frame is
     answered, and handle_response pops atomically.
C13  a raise between register and the removal `finally` must not leave a zombie record.
"""

import asyncio
import json
import logging
from unittest.mock import patch

import pytest

import test_imports  # noqa: F401
from test_browser_registry import (FakeWS, connect, disconnect, hello, send, server,  # noqa: F401
                                   wait_until)


def _sent_ids(ws):
    return [m.get("id") for m in ws.sent]


def _frame(action, msg_id, data=None):
    return json.dumps({"id": msg_id, "type": "request", "action": action,
                       "data": data or {}})


def _pending_keys(server):
    """Every `pending-N` key still present in any identity structure, by structure."""
    reg = server.registry
    found = {}
    for name, keys in (
            ("browsers", reg.browsers), ("slots", reg.slots),
            ("pending_owners", server.rpc.pending_owners.values()),
            ("gates", server.request_queue._gates)):
        bad = [k for k in keys if str(k).startswith("pending-")]
        if bad:
            found[name] = bad
    return found


# --- C10 -------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_c10_unregistered_socket_gets_no_reply_on_the_default_browser(server):
    default_ws, stray_ws = FakeWS(), FakeWS(peer=("127.0.0.1", 7777))
    task = await connect(server, default_ws)
    await hello(server, default_ws, instanceId="ff-a")
    default_ws.sent.clear()

    # stray_ws never registered (or was removed): its frames must not be answered anywhere.
    for i, action in enumerate(("server.get_connections", "server.health",
                                "server.get_default_browser")):
        await server.handle_extension_message(_frame(action, f"stray-{i}"), stray_ws)

    assert [i for i in _sent_ids(default_ws) if str(i).startswith("stray-")] == []
    assert stray_ws.sent == []
    await disconnect(default_ws, task)


@pytest.mark.asyncio
async def test_c10_unregistered_socket_cannot_restart_or_set_the_default(server):
    default_ws, stray_ws = FakeWS(), FakeWS(peer=("127.0.0.1", 7777))
    task = await connect(server, default_ws)
    await hello(server, default_ws, instanceId="ff-a")

    with patch.object(server, "_begin_restart") as begin, \
            patch.object(server, "set_default_browser") as setter:
        await server.handle_extension_message(_frame("server.restart", "r-1"), stray_ws)
        await server.handle_extension_message(
            _frame("server.set_default_browser", "s-1", {"instance_id": "ff-a"}), stray_ws)

    begin.assert_not_called()
    setter.assert_not_called()
    await disconnect(default_ws, task)


@pytest.mark.asyncio
async def test_c10_registered_socket_is_still_answered(server):
    ws = FakeWS()
    task = await connect(server, ws)
    await hello(server, ws, instanceId="ff-a")
    ws.sent.clear()
    await server.handle_extension_message(_frame("server.health", "h-1"), ws)
    assert "h-1" in _sent_ids(ws)
    await disconnect(ws, task)


# --- C11 -------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_c11_rekey_moves_the_gate_and_owners_off_the_pending_key(server):
    ws = FakeWS()
    task = await connect(server, ws)
    provisional = server.default_browser
    call = send(server, "req-1", provisional, action="tabs.close")
    await wait_until(lambda: server.rpc.pending_owners, "request registered")
    assert "gates" in _pending_keys(server)  # the gate exists under the provisional key

    await hello(server, ws, instanceId="ff-a")

    assert _pending_keys(server) == {}
    assert "ff-a" in server.request_queue.snapshot()
    await disconnect(ws, task)
    await call


@pytest.mark.asyncio
async def test_c11_merge_carries_encrypted_and_drops_the_pending_gate(server):
    first = FakeWS()
    t1 = await connect(server, first)
    await hello(server, first, instanceId="ff-a")
    assert server.browsers["ff-a"].encrypted is False

    # The established socket is dead but its handler has not torn the record down yet:
    # the window in which a reconnect's hello MERGES into the established record.
    first.close_code = 1006
    second = FakeWS(peer=("127.0.0.1", 6666))
    second.encrypted = True
    t2 = await connect(server, second)
    provisional = [b for b in server.registry.by_slot() if b.provisional][0]
    call = send(server, "req-2", provisional, action="tabs.close")
    await wait_until(lambda: server.rpc.pending_owners, "request registered")

    await hello(server, second, instanceId="ff-a")

    assert server.browsers["ff-a"].ws is second
    assert server.browsers["ff-a"].encrypted is True
    assert _pending_keys(server) == {}
    assert server.rpc.pending_owners == {"req-2": "ff-a"}
    await disconnect(second, t2)
    await call
    first.drop()
    await asyncio.wait_for(t1, timeout=2.0)


@pytest.mark.asyncio
async def test_c11_remove_leaves_no_orphans_for_a_browser_that_never_said_hello(server):
    ws = FakeWS()
    task = await connect(server, ws)
    provisional = server.default_browser
    call = send(server, "req-3", provisional, action="tabs.close")
    await wait_until(lambda: server.rpc.pending_owners, "request registered")

    await disconnect(ws, task)
    await call

    assert _pending_keys(server) == {}
    assert server.rpc.pending_owners == {}


@pytest.mark.asyncio
async def test_c11_walk_every_structure_after_each_lifecycle_step(server):
    """register -> promote -> displace -> remove, asserting after each step that no
    structure names a key the browsers table does not hold (departed excepted: it exists to
    outlive the record)."""
    def consistent():
        keys = set(server.browsers)
        stray = {k for k in server.rpc.pending_owners.values() if k not in keys}
        stray |= {k for k in server.request_queue._gates
                  if k not in keys and str(k).startswith("pending-")}
        stray |= {k for k in server.registry.slots if str(k).startswith("pending-")}
        return stray

    ws = FakeWS()
    task = await connect(server, ws)
    assert consistent() == set()                       # register
    await hello(server, ws, instanceId="ff-a")
    assert consistent() == set()                       # promote
    ws2 = FakeWS(peer=("127.0.0.1", 6001))
    t2 = await connect(server, ws2)
    await hello(server, ws2, instanceId="ff-a")
    assert consistent() == set()                       # displace via merge
    await disconnect(ws2, t2)
    assert consistent() == set()                       # remove
    await disconnect(ws, task)


# --- C12 -------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_c12_send_admitted_logs_the_swallowed_exception(server, caplog):
    ws = FakeWS()
    task = await connect(server, ws)
    await hello(server, ws, instanceId="ff-a")

    async def boom(*a, **k):
        raise RuntimeError("kaboom")

    with patch.object(server.rpc, "send_to_extension", boom), \
            caplog.at_level(logging.WARNING):
        result = await server.send_request_and_wait(
            {"id": "x-1", "type": "request", "action": "tabs.list", "data": {}},
            timeout=1.0, browser=server.browsers["ff-a"])

    assert result == {"error": "Request failed: kaboom"}
    assert any(r.exc_info and r.levelno >= logging.WARNING for r in caplog.records)
    assert server.rpc.pending_requests == {}
    await disconnect(ws, task)


@pytest.mark.asyncio
async def test_c12_a_failing_handler_logs_a_traceback_and_answers_the_request(server, caplog):
    ws = FakeWS()
    task = await connect(server, ws)
    await hello(server, ws, instanceId="ff-a")
    ws.sent.clear()

    with patch.object(server, "get_connections_info", side_effect=ValueError("bad report")), \
            caplog.at_level(logging.ERROR):
        await server.handle_extension_message(_frame("server.get_connections", "gc-1"), ws)

    assert any(r.exc_info for r in caplog.records)
    replies = [m for m in ws.sent if m.get("id") == "gc-1"]
    assert len(replies) == 1
    assert replies[0]["type"] == "error"
    assert "bad report" in replies[0]["data"]["message"]
    await disconnect(ws, task)


@pytest.mark.asyncio
async def test_c12_handle_response_survives_losing_the_pop_race(server):
    """The timeout path pops the future between the membership check and the pop."""
    fut = asyncio.get_running_loop().create_future()
    server.rpc.pending_requests["race-1"] = fut
    with patch.object(server.rpc, "pop_pending", return_value=None):
        await server.rpc.handle_response({"id": "race-1", "type": "response", "data": {}})
    assert not fut.done()


# --- C13 -------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_c13_a_raise_after_register_does_not_leave_a_zombie(server):
    ws = FakeWS()
    with patch.object(server, "_notify_connection_waiters",
                      side_effect=asyncio.InvalidStateError("boom")):
        task = await connect(server, ws)
        await asyncio.wait_for(task, timeout=2.0)
    assert server.browsers == {}
    assert server.default_browser is None


@pytest.mark.asyncio
async def test_c13_one_bad_waiter_does_not_starve_the_rest(server):
    loop = asyncio.get_running_loop()
    done, live = loop.create_future(), loop.create_future()
    done.set_result(True)            # set_result on it raises InvalidStateError
    server._connection_waiters.extend([done, live])
    server._notify_connection_waiters()
    assert live.done() and live.result() is True
    assert server._connection_waiters == []
