"""Requests to a browser extension and the futures that wait for their answers.

Owns: the write to the extension socket, the pending-request table, admission through the
per-browser queue, the timeout, and the one automatic retry after a transient disconnect.
The registry, telemetry and routing stay in server.py and reach this module through the
callables passed to the constructor; nothing here imports server.py.

Threads. Tool coroutines run on the uvicorn thread's loop and the extension sockets belong
to the WebSocket loop, so a request future lives on the CALLER's loop while the answer
arrives on the WS loop. Settling therefore always goes through call_soon_threadsafe
(settle_request_future). The write side hops the other way: `send_to_extension`, the only
function in this module that touches a socket, schedules the write on the WS loop when it
is called from another one, and awaits it through asyncio.wrap_future.
"""

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, Optional

from server.runtime.graceful_restart import PENDING_REASON, RESTARTING_REFUSAL
from server.browsers.request_queue import QueueRejected, is_contended, timeout_message
from server.rpc.request_retry import (CODE_DISCONNECTED, CODE_RESTARTING, CODE_SEND_FAILED,
                                      CODE_TIMEOUT, is_idempotent, reconnect_grace_seconds, strip_private_keys,
                                      wait_for_reconnect, with_disconnect_advice)

logger = logging.getLogger(__name__)

# A slot kept for a request whose caller gave up is bounded: twice the request's own
# timeout, never more than this.
LINGER_CAP_SECONDS = 120.0


@dataclass(eq=False)
class _Lingering:
    """A slot still held for a request the caller has abandoned (timeout or cancel)."""
    gate: Any
    ticket: Any
    owner: Any
    loop: asyncio.AbstractEventLoop
    timer: Any


def linger_seconds(timeout: float) -> float:
    return max(0.0, min(2.0 * float(timeout), LINGER_CAP_SECONDS))


