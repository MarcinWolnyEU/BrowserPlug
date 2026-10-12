"""
Unit tests for BrowserPlugServer failure handling:
  * in-flight requests are failed when the extension disconnects, is displaced, or the
    server shuts down — and the failure actually WAKES the awaiting loop
  * an unrequested death of the MCP HTTP thread is fatal for the standalone server
"""

import asyncio
import json
import logging
import threading
import time
import warnings

import pytest
from unittest.mock import AsyncMock, MagicMock, Mock, call, patch

from server.server import BrowserPlugServer


def park_awaiter_on_another_loop(server, request_id, timeout=5.0):
    """Register a pending request whose future lives on a DIFFERENT loop, in another
    thread, with a coroutine parked on it — the real topology (tool coroutines await on
    the uvicorn thread's loop; the WebSocket handler resolves them from the WS loop).

    Returns (thread, result) where result fills in with {"value", "elapsed"} once the
    awaiter resumes. A resolve that does not WAKE the owning loop leaves it asleep until
    its own timeout timer fires, so `elapsed` is the assertion that matters.
    """
    result = {}
    registered = threading.Event()

    def run():
        loop = asyncio.new_event_loop()

        async def waiter():
            future = loop.create_future()
            server.rpc.pending_requests[request_id] = future
            registered.set()
            start = time.perf_counter()
            try:
                result["value"] = await asyncio.wait_for(future, timeout)
            except asyncio.TimeoutError:
                result["value"] = "TIMED OUT — never woken"
            result["elapsed"] = time.perf_counter() - start

        try:
            loop.run_until_complete(waiter())
        finally:
            loop.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert registered.wait(5.0), "awaiter thread never registered its future"
    time.sleep(0.1)  # let that loop actually PARK — a busy loop would pick up an
                     # unwoken call_soon callback on its current spin and hide the bug
    return thread, result


def register_browser(server, sock, instance_id=None, pid=None):
    """Put a connected socket into the browser registry without running the handler
    loop — as a connect (+ its extension_hello, when instance_id is given) would."""
    ident = {"pid": pid, "friendly": "Firefox"} if pid else {}
    with patch.object(server, "_probe_browser_identity", return_value=ident):
        browser = server.registry.register(sock)
    if instance_id:
        browser = server.registry.promote(browser, instance_id)
    return browser


