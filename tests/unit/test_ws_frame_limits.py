"""Frame-size limits on the extension WebSocket (review findings S9 and T10).

S9: an unauthenticated socket could buffer ~32 MiB (16 held + 16 queued frames of 1 MiB)
for the whole hello window, and nothing limited how many such sockets were open.
T10: websockets' default 1 MiB inbound limit sat under legitimate replies (a captured body
is up to 1.33 MB of base64, a tall screenshot more), so one oversize reply closed the
socket with 1009 for every client.

These run over real sockets through ServerLifecycle.start_server, because the limits are
serve() arguments and the thing to prove is that the glue passes them.
"""
import asyncio
import json
import socket

import pytest
import websockets

from secure_ext import SecureClient, device_token
from server.server import BrowserPlugServer

MIB = 1024 * 1024


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _wait_until(predicate, what, timeout=8.0):
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            raise AssertionError(f"timed out after {timeout}s waiting until {what}")
        await asyncio.sleep(0.02)


class _Running:
    """A real server on a free port; `strict` makes even loopback peers authenticate."""

    def __init__(self, strict):
        self.port = _free_port()
        self.server = BrowserPlugServer(host="localhost", port=self.port, start_mcp=False)
        if strict:
            self.server.ws_token_explicit = True
        self.task = None
        self.url = f"ws://127.0.0.1:{self.port}"

    async def __aenter__(self):
        self.task = asyncio.create_task(self.server.start_server())
        await _wait_until(lambda: self.server.lifecycle.websocket_server is not None,
                          "the listener is up")
        return self

    async def __aexit__(self, *exc):
        self.server.lifecycle.websocket_server.close()
        await self.server.lifecycle.websocket_server.wait_closed()
        self.task.cancel()
        try:
            await self.task
        except BaseException:
            pass


def _hello(instance_id="ff-a"):
    return {"type": "extension_hello",
            "data": {"version": "1.9.1", "instance_id": instance_id}}


@pytest.mark.asyncio
async def test_an_authenticated_reply_over_one_mib_does_not_kill_the_connection():
    """T10: a 2 MB reply (a captured body's base64 is 1.33 MB) must arrive."""
    async with _Running(strict=True) as run:
        seen = []

        async def record(message, websocket=None):
            seen.append(len(message) if isinstance(message, str) else -1)
        run.server.handle_extension_message = record
        async with websockets.connect(run.url, max_size=None) as sock:
            client = await SecureClient.connect(sock, device_token(run.server), _hello())
            await _wait_until(lambda: len(seen) >= 1, "the hello was handled")
            await client.send({"type": "response", "id": "x", "data": "A" * (2 * MIB)})
            await _wait_until(lambda: len(seen) >= 2, "the 2 MB reply was handled")
            assert sock.close_code is None, "the socket must still be open"
            assert seen[-1] > 2 * MIB


@pytest.mark.asyncio
async def test_an_unauthenticated_frame_of_200_kib_is_refused_not_buffered():
    """S9: nothing legitimate is bigger than a few KiB before the hello is accepted."""
    async with _Running(strict=True) as run:
        async with websockets.connect(run.url, max_size=None) as sock:
            await sock.send("x" * (200 * 1024))
            with pytest.raises(websockets.exceptions.ConnectionClosed) as closed:
                await asyncio.wait_for(sock.recv(), timeout=5)
        assert closed.value.rcvd.code == 1009


@pytest.mark.asyncio
async def test_only_a_bounded_number_of_silent_sockets_are_held_open():
    """S9: sockets that never send a hello are capped per peer and in total."""
    async with _Running(strict=True) as run:
        socks = []
        try:
            for _ in range(40):
                socks.append(await websockets.connect(run.url))
            await asyncio.sleep(0.5)
            refused = [s for s in socks if s.close_code is not None or s.state.name != "OPEN"]
            open_ = len(socks) - len(refused)
            assert open_ <= 8, f"{open_} unauthenticated sockets held open"
            codes = {s.close_code for s in refused if s.close_code is not None}
            assert 1013 in codes, codes
        finally:
            await asyncio.gather(*(s.close() for s in socks), return_exceptions=True)


@pytest.mark.asyncio
async def test_a_slot_frees_when_an_unauthenticated_socket_leaves():
    async with _Running(strict=True) as run:
        for _ in range(3):  # far more than the cap over time, never at once
            for _ in range(8):
                s = await websockets.connect(run.url)
                await s.close()
        async with websockets.connect(run.url) as sock:
            await sock.send(json.dumps(_hello()))
            with pytest.raises(websockets.exceptions.ConnectionClosed) as closed:
                await asyncio.wait_for(sock.recv(), timeout=5)
        assert closed.value.rcvd.code == 4401, "a normal rejection, not the capacity refusal"


@pytest.mark.asyncio
async def test_an_oversize_close_tells_the_waiting_caller_why():
    """The 1009 close must fail the in-flight request with a readable reason."""
    async with _Running(strict=True) as run:
        from server.ws import ws_limits
        async with websockets.connect(run.url, max_size=None) as sock:
            await SecureClient.connect(sock, device_token(run.server), _hello("ff-big"))
            await _wait_until(lambda: "ff-big" in run.server.browsers, "the browser registered")
            fut = asyncio.get_event_loop().create_future()
            run.server.rpc.pending_requests["rid-1"] = fut
            run.server.rpc.pending_owners["rid-1"] = "ff-big"
            await sock.send(b"\x00" * (ws_limits.POST_AUTH_MAX_MESSAGE_BYTES + 1024))
            result = await asyncio.wait_for(fut, timeout=8)
        message = result["error"]
        assert "too large" in message and "MiB" in message, message
        assert "closed, restarted" not in message, "not the generic disconnect text"


def test_the_gate_counts_per_peer_and_in_total_and_frees_slots():
    from server.ws.ws_limits import PreauthGate
    gate = PreauthGate(per_peer=2, total=3)
    assert gate.try_acquire("a") and gate.try_acquire("a")
    assert not gate.try_acquire("a"), "per-peer cap"
    assert gate.try_acquire("b")
    assert not gate.try_acquire("c"), "total cap"
    gate.release("a")
    assert gate.try_acquire("c")
    for peer in ("a", "b", "c"):
        gate.release(peer)
    gate.release("a")  # a stray extra release never goes negative
    assert gate.held() == 0


def test_raise_limit_reaches_through_the_secure_wrapper_and_tolerates_doubles():
    from types import SimpleNamespace
    from server.ws import ws_limits
    proto = SimpleNamespace(max_message_size=ws_limits.PRE_AUTH_MAX_MESSAGE_BYTES)
    ws_limits.raise_limit(SimpleNamespace(raw=SimpleNamespace(protocol=proto)))
    assert proto.max_message_size == ws_limits.POST_AUTH_MAX_MESSAGE_BYTES
    ws_limits.raise_limit(object())  # a test double with no protocol: no error


def test_the_limits_fit_the_largest_legitimate_payloads():
    from server.ws import ws_limits
    assert ws_limits.PRE_AUTH_MAX_MESSAGE_BYTES >= 4096  # request, proof, health probe, hello
    assert ws_limits.POST_AUTH_MAX_MESSAGE_BYTES >= 2 * 1_000_000 * 4 // 3  # 1 MB body, base64