class ExtensionRpc:
    def __init__(self, *, request_queue,
                 default_browser: Callable[[], Any],
                 browser_by_instance_id: Callable[[str], Any],
                 note_command_sent: Callable[[Dict[str, Any]], Dict[str, Any]],
                 note_browser_activity: Callable[[Any, Any], None],
                 current_mcp_call: Callable[[], Optional[Dict[str, Any]]],
                 ws_loop: Callable[[], Optional[asyncio.AbstractEventLoop]] = lambda: None):
        """Every dependency is a callable that is looked up at call time, because the
        server rebinds some of them (`browsers` is replaced on shutdown) and tests patch
        others on the server instance."""
        self.request_queue = request_queue
        self._default_browser = default_browser
        self._browser_by_instance_id = browser_by_instance_id
        self._note_command_sent = note_command_sent
        self._note_browser_activity = note_browser_activity
        self._current_mcp_call = current_mcp_call
        self._ws_loop = ws_loop

        self.pending_requests = {}  # Map of request IDs to Future objects
        # request id -> owning browser instance_id, so a disconnect fails only THAT
        # browser's in-flight requests. Kept beside pending_requests rather than folded
        # into its values because call sites (and tests) register/inspect bare futures;
        # pop_pending / fail_pending_requests keep the two dicts in step.
        self.pending_owners = {}
        # request id -> _Lingering. The wire has no cancel message (a decision that
        # stands), so after a timeout or a client cancel the browser may keep working.
        # The admission slot stays taken until its late reply arrives, its browser goes
        # away, or the grace runs out; otherwise the gate would stop bounding the extension
        # exactly when it is saturated. Mutated on the owning tool loop; the WS thread
        # only looks entries up and hops back through call_soon_threadsafe.
        self._lingering = {}
        # True while a graceful restart is under way (graceful_restart.perform_restart sets
        # it before the drain). New calls are refused with the restart message, and a
        # failure that would read "disconnected, safe to retry" says restart instead and is
        # never auto-retried: the reconnect poll would outlive the process it waits on.
        self.restarting = False

    def set_restarting(self, on: bool):
        self.restarting = bool(on)

    # ---- Pending futures -----------------------------------------------------------

    @staticmethod
    def settle_request_future(request_id: str, future: asyncio.Future, result: Dict[str, Any]):
        """Complete a request future from the WebSocket loop's thread.

        The future belongs to the CALLER's loop (tool coroutines run on the uvicorn
        thread's loop, everything here on the WebSocket loop's), and Future.set_result
        resolves it via loop.call_soon, which is NOT thread-safe: it queues the wakeup
        without ever waking the target loop (the awaiter then resumes only when its own
        timeout timer next spins it), and under asyncio debug mode it RAISES after the
        future is already finished and its callbacks cleared, so the awaiter never
        resumes at all. call_soon_threadsafe wakes the owning loop, and settling on that
        loop also removes the cross-thread done()/set_result race.
        """
        def settle():
            if not future.done():  # awaiter may already have timed out / been cancelled
                future.set_result(result)

        try:
            future.get_loop().call_soon_threadsafe(settle)
        except RuntimeError as e:
            # The owning loop is gone: nothing will ever resume that awaiter, so this is
            # a permanently hung tool call; it must never be logged below WARNING.
            logger.error(f"Could not complete request {request_id} — its event loop is gone: {e}")

    def pop_pending(self, request_id: str):
        """Drop a request from both halves of the pending state."""
        self.pending_owners.pop(request_id, None)
        return self.pending_requests.pop(request_id, None)

    def _linger(self, request_id: str, gate, ticket, owner, timeout: float) -> None:
        """Keep `ticket`'s slot for an abandoned request; see `_lingering`."""
        loop = asyncio.get_running_loop()
        ticket.lingering = True
        timer = loop.call_later(linger_seconds(timeout), self._end_linger, request_id)
        self._lingering[request_id] = _Lingering(gate, ticket, owner, loop, timer)

    def _end_linger(self, request_id: str) -> None:
        """Release a lingering slot. Runs on the loop that owns the gate's waiters."""
        entry = self._lingering.pop(request_id, None)
        if entry is None:
            return
        entry.timer.cancel()
        entry.gate.release(entry.ticket)

    def _end_linger_threadsafe(self, request_id: str) -> None:
        entry = self._lingering.get(request_id)
        if entry is None:
            return
        try:
            entry.loop.call_soon_threadsafe(self._end_linger, request_id)
        except RuntimeError:
            # The tool loop is gone; nobody waits on that gate through it any more.
            self._end_linger(request_id)

    def reassign_pending_owner(self, old_instance_id: str, new_instance_id: str):
        """Follow a browser through a re-key (provisional -> instance_id, or a merge):
        its in-flight requests must keep pointing at the record that owns the socket."""
        for entry in self._lingering.values():
            if entry.owner == old_instance_id:
                entry.owner = new_instance_id
        for request_id, owner in list(self.pending_owners.items()):
            if owner == old_instance_id:
                self.pending_owners[request_id] = new_instance_id

    def fail_pending_requests(self, reason: str, instance_id: Optional[str] = None,
                              retry: bool = True):
        """Complete in-flight requests with the same {"error": ...} envelope
        send_request_and_wait returns for its own failures (BrowserCalls.error_of in server/tools reads it).
        The reason is the whole agent-facing message: the request id is ours, not
        something the agent has ever seen or can act on; it stays in the log.

        retry=False marks a failure the same request would hit again (a reply over the
        WebSocket size limit): no transient tag, so nothing re-sends it after a reconnect.

        instance_id scopes the failure to one browser (a disconnect must not fail another
        Firefox's calls); None fails everything (server shutdown). Requests with no
        recorded owner are failed either way: they cannot be attributed, and they only
        arise where a caller registered a future without going through
        send_request_and_wait, with a single browser they are its by construction.
        """
        # A dead socket executes nothing: its abandoned requests' slots go now.
        for rid, entry in list(self._lingering.items()):
            if instance_id is None or entry.owner in (instance_id, None):
                self._end_linger_threadsafe(rid)
        if instance_id is None:
            pending, self.pending_requests = self.pending_requests, {}
            self.pending_owners.clear()
        else:
            pending = {rid: fut for rid, fut in list(self.pending_requests.items())
                       if self.pending_owners.get(rid) in (instance_id, None)}
            for rid in pending:
                self.pop_pending(rid)
        if not pending:
            return
        restart_failure = self.restarting
        if self.restarting:
            # Each browser handler's `finally` runs when the restart closes its socket,
            # before the registry's own close_all; without this the caller is told a
            # browser "disconnected" and an idempotent read starts a reconnect poll.
            reason, instance_id = PENDING_REASON, None
        logger.warning(f"Failing {len(pending)} in-flight request(s) "
                       f"{sorted(pending)}: {reason}")
        # An instance-scoped failure is a DISCONNECT of one browser (not a server shutdown),
        # which is very often transport-transient; the extension reconnects within seconds.
        # Tag the envelope so send_request_and_wait can auto-retry an idempotent read ONCE.
        # The shutdown path (instance_id is None) gets the plain envelope and is never
        # retried. Each future gets its own copy: send_request_and_wait strips these keys off
        # the copy it returns, and the futures live on different loops.
        # `code` says what happened; whether a retry is safe depends on the ACTION, which
        # only the waiting side knows, so it adds the advice and `retryable` there.
        def envelope():
            if not retry:
                return {"error": reason}
            if instance_id is None:
                # A restart-drain failure is a disconnect the caller can advise on; a plain
                # server shutdown says only what it says (there is nothing to retry against).
                return ({"error": reason, "code": CODE_DISCONNECTED} if restart_failure
                        else {"error": reason})
            return {"error": reason, "code": CODE_DISCONNECTED,
                    "transient_disconnect": True, "instance_id": instance_id}
        for request_id, future in pending.items():
            self.settle_request_future(request_id, future, envelope())

    async def handle_response(self, response_data: Dict[str, Any]):
        """Handle response or error from browser extension"""
        request_id = response_data.get('id')
        if not request_id:
            logger.warning("Received response without ID")
            return

        # One pop, not check-then-pop: the timeout path pops on another thread, and losing
        # that race after the check would hand settle_request_future a None.
        future = self.pop_pending(request_id)
        if future is not None:
            self.settle_request_future(request_id, future, response_data)
            logger.info(f"Completed pending request: {request_id}")
        elif request_id in self._lingering:
            # The browser finished what the caller gave up on: the slot is free again.
            self._end_linger_threadsafe(request_id)
            logger.info(f"Late reply for abandoned request {request_id}; slot released")
        else:
            logger.warning(f"Received response for unknown request: {request_id}")

    # ---- Sending ---------------------------------------------------------------------

    async def send_to_extension(self, message: Dict[str, Any], browser: 'Any' = None) -> bool:
        """Send a message to a browser extension (default: the default browser).

        The ONLY place this module writes to an extension socket.
        """
        target = browser or self._default_browser()
        if target is None or target.ws is None:
            logger.warning("No extension connection available")
            return False

        try:
            message['timestamp'] = datetime.now().isoformat()
            await self._write(target.ws, json.dumps(message))
            return True
        except Exception as e:
            logger.error(f"Error sending to extension: {e}")
            return False

    async def _write(self, ws, payload: str) -> None:
        """Write on the loop that owns the socket.

        The socket, its transport and SecureSocket's asyncio.Lock belong to the WS loop.
        Awaiting ws.send from a tool's loop lets the lock bind to two loops (a contended
        acquire then raises "bound to a different event loop", and a release from the
        wrong thread never wakes its waiter) and writes to a transport from a foreign
        thread. With no WS loop running (an embedded server, tests) or when already on it,
        the write is inline.
        """
        ws_loop = self._ws_loop()
        if ws_loop is None or ws_loop.is_closed() or not ws_loop.is_running()                 or ws_loop is asyncio.get_running_loop():
            await ws.send(payload)
            return
        coro = ws.send(payload)
        try:
            concurrent = asyncio.run_coroutine_threadsafe(coro, ws_loop)
        except RuntimeError:
            # The loop closed between the check and the call; nothing was scheduled.
            await coro
            return
        # Shielded: a caller that times out or is cancelled must not abort a write that is
        # already sealed, or the peer's record counter would fall out of step.
        await asyncio.shield(asyncio.wrap_future(concurrent))

    async def send_request_and_wait(self, request: Dict[str, Any], timeout: float = 30.0,
                                    browser: 'Any' = None,
                                    _is_retry: bool = False) -> Dict[str, Any]:
        """Send a request to a browser extension and wait for its response.

        browser selects the target Firefox; the default browser (first connected, still
        alive) when unset, which is the only browser in the single-Firefox case.

        _is_retry marks the one automatic re-send of a transient-disconnect retry; it is
        internal and stops the retry from ever recursing more than once.
        """
        request_id = request.get('id')
        if not request_id:
            raise ValueError("Request must have an ID")

        if self.restarting:
            # Refused before admission: nothing is sent, and the drain only has the calls
            # that were already in flight to wait for.
            # Nothing was sent, so a retry is safe for every action, not only the reads.
            return {"error": RESTARTING_REFUSAL, "code": CODE_RESTARTING, "retryable": True}

        target = browser or self._default_browser()
        if target is None or target.ws is None:
            return {"error": "No extension connection available"}

        # Wait our turn at THIS browser before touching the wire. Solo, this is one
        # integer compare and no await, so the common path is unchanged. Contended, the
        # ticket records what we competed with, which is the only way the timeout below can
        # tell "the browser was too busy to answer" apart from "the page did not load".
        # A refusal here means NOTHING was sent, so the caller can safely re-issue.
        gate = self.request_queue.gate_for(target.instance_id)
        try:
            ticket = await gate.acquire(action=request.get("action"))
        except QueueRejected as rejected:
            return {"error": str(rejected)}
        try:
            return await self._send_admitted(request, timeout, target, ticket, _is_retry,
                                             gate=gate)
        finally:
            if not ticket.lingering:  # a lingering slot is released by _end_linger
                gate.release(ticket)

    async def _send_admitted(self, request: Dict[str, Any], timeout: float,
                             target: 'Any', ticket, _is_retry: bool,
                             gate: Any = None) -> Dict[str, Any]:
        """Send an ADMITTED request and await its answer; admission happened in the caller.

        Everything below this line is what send_request_and_wait always did; it lives in its
        own method so the gate's release can be a plain `finally` around one call rather than
        wrapped around a long body where an early return could skip it.
        """
        request_id = request['id']

        # Create future for response
        response_future = asyncio.Future()
        self.pending_requests[request_id] = response_future
        # Owner: a disconnect must fail only THIS browser's in-flight requests.
        self.pending_owners[request_id] = target.instance_id
        cmd_record = self._note_command_sent(request)
        # Real browser activity (excludes heartbeats/pings/popup polls/reconnects).
        self._note_browser_activity(target, request.get('action'))
        # Capture the call record NOW: by the time the answer arrives, a concurrent
        # client may have opened a newer call; only ours may be stamped.
        mc = self._current_mcp_call()
        if mc is not None and mc.get("session") == cmd_record.get("session"):
            if mc.get("browser_slot") is None:
                mc["browser_slot"] = getattr(target, "slot", None)
        else:
            mc = None

        sent = False
        try:
            # Send the request
            sent = True  # from here the browser may act on it, even if we are cancelled
            success = await self.send_to_extension(request, browser=target)
            if not success:
                self.pop_pending(request_id)
                sent = False
                return {"error": "Failed to send request to extension",
                        "code": CODE_SEND_FAILED,
                        "retryable": is_idempotent(request.get("action"))}

            # Wait for response with timeout
            response = await asyncio.wait_for(response_future, timeout=timeout)
            # Transient-disconnect auto-retry: fail_pending_requests tags a failure
            # caused by THIS browser dropping its socket. If the action is a read-only one
            # (re-sending is safe) and this is the first attempt, give the browser a short
            # grace to reconnect and re-send exactly once. The tag keys are private plumbing;
            # strip them off whatever envelope we ultimately return so tool code never sees
            # them. Non-transient errors and mutating actions fall straight through unchanged.
            if isinstance(response, dict) and response.get("transient_disconnect"):
                # The socket that carried this request is gone, so it holds nothing in the
                # browser. Give the slot back before the retry queues for one: a displaced
                # (not removed) socket keeps its gate, and the retry would otherwise wait
                # for its own parent.
                if gate is not None:
                    gate.release(ticket)
                retried = await self._retry_after_transient_disconnect(
                    request, response, timeout, _is_retry)
                return strip_private_keys(retried)
            if isinstance(response, dict) and response.get("code") == CODE_DISCONNECTED:
                # The restart drain failed this one (no transient tag, never retried).
                return with_disconnect_advice(response, request.get("action"))
            # The extension answered -> this command is acknowledged. A future completed by
            # fail_pending_requests (the extension went away) carries a bare {"error": ...}
            # with no protocol envelope; nobody answered that one, so don't claim an ack.
            if not (isinstance(response, dict) and "error" in response and "type" not in response):
                cmd_record["acknowledged"] = True
                cmd_record["acknowledged_at"] = datetime.now()
                # Last answer wins: for a multi-command tool the browser leg runs
                # from the first dispatch to the final answer.
                if mc is not None and self._current_mcp_call() is mc:
                    mc["ws_ack_at"] = cmd_record["acknowledged_at"]
            return response

        except asyncio.TimeoutError:
            self.pop_pending(request_id)
            if gate is not None:
                self._linger(request_id, gate, ticket, target.instance_id, timeout)
            # Name contention when there WAS contention, and say nothing extra when
            # there was not; a solo timeout still reads exactly as it always did, which is
            # what keeps the contended wording worth reading.
            self.request_queue.note_contention_timeout(ticket)
            # Retryable is data: a read, and not the contended wording that tells the agent
            # NOT to retry. A click or script that timed out may still be running.
            return {"error": timeout_message(ticket, timeout), "code": CODE_TIMEOUT,
                    "retryable": (is_idempotent(request.get("action"))
                                  and not is_contended(ticket))}
        except asyncio.CancelledError:
            # The client went away. Without this the entries outlive the call and every
            # restart drain waits its full budget for a request nobody awaits.
            self.pop_pending(request_id)
            if sent and gate is not None:
                self._linger(request_id, gate, ticket, target.instance_id, timeout)
            raise
        except Exception as e:
            # The caller gets the message; the traceback is the only trace of what broke.
            logger.warning(f"Request {request_id} ({request.get('action')}) failed: {e}",
                           exc_info=True)
            self.pop_pending(request_id)
            return {"error": f"Request failed: {str(e)}"}

    async def _retry_after_transient_disconnect(self, request: Dict[str, Any],
                                                response: Dict[str, Any], timeout: float,
                                                is_retry: bool) -> Dict[str, Any]:
        """Handle a transient-disconnect envelope for `request`.

        If the action is read-only (safe to re-send) and this is the FIRST attempt, wait a
        short grace for the SAME browser (by instance_id) to reconnect with a live socket,
        then re-send the request ONCE as a fresh request id to that browser and return the
        retry's outcome, whatever it is (success, a real error, even a second disconnect).
        Otherwise (already a retry, a mutating action, or no reconnect within the grace)
        return the original error unchanged. The returned envelope may still carry the private
        tag keys; the caller strips them. Retries at most once and only ever affects the one
        disconnected browser's own request (the owner scoping already did that filtering).
        """
        action = request.get("action")
        instance_id = response.get("instance_id")
        if is_retry or not instance_id or not is_idempotent(action) or self.restarting:
            return with_disconnect_advice(response, action)

        grace = reconnect_grace_seconds()
        logger.info(
            f"{action} failed on a transient disconnect of browser {instance_id}; waiting up "
            f"to {grace:.1f}s for it to reconnect, then retrying once")

        def reconnected():
            b = self._browser_by_instance_id(instance_id)
            return b if (b is not None and b.ws is not None) else None

        if not await wait_for_reconnect(reconnected, grace):
            logger.info(
                f"browser {instance_id} did not reconnect within {grace:.1f}s; returning the "
                f"original transient-disconnect error for {action}")
            return with_disconnect_advice(response, action)

        target = reconnected()
        retry_request = {
            "id": str(uuid.uuid4()),
            "type": "request",
            "action": action,
            "data": request.get("data", {}),
            "timestamp": datetime.now().isoformat(),
        }
        logger.info(
            f"browser {instance_id} reconnected; retrying {action} once as request "
            f"{retry_request['id']}")
        return await self.send_request_and_wait(
            retry_request, timeout=timeout, browser=target, _is_retry=True)
