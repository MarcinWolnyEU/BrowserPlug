"""The write side of the two-loop topology (review finding C1).

Tool coroutines run on uvicorn's loop; every extension socket, its transport and
SecureSocket's asyncio.Lock belong to the WebSocket loop. `send_to_extension` is the one
funnel that writes, so it must hop onto the WebSocket loop, and the record layer's counter
must not depend on which thread got there first.
"""

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest

from server.rpc.extension_rpc import ExtensionRpc
from server.browsers.request_queue import RequestQueue
from server.ws.ws_crypto import RecordLayer, SecureSocket


class WsLoopThread:
    """A running event loop in its own thread, standing in for the WebSocket loop."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.ready = threading.Event()
        self.thread.start()
        assert self.ready.wait(5)

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.call_soon(self.ready.set)
        self.loop.run_forever()

    def run(self, coro, timeout=10):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def stop(self):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)
        self.loop.close()


class SlowRawSocket:
    """The websockets connection: send takes a while, and remembers which loop ran it."""

    def __init__(self, delay=0.3):
        self.delay = delay
        self.frames = []
        self.send_loops = []

    async def send(self, data):
        self.send_loops.append(asyncio.get_running_loop())
        await asyncio.sleep(self.delay)
        self.frames.append(data)


@pytest.fixture
def ws_thread():
    t = WsLoopThread()
    yield t
    t.stop()


def make_rpc(ws_loop):
    return ExtensionRpc(
        request_queue=RequestQueue(),
        default_browser=lambda: None,
        browser_by_instance_id=lambda _id: None,
        note_command_sent=lambda request: {},
        note_browser_activity=lambda browser, action: None,
        current_mcp_call=lambda: None,
        ws_loop=lambda: ws_loop)


def encrypted_target(raw):
    key_a, key_b = b"a" * 32, b"b" * 32
    # SecureSocket builds its asyncio.Lock here, off any running loop, like the server does
    # (the lock binds to the first loop that contends it).
    ws = SecureSocket(raw, RecordLayer(send_key=key_a, recv_key=key_b))
    return SimpleNamespace(ws=ws, instance_id="fx-1")


class TestSendGoesThroughTheWsLoop:

    async def test_contended_send_from_the_caller_loop_succeeds(self, ws_thread):
        """A first send from the WS loop contends the lock; a second one from the tool loop
        used to raise 'bound to a different event loop' and report False."""
        raw = SlowRawSocket(delay=0.3)
        target = encrypted_target(raw)
        rpc = make_rpc(ws_thread.loop)

        # The WS loop's own traffic (a health reply) holds the lock while we send.
        in_flight = asyncio.run_coroutine_threadsafe(
            rpc.send_to_extension({"type": "health"}, browser=target), ws_thread.loop)
        await asyncio.sleep(0.05)
        started = time.perf_counter()
        try:
            ok = await asyncio.wait_for(
                rpc.send_to_extension({"type": "request", "id": "r1"}, browser=target), 5)
        except asyncio.TimeoutError:
            ok = "hung: the lock release never woke this loop"
        elapsed = time.perf_counter() - started

        assert in_flight.result(5) is True
        assert ok is True, "the contended send from the tool loop failed"
        assert len(raw.frames) == 2
        assert elapsed < 2.0

    async def test_every_write_runs_on_the_ws_loop(self, ws_thread):
        raw = SlowRawSocket(delay=0.01)
        target = encrypted_target(raw)
        rpc = make_rpc(ws_thread.loop)

        for i in range(5):
            assert await rpc.send_to_extension({"id": f"r{i}"}, browser=target) is True

        assert raw.send_loops and all(loop is ws_thread.loop for loop in raw.send_loops)

    async def test_without_a_ws_loop_it_sends_inline(self):
        """An embedded server (tests, no listener) has no WS loop: write on the caller's."""
        raw = SlowRawSocket(delay=0.0)
        target = SimpleNamespace(ws=raw, instance_id="fx-1")
        rpc = make_rpc(None)

        assert await rpc.send_to_extension({"id": "r1"}, browser=target) is True
        assert raw.send_loops == [asyncio.get_running_loop()]

    async def test_a_closed_ws_loop_falls_back_to_an_inline_write(self):
        loop = asyncio.new_event_loop()
        loop.close()
        raw = SlowRawSocket(delay=0.0)
        target = SimpleNamespace(ws=raw, instance_id="fx-1")
        rpc = make_rpc(loop)

        ok = await asyncio.wait_for(rpc.send_to_extension({"id": "r1"}, browser=target), 3)
        assert ok is True, "a closed WS loop must fall back to an inline write"


class RacyCounterRecordLayer(RecordLayer):
    """Forces the read-then-advance window open: every read of the counter waits for the
    other thread to have read it too. Without a lock around seal, both
    threads read the same value; with one, the second cannot get in and the barrier times
    out, which is the fixed behaviour."""

    def __init__(self, *args, **kwargs):
        self._n = 0
        self.gate = threading.Barrier(2, timeout=0.5)
        super().__init__(*args, **kwargs)

    @property
    def _send_n(self):
        value = self._n     # read first, then wait: both threads hold the same stale value
        try:
            self.gate.wait()
        except threading.BrokenBarrierError:
            pass
        return value

    @_send_n.setter
    def _send_n(self, value):
        self._n = value


class NonceRecorder:
    def __init__(self):
        self.nonces = []

    def encrypt(self, nonce, plaintext, aad):
        self.nonces.append(bytes(nonce))
        return bytes(nonce) + plaintext


class TestSealCounter:

    def test_two_threads_never_share_a_nonce(self):
        layer = RacyCounterRecordLayer(b"a" * 32, b"b" * 32)
        recorder = NonceRecorder()
        layer._send = recorder

        threads = [threading.Thread(target=layer.seal, args=(b"x",)) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)

        assert len(recorder.nonces) == 2
        assert len(set(recorder.nonces)) == 2, "two records were sealed under one nonce"

    def test_concurrent_seal_yields_a_gapless_counter(self):
        layer = RecordLayer(b"a" * 32, b"b" * 32)
        recorder = NonceRecorder()
        layer._send = recorder
        per_thread, workers = 2000, 4
        start = threading.Barrier(workers)

        def hammer():
            start.wait()
            for _ in range(per_thread):
                layer.seal(b"x")

        threads = [threading.Thread(target=hammer) for _ in range(workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)

        counters = sorted(int.from_bytes(n[-8:], "big") for n in recorder.nonces)
        assert counters == list(range(per_thread * workers))