class TestPendingRequestsOnDisconnect:

    @pytest.fixture
    def server(self):
        return BrowserPlugServer(host="localhost", port=8772, start_mcp=False)

    async def _run_handler(self, server, sock, mid_stream=None, messages=(), identity=None):
        async def stream():
            for message in messages:
                yield message
            if mid_stream:
                mid_stream()
        sock.__aiter__ = lambda *args: stream()

        with patch.object(server, "_probe_browser_identity", return_value=identity or {}), \
             patch.object(server, "_notify_connection_waiters"):
            await server.handle_extension_connection(sock)
        await asyncio.sleep(0)  # futures settle on their owning loop's next tick

    @pytest.mark.asyncio
    async def test_disconnect_fails_pending_requests(self, server):
        """A dying extension must fail in-flight requests instead of letting each one
        burn its full timeout and then misreport 'Request timed out'."""
        sock = AsyncMock()
        sock.remote_address = ("127.0.0.1", 1111)
        sock.close_code = None

        pending = asyncio.Future()
        server.rpc.pending_requests["req_1"] = pending

        await self._run_handler(server, sock)

        assert pending.done()
        result = pending.result()
        # Same envelope send_request_and_wait returns for its own failures (mcp_tools._error_of).
        assert "error" in result
        assert "disconnected" in result["error"].lower()
        # The request id is ours, not something the agent has ever seen — it stays in the log.
        assert "req_1" not in result["error"]
        assert server.rpc.pending_requests == {}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("close_code", [None, 1006])
    async def test_displacement_fails_the_old_connections_requests(self, server, close_code):
        """Displacement is now per browser: a socket carrying the instance_id of a
        REGISTERED browser (that Firefox restarted / reloaded the extension) takes that
        browser's record over, and must fail the displaced socket's in-flight requests
        itself. Relying on the displaced handler's finally is not enough: it is skipped
        once the record no longer points at that socket — guaranteed when the old socket's
        close_code is ALREADY set, because then the displacement path never suspends and
        the old handler cannot run first."""
        old = AsyncMock()
        old.close_code = close_code
        # Both sockets come from one browser process: taking over a record whose socket
        # is still open needs that proof (identity_claim.py).
        register_browser(server, old, instance_id="ff-1", pid=4242)

        pending = asyncio.Future()
        server.rpc.pending_requests["req_old"] = pending
        server.rpc.pending_owners["req_old"] = "ff-1"

        new = AsyncMock()
        new.remote_address = ("127.0.0.1", 2222)
        new.close_code = None
        await self._run_handler(server, new, messages=[
            json.dumps({"type": "extension_hello", "data": {"instanceId": "ff-1"}})],
            identity={"pid": 4242, "friendly": "Firefox"})

        assert pending.done(), "the displaced connection's request was orphaned"
        assert "displaced" in pending.result()["error"]
        assert server.rpc.pending_requests == {}

    @pytest.mark.asyncio
    async def test_displaced_connection_does_not_fail_successor_requests(self, server):
        """A socket that was already replaced must NOT fail the successor's requests —
        same race the state-clearing guard protects against."""
        sock_a = AsyncMock()
        sock_a.remote_address = ("127.0.0.1", 1111)
        sock_a.close_code = None
        sock_b = AsyncMock()

        pending = asyncio.Future()

        def b_takes_over():
            server.extension_connection = sock_b
            server.rpc.pending_requests["req_b"] = pending

        await self._run_handler(server, sock_a, mid_stream=b_takes_over)

        assert not pending.done()
        assert server.rpc.pending_requests == {"req_b": pending}

    @pytest.mark.asyncio
    async def test_already_done_future_is_left_alone(self, server):
        """A request whose awaiter already timed out leaves a done future behind —
        completing it again would raise InvalidStateError."""
        sock = AsyncMock()
        sock.remote_address = ("127.0.0.1", 1111)
        sock.close_code = None

        done = asyncio.Future()
        done.set_result({"type": "response", "data": {}})
        cancelled = asyncio.Future()
        cancelled.cancel()
        server.rpc.pending_requests["req_done"] = done
        server.rpc.pending_requests["req_cancelled"] = cancelled

        await self._run_handler(server, sock)  # must not raise

        assert done.result() == {"type": "response", "data": {}}
        assert server.rpc.pending_requests == {}

    @pytest.mark.asyncio
    async def test_disconnect_error_is_not_acknowledged(self, server):
        """The synthetic disconnect error is not an extension answer — /health's
        last_command must not claim it was acknowledged."""
        sock = AsyncMock()
        sock.close_code = None
        server.extension_connection = sock

        request = {"id": "req_1", "type": "request", "action": "tabs.list", "data": {}}
        task = asyncio.create_task(server.send_request_and_wait(request, timeout=5.0))
        await asyncio.sleep(0)  # let it register the pending future

        server.rpc.fail_pending_requests("Extension disconnected before responding")
        response = await task

        assert "error" in response
        assert server.telemetry.last_command["acknowledged"] is False


class TestCrossLoopFutureSettling:
    """The futures are created on the MCP client's loop but resolved from the WebSocket
    loop's thread. Future.set_result routes through the non-thread-safe loop.call_soon,
    which queues the wakeup WITHOUT waking the loop — the awaiter then sleeps on until
    its own timeout timer happens to spin it (and hangs forever under asyncio debug mode,
    where call_soon raises after the future is already finished)."""

    @pytest.fixture
    def server(self):
        return BrowserPlugServer(host="localhost", port=8772, start_mcp=False)

    @pytest.mark.asyncio
    async def test_response_wakes_a_future_owned_by_another_loop(self, server):
        thread, result = park_awaiter_on_another_loop(server, "req_1", timeout=5.0)

        await server.rpc.handle_response(
            {"id": "req_1", "type": "response", "action": "tabs.list", "data": {"ok": True}})

        thread.join(timeout=3.0)
        assert not thread.is_alive(), "the awaiting loop was never woken"
        assert result["value"]["data"] == {"ok": True}
        assert result["elapsed"] < 1.0, "resumed only when its own timeout timer fired"
        assert server.rpc.pending_requests == {}

    @pytest.mark.asyncio
    async def test_failure_wakes_a_future_owned_by_another_loop(self, server):
        thread, result = park_awaiter_on_another_loop(server, "req_1", timeout=5.0)

        server.rpc.fail_pending_requests("Extension disconnected before responding")

        thread.join(timeout=3.0)
        assert not thread.is_alive(), "the awaiting loop was never woken"
        assert "error" in result["value"]
        assert result["elapsed"] < 1.0, "resumed only when its own timeout timer fired"

    @pytest.mark.asyncio
    async def test_dead_owning_loop_is_reported_loudly(self, server, caplog):
        """A wakeup that cannot be delivered is a permanently hung tool call — it must
        never be swallowed at debug level."""
        dead_loop = asyncio.new_event_loop()
        orphan = dead_loop.create_future()
        dead_loop.close()
        server.rpc.pending_requests["req_1"] = orphan

        with caplog.at_level(logging.ERROR, logger="server.server"):
            server.rpc.fail_pending_requests("Extension disconnected before responding")

        assert [r for r in caplog.records if r.levelno >= logging.ERROR], \
            "a dropped wakeup was not logged at ERROR"


class TestShutdownWithRequestsInFlight:

    @pytest.fixture
    def server(self):
        return BrowserPlugServer(host="localhost", port=8772, start_mcp=False)

    @pytest.mark.asyncio
    async def test_stop_fails_pending_requests_and_actually_closes_the_socket(self, server):
        """stop_websocket_server nulls extension_connection, so the handler's finally no
        longer recognizes the socket and never fails its requests — and websockets 16's
        close() is a coroutine, so the old bare call never closed anything either."""
        sock = AsyncMock()
        sock.close_code = None
        server.extension_connection = sock
        server.lifecycle.websocket_server = MagicMock()  # Server.close() is sync in websockets 16

        pending = asyncio.Future()
        server.rpc.pending_requests["req_1"] = pending

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            server.lifecycle.stop_websocket_server()
            await asyncio.sleep(0.05)  # let the settle + close task run

        assert pending.done(), "a call in flight would hang its whole timeout budget"
        assert "shutting down" in pending.result()["error"]
        assert server.rpc.pending_requests == {}
        sock.close.assert_awaited_once()
        assert not [w for w in caught if "never awaited" in str(w.message)]
        assert server.extension_connection is None


class TestMcpThreadDeath:

    @pytest.fixture
    def server(self):
        return BrowserPlugServer(host="localhost", port=8772, start_mcp=False)

    async def _drive_real_mcp_thread(self, server, run_side_effect=None, run_return=None):
        """Drive the REAL run_mcp_server thread body (start_mcp_server's closure), with
        uvicorn stubbed to fail the way it fails in production. Returns the recorder that
        witnessed release_instance_lock / os._exit, in call order."""
        recorder = Mock()
        with patch("server.runtime.lifecycle.uvicorn.Config"), \
             patch("server.runtime.lifecycle.uvicorn.Server") as uvicorn_server, \
             patch.object(server, "mcp_app", MagicMock()), \
             patch("server.runtime.lifecycle.os._exit") as exit_mock, \
             patch("server.runtime.lifecycle.release_instance_lock") as release_mock:
            uvicorn_server.return_value.run.side_effect = run_side_effect
            uvicorn_server.return_value.run.return_value = run_return
            recorder.attach_mock(release_mock, "release")
            recorder.attach_mock(exit_mock, "exit")

            await server.start_mcp_server()
            server.lifecycle.mcp_thread.join(timeout=10.0)

        assert not server.lifecycle.mcp_thread.is_alive()
        return recorder

    @pytest.mark.asyncio
    async def test_uvicorn_systemexit_reaches_the_fatal_policy(self, server):
        """The wiring, end to end: uvicorn's bind failure calls sys.exit(1) INSIDE run(),
        and SystemExit is not an Exception — the thread body must still route it to
        on_mcp_thread_exit, which must drop the instance lock BEFORE leaving (a
        replacement that starts first would otherwise race a lock we still hold)."""
        server.fatal_on_mcp_failure = True

        recorder = await self._drive_real_mcp_thread(server, run_side_effect=SystemExit(1))

        assert recorder.mock_calls == [call.release(), call.exit(1)]

    @pytest.mark.asyncio
    async def test_uvicorn_clean_return_reaches_the_fatal_policy(self, server):
        """run() returning on its own leaves no HTTP listener — equally unrecoverable."""
        server.fatal_on_mcp_failure = True

        recorder = await self._drive_real_mcp_thread(server, run_return=None)

        assert recorder.mock_calls == [call.release(), call.exit(1)]

    @pytest.mark.asyncio
    async def test_uvicorn_death_is_tolerated_when_embedded(self, server):
        """Default (tests embed BrowserPlugServer in-process): keep the WebSocket bridge."""
        assert server.fatal_on_mcp_failure is False

        recorder = await self._drive_real_mcp_thread(server, run_side_effect=SystemExit(1))

        assert recorder.mock_calls == []

    def test_unexpected_death_is_fatal_when_standalone(self, server):
        """uvicorn's bind failure raises SystemExit — the standalone server must exit
        (releasing the instance lock) rather than linger as a lock-holding zombie."""
        server.fatal_on_mcp_failure = True

        with patch("server.runtime.lifecycle.os._exit") as exit_mock, \
             patch("server.runtime.lifecycle.release_instance_lock") as release_mock:
            server.lifecycle.on_mcp_thread_exit(SystemExit(1))

        release_mock.assert_called_once()
        exit_mock.assert_called_once_with(1)

    def test_clean_return_without_death_is_also_fatal(self, server):
        """run() returning on its own leaves no HTTP listener — equally unrecoverable."""
        server.fatal_on_mcp_failure = True

        with patch("server.runtime.lifecycle.os._exit") as exit_mock, \
             patch("server.runtime.lifecycle.release_instance_lock"):
            server.lifecycle.on_mcp_thread_exit(None)

        exit_mock.assert_called_once_with(1)

    def test_requested_shutdown_is_not_fatal(self, server):
        """stop_mcp_server sets should_exit; run() then returns normally; expected."""
        server.fatal_on_mcp_failure = True
        server.lifecycle.stop_mcp_server()

        with patch("server.runtime.lifecycle.os._exit") as exit_mock:
            server.lifecycle.on_mcp_thread_exit(None)

        exit_mock.assert_not_called()

    def test_embedded_server_tolerates_mcp_death(self, server):
        """Default (tests embed BrowserPlugServer in-process): keep the WebSocket bridge alive."""
        assert server.fatal_on_mcp_failure is False

        with patch("server.runtime.lifecycle.os._exit") as exit_mock:
            server.lifecycle.on_mcp_thread_exit(RuntimeError("boom"))

        exit_mock.assert_not_called()

    def test_superseded_thread_exit_does_not_kill_a_healthy_server(self, server):
        """stop_mcp_server gives up joining after 5s and drops its references anyway, so
        an OLD uvicorn thread can still be alive and exit long after a replacement took
        over. Only the CURRENT instance's death may take the process down."""
        server.fatal_on_mcp_failure = True
        superseded, current = object(), object()
        server.lifecycle.mcp_server_instance = current

        with patch("server.runtime.lifecycle.os._exit") as exit_mock, \
             patch("server.runtime.lifecycle.release_instance_lock"):
            server.lifecycle.on_mcp_thread_exit(SystemExit(1), superseded)

        exit_mock.assert_not_called()

    def test_current_instance_exit_is_still_fatal(self, server):
        """The identity check must not defang the real case it guards."""
        server.fatal_on_mcp_failure = True
        current = object()
        server.lifecycle.mcp_server_instance = current

        with patch("server.runtime.lifecycle.os._exit") as exit_mock, \
             patch("server.runtime.lifecycle.release_instance_lock"):
            server.lifecycle.on_mcp_thread_exit(SystemExit(1), current)

        exit_mock.assert_called_once_with(1)
