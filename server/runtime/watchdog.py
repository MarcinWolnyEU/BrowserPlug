"""The server's watchdog: notice a frozen event loop and end the freeze.

On 2026-10-02 the shared server stopped answering for 14 minutes while its process stayed
alive and kept the single-instance lock. Synchronous work on one loop (a regex that took
minutes, run on the uvicorn loop) held the GIL, so the WebSocket loop froze with it, and
nothing in the process could notice. Every client then waited on a server that was neither
serving nor gone. This module is the part that notices.

Three layers, each covering what the one before cannot:

  1. Heartbeats. A task on each event loop touches a timestamp every HEARTBEAT_SECONDS. A
     loop that is blocked stops touching it.
  2. A supervisor thread. Every TICK_SECONDS it reads both heartbeats, probes the server's
     own /health over loopback, and feeds `decide`, a pure function. It acts when the same
     problem lasts BAD_TICKS ticks: it dumps every thread's stack into the hang log and
     starts a graceful restart (`begin_restart("watchdog")`, which runs on the loop that
     still works). If that has not ended the freeze after RESTART_GRACE_SECONDS it hands the
     server over and ends the process: the instance lock is released, a successor is spawned
     (it waits for the ports), and the process is terminated.
  3. A dead-man timer. `faulthandler.dump_traceback_later(..., exit=True)` is a C thread
     that needs no GIL. The supervisor re-arms it each tick only while both heartbeats are
     fresh and the probe worked. A freeze that stops the supervisor too (the GIL held by
     one call) stops the re-arming, and DEAD_MAN_SECONDS later the C thread writes every
     stack to the hang log and ends the process. The OS releases the lock with it. On
     Windows a second timer, on a kernel timer thread, calls TerminateProcess a few seconds
     after the first one.

The supervisor must not need anything that the freeze it is looking at can take away. On
2026-10-09 a stuck kernel-wide call (psutil's open_files, see usage_panels/handle_scan.py)
made every kernel object creation in the process wait: a socket for the WebSocket loop, a
thread for the HTTP loop. The supervisor was a victim too. It made its probe's socket and
its log thread itself, and one tick took about 58 s (the next arrived 63 s after the
last), so the three bad ticks never counted. The dead-man then fired, but the process did
not end (the hang log has the dead-man's dump first and the supervisor's own dump after it,
so Python code kept running after `_exit` was called): it lived another 12 minutes holding
the instance lock, and every launcher that tried to start a server was refused. `_exit` is
ExitProcess on Windows, which serialises on the loader lock; that is the likely place it
stuck. So now:

  * the supervisor creates no thread, socket or process on its own. The probe and the
    hand-over run on worker threads made at `start()`, and a tick waits for them with a
    deadline; a worker that is still stuck from an earlier tick counts as a failed probe;
  * the process is ended with TerminateProcess (`terminate_process`), a system call that
    takes no lock of the interpreter's or the loader's, and never with `os._exit` alone;
  * the successor is spawned by the dying server itself, after the lock is released, so a
    confirmed freeze ends with the old process gone and a new one starting, whether or not a
    client's launcher is around to notice.

The numbers are deliberately slow. Nothing the loops do legitimately blocks for more than a
fraction of a second (reports are built on worker threads, a navigation waits on the
extension, restarts and uv calls run off the loop), so a loop silent for STALE_SECONDS is
already unusable to every client. Detection takes about 25 to 30 s, a forced exit about a
minute, the dead-man two minutes: against a 14 minute outage, but far above any stall a
loaded machine produces. A suspended machine is handled separately: a tick that arrives
much later than scheduled, with the lateness spent asleep, means the supervisor was not
running, so that tick is forgiven (`GAP_FORGIVE_SECONDS`). A late tick that was not spent
asleep is a stall of the process itself (one call held the GIL, the supervisor included); it
is dumped, logged and counted, because a freeze that thaws briefly and freezes again never
shows two stale ticks in a row.

The watchdog runs only in the standalone server process, never in an embedded server (tests
construct those directly), because the dead-man exits the process it lives in.

Knobs, for tests and for a box that needs different numbers: BROWSERPLUG_WATCHDOG=0 turns
it off; BROWSERPLUG_WATCHDOG_TICK, _STALE, _BAD_TICKS, _GRACE, _DEAD_MAN, _GAP_FORGIVE and
_HANDOVER override the constants below.

Importing this module has no side effects.
"""

import asyncio
import ctypes
import faulthandler
import http.client
import json
import logging
import os
import queue
import sys
import threading
import time
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Optional

from browserplug_shared.mcp_port import HEALTH_SERVICE

logger = logging.getLogger(__name__)

ENABLE_ENV = "BROWSERPLUG_WATCHDOG"
HEARTBEAT_SECONDS = 1.0
TICK_SECONDS = 5.0
STALE_SECONDS = 15.0
BAD_TICKS = 3
RESTART_GRACE_SECONDS = 30.0
# The most a tick waits for `begin_restart` to return (it only schedules the sequence).
RESTART_CALL_SECONDS = 5.0
DEAD_MAN_SECONDS = 120.0
PROBE_TIMEOUT_SECONDS = 4.0
# How long a tick waits beyond the probe's own timeout for the probe worker to report.
PROBE_SLACK_SECONDS = 1.0
# The most the supervisor waits for the hand-over (lock released, successor spawned) before
# it ends the process anyway.
HANDOVER_SECONDS = 10.0
# The Windows terminate timer fires this long after faulthandler's, which has to dump first.
NATIVE_CUSHION_SECONDS = 5.0
# A tick this much later than TICK_SECONDS means the supervisor was not running (the machine
# slept, a debugger stopped the process), so what it sees is not evidence of a frozen loop.
GAP_FORGIVE_SECONDS = 10.0
# The hang log is appended to for the life of the process; past this size the previous run's
# content is moved aside when the next process starts.
HANG_LOG_MAX_BYTES = 512 * 1024


def hang_log_path(log_file):
    """`<base>.hang.log` next to the server log: where every stack dump goes."""
    base, _, _ = str(log_file).rpartition(".log")
    return (base or str(log_file)) + ".hang.log"


@dataclass(frozen=True)
class Config:
    heartbeat: float = HEARTBEAT_SECONDS
    tick: float = TICK_SECONDS
    stale: float = STALE_SECONDS
    bad_ticks: int = BAD_TICKS
    grace: float = RESTART_GRACE_SECONDS
    dead_man: float = DEAD_MAN_SECONDS
    probe_timeout: float = PROBE_TIMEOUT_SECONDS
    gap_forgive: float = GAP_FORGIVE_SECONDS
    handover_wait: float = HANDOVER_SECONDS

    @classmethod
    def from_env(cls, environ=None):
        env = os.environ if environ is None else environ

        def number(name, default, cast=float):
            raw = env.get(f"{ENABLE_ENV}_{name}")
            try:
                value = cast(raw)
            except (TypeError, ValueError):
                return default
            return value if value > 0 else default

        tick = number("TICK", TICK_SECONDS)
        return cls(heartbeat=min(HEARTBEAT_SECONDS, tick),
                   tick=tick,
                   stale=number("STALE", STALE_SECONDS),
                   bad_ticks=number("BAD_TICKS", BAD_TICKS, int),
                   grace=number("GRACE", RESTART_GRACE_SECONDS),
                   dead_man=number("DEAD_MAN", DEAD_MAN_SECONDS),
                   gap_forgive=number("GAP_FORGIVE", max(GAP_FORGIVE_SECONDS, 2 * tick)),
                   handover_wait=number("HANDOVER", HANDOVER_SECONDS))


def enabled(environ=None):
    env = os.environ if environ is None else environ
    return str(env.get(ENABLE_ENV, "1")).strip().lower() not in ("0", "false", "off", "no")


# --- The decision table ---------------------------------------------------------------

class Action(Enum):
    NONE = "none"
    # Dump the stacks and start a graceful restart.
    RESTART = "restart"
    # Dump the stacks only: a restart (or stop) is already under way and the loop is
    # still stuck.
    DUMP = "dump"
    # The grace period ran out and the freeze is still there: hand the server over (release
    # the lock, spawn a successor) and end the process.
    FORCE_EXIT = "force_exit"


@dataclass(frozen=True)
class Observation:
    """What one supervisor tick saw."""
    now: float                       # monotonic seconds
    gap: float                       # seconds since the previous tick
    ws_age: Optional[float]          # seconds since the WebSocket loop's heartbeat; None = no such loop
    mcp_age: Optional[float]         # same for the MCP HTTP loop
    probe_ok: Optional[bool]         # the loopback /health probe; None = nothing to probe
    leaving: bool = False            # an intentional stop is under way (lifecycle.stopping)
    restarting: bool = False         # a restart sequence is already running
    # Seconds of `gap` the machine spent suspended; None = this platform cannot tell, so a
    # late tick is assumed to be a suspend.
    slept: Optional[float] = None
    # The probe has answered at least once since the process started. Until it has, a probe
    # that fails while both loops beat says more about the probe (the wrong address) than
    # about the server.
    probe_seen: bool = True


@dataclass(frozen=True)
class State:
    bad_ticks: int = 0
    escalated_at: Optional[float] = None
    # When the supervisor itself stalled (a late tick that was not a suspend), within the last
    # DEAD_MAN seconds. A freeze that thaws between stalls resets `bad_ticks` every time, so
    # the stalls are counted on their own.
    stalls: tuple = ()


@dataclass(frozen=True)
class Decision:
    action: Action
    rearm: bool                      # re-arm the dead-man timer this tick
    reason: str
    state: State = field(default_factory=State)
    stall: bool = False              # this tick arrived late and the machine was not asleep


def slept_through(obs: Observation, cfg: Config):
    """Whether a late tick's lateness was spent suspended (or cannot be told apart)."""
    return obs.slept is None or obs.slept >= (obs.gap - cfg.tick) / 2


def loops_beating(obs: Observation, cfg: Config):
    return all(age is None or age <= cfg.stale for age in (obs.ws_age, obs.mcp_age))


def problems_of(obs: Observation, cfg: Config):
    found = []
    if obs.ws_age is not None and obs.ws_age > cfg.stale:
        found.append(f"the WebSocket loop has not run for {obs.ws_age:.0f}s")
    if obs.mcp_age is not None and obs.mcp_age > cfg.stale:
        found.append(f"the MCP HTTP loop has not run for {obs.mcp_age:.0f}s")
    if obs.probe_ok is False and (obs.probe_seen or not loops_beating(obs, cfg)):
        found.append("GET /health over loopback failed")
    return found


def decide(obs: Observation, state: State, cfg: Config) -> Decision:
    """The whole policy. Pure: a tick's observation and the previous state in, the action,
    the re-arm verdict and the next state out.

    - A tick that came much later than scheduled because the machine slept is forgiven and
      resets everything.
    - A late tick that was not a suspend is a stall of the process (one call held the GIL,
      the supervisor included). It is a problem of its own, and BAD_TICKS of them within
      DEAD_MAN_SECONDS act even when every tick in between looked healthy.
    - A tick with no problem re-arms the dead-man and resets the count of bad ticks.
    - A tick with a problem never re-arms it. The first BAD_TICKS-1 such ticks only count.
    - The BAD_TICKS-th in a row acts: RESTART, or DUMP when a restart is already running.
    - After acting, a problem that outlasts the grace period ends in FORCE_EXIT. This is
      checked before `leaving`, because the watchdog's own restart sets `leaving`.
    - While an intentional stop is under way and nothing was escalated, a problem is only
      counted: the dead-man timer remains the backstop for a stop that never finishes.
    - A failing probe is no problem while both loops beat and it has never answered.
    """
    late = obs.gap > cfg.tick + cfg.gap_forgive
    if late and slept_through(obs, cfg):
        return Decision(Action.NONE, True, "supervisor tick delayed; forgiven")
    stalls = tuple(t for t in state.stalls if obs.now - t < cfg.dead_man)
    problems = problems_of(obs, cfg)
    if late:
        stalls += (obs.now,)
        problems.insert(0, f"the process stalled for about {obs.gap - cfg.tick:.0f}s "
                           f"(the supervisor ran that late and the machine was awake)")
    if not problems:
        return Decision(Action.NONE, True, "",
                        State(stalls=stalls, escalated_at=state.escalated_at if stalls else None))
    reason = "; ".join(problems)
    bad = state.bad_ticks + 1
    if state.escalated_at is not None:
        if obs.now - state.escalated_at >= cfg.grace:
            return Decision(Action.FORCE_EXIT, False, reason,
                            replace(state, bad_ticks=bad, stalls=stalls), late)
        return Decision(Action.NONE, False, reason,
                        replace(state, bad_ticks=bad, stalls=stalls), late)
    if obs.leaving and not obs.restarting:
        return Decision(Action.NONE, False, reason,
                        replace(state, bad_ticks=bad, stalls=stalls), late)
    if bad >= cfg.bad_ticks or len(stalls) >= cfg.bad_ticks:
        action = Action.DUMP if obs.restarting or obs.leaving else Action.RESTART
        return Decision(action, False, reason,
                        State(bad_ticks=bad, escalated_at=obs.now, stalls=stalls), late)
    return Decision(Action.NONE, False, reason, State(bad_ticks=bad, stalls=stalls), late)


# --- Heartbeats and the supervisor ----------------------------------------------------

class Heartbeat:
    """The last time a loop ran its beat task. Written by the loop, read by the supervisor."""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self.last = clock()

    def touch(self):
        self.last = self._clock()

    def age(self):
        return self._clock() - self.last


def probe_hosts(bound):
    """The loopback addresses worth trying for a listener bound to `bound`, best first.

    The probe must reach the socket the server really listens on: `--ws-host ::` makes
    uvicorn listen on :: only (IPV6_V6ONLY on Windows), where 127.0.0.1 is refused."""
    host = (bound or "").strip().strip("[]").lower()
    if host in ("", "0.0.0.0"):
        return ("127.0.0.1",)
    if host in ("::", "::0"):
        return ("::1", "127.0.0.1")
    if host == "localhost":
        return ("127.0.0.1", "::1")
    return (host,)


def default_probe(port, timeout, bound=None):
    """True when this server's own /health answers over loopback; None with no port.

    A host that refuses the connection is skipped for the next one; a host that accepts it
    and then does not answer is the verdict (False), because that is what a frozen loop
    looks like and trying another address would only double the wait."""
    if not port:
        return None
    for host in probe_hosts(bound):
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
        try:
            try:
                conn.connect()
            except OSError:
                continue
            conn.request("GET", "/health?identity=1")
            resp = conn.getresponse()
            body = json.loads(resp.read() or b"{}")
            return resp.status == 200 and body.get("service") == HEALTH_SERVICE
        except Exception:
            return False
        finally:
            conn.close()
    return False


# --- Telling a suspend from a stall -----------------------------------------------------

def suspended_seconds():
    """Cumulative seconds this machine has spent suspended since boot, or None when the
    platform cannot say. Two clocks that differ only by suspended time, subtracted:
    Windows has the interrupt time with and without sleep, Linux has CLOCK_BOOTTIME and
    CLOCK_MONOTONIC. macOS's monotonic clock already stops during sleep, so a suspend
    never shows up as a late tick there."""
    try:
        if sys.platform == "win32":
            biased = ctypes.c_ulonglong()
            unbiased = ctypes.c_ulonglong()
            ctypes.WinDLL("kernelbase").QueryInterruptTime(ctypes.byref(biased))
            ctypes.WinDLL("kernel32").QueryUnbiasedInterruptTime(ctypes.byref(unbiased))
            return (biased.value - unbiased.value) / 1e7      # 100 ns units
        if sys.platform.startswith("linux"):
            return (time.clock_gettime(time.CLOCK_BOOTTIME)
                    - time.clock_gettime(time.CLOCK_MONOTONIC))
    except Exception:
        pass
    return None


def terminate_process(code=1):
    """End this process now. On Windows that is TerminateProcess on the current process, a
    system call that runs no DLL detach, takes no loader lock and needs no GIL, where
    `os._exit` is ExitProcess and can wait on the very lock a stuck thread holds. Does not
    return; elsewhere `os._exit` is already a plain `_exit`."""
    if sys.platform == "win32":
        try:
            kernel32 = ctypes.WinDLL("kernel32")
            kernel32.GetCurrentProcess.restype = ctypes.c_void_p
            kernel32.TerminateProcess.argtypes = (ctypes.c_void_p, ctypes.c_uint)
            kernel32.TerminateProcess(kernel32.GetCurrentProcess(), code)
        except Exception:
            pass
    os._exit(code)


class _NativeDeadMan:
    """Windows only: TerminateProcess run by a kernel timer, behind faulthandler's timer.

    faulthandler's C thread dumps and then calls `_exit`, which on Windows is ExitProcess
    and did not end the stuck server on 2026-10-09. This timer's callback IS TerminateProcess
    (the timer passes it the pseudo handle of the current process as its first argument), so
    nothing runs in Python and nothing waits on the loader lock. It is armed and re-armed
    together with faulthandler's, NATIVE_CUSHION_SECONDS later, so a healthy server never
    reaches it. If a re-arm fails the timer is dropped rather than left to fire: a server
    without this layer is no worse off than before it existed."""

    WT_EXECUTEONLYONCE = 0x8
    WT_EXECUTEINTIMERTHREAD = 0x20

    def __init__(self, cushion=NATIVE_CUSHION_SECONDS):
        self.cushion = cushion
        self.available = sys.platform == "win32"
        self._k = None
        self._timer = None
        self._terminate = None
        self._lock = threading.Lock()

    def _api(self):
        if self._k is None:
            c = ctypes
            k = c.WinDLL("kernel32", use_last_error=True)
            k.CreateTimerQueueTimer.argtypes = [c.POINTER(c.c_void_p), c.c_void_p, c.c_void_p,
                                                c.c_void_p, c.c_ulong, c.c_ulong, c.c_ulong]
            k.CreateTimerQueueTimer.restype = c.c_int
            k.ChangeTimerQueueTimer.argtypes = [c.c_void_p, c.c_void_p, c.c_ulong, c.c_ulong]
            k.ChangeTimerQueueTimer.restype = c.c_int
            k.DeleteTimerQueueTimer.argtypes = [c.c_void_p, c.c_void_p, c.c_void_p]
            k.DeleteTimerQueueTimer.restype = c.c_int
            self._terminate = c.cast(k.TerminateProcess, c.c_void_p).value
            self._k = k
        return self._k

    def arm(self, seconds):
        """(Re)start the countdown at `seconds` plus the cushion. Returns whether armed."""
        if not self.available:
            return False
        with self._lock:
            try:
                k = self._api()
                ms = max(1, min(int((seconds + self.cushion) * 1000), 0xFFFFFFFE))
                if self._timer is not None and k.ChangeTimerQueueTimer(None, self._timer, ms, 0):
                    return True
                self._drop(k)
                handle = ctypes.c_void_p()
                me = ctypes.c_void_p(ctypes.c_size_t(-1).value)      # GetCurrentProcess()
                flags = self.WT_EXECUTEONLYONCE | self.WT_EXECUTEINTIMERTHREAD
                if k.CreateTimerQueueTimer(ctypes.byref(handle), None, self._terminate, me,
                                           ms, 0, flags):
                    self._timer = handle
                    return True
            except Exception:
                pass
            self.available = False
            return False

    def _drop(self, k):
        timer, self._timer = self._timer, None
        if timer is not None:
            try:
                k.DeleteTimerQueueTimer(None, timer, None)    # no wait: never blocks
            except Exception:
                pass

    def cancel(self):
        with self._lock:
            if self._k is not None:
                self._drop(self._k)


class _BoundedCall:
    """One long-lived worker thread that runs a callable while the caller waits with a
    deadline. The caller is the supervisor, which must never wait on anything a freeze can
    take away: a probe's socket, a spawned process. The thread is made by `start()` while
    the server is healthy, not by the first call during a freeze. A call that outlives its
    deadline keeps the worker busy; the next `call` answers "did not finish" at once instead
    of queueing behind it."""

    def __init__(self, name):
        self._name = name
        self._jobs = queue.SimpleQueue()
        self._done = threading.Event()
        self._thread = None
        self._running = False
        self._seq = 0
        self._result_seq = -1
        self._result = None

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name=self._name, daemon=True)
            self._thread.start()

    def call(self, fn, timeout):
        """`(True, value)` when `fn` returned within `timeout` seconds, else `(False, None)`.
        An exception counts as returning None."""
        self.start()
        if self._running:
            return False, None
        self._running = True
        self._done.clear()
        self._seq += 1
        seq = self._seq
        self._jobs.put((seq, fn))
        if self._done.wait(timeout) and self._result_seq == seq:
            return True, self._result
        return False, None

    def _run(self):
        while True:
            seq, fn = self._jobs.get()
            try:
                value = fn()
            except Exception:
                value = None
            self._result, self._result_seq = value, seq
            self._running = False
            self._done.set()


class _BackgroundLog:
    """Logging for the supervisor thread that cannot block it.

    A freeze can hold the logging handler's lock (the frozen thread was writing a record),
    and a log call from the supervisor would then wait for ever, ahead of the dump, the
    restart and the exit it exists to perform. Records go through a queue to a thread that
    may block instead."""

    def __init__(self):
        self._queue = queue.SimpleQueue()
        self._thread = None

    def start(self):
        """Make the thread now. Left to the first record it would be made during a freeze,
        and creating a thread is one of the things a freeze can stop."""
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="watchdog-log", daemon=True)
            self._thread.start()

    def log(self, level, message, *args, **kwargs):
        self.start()
        self._queue.put((level, message, args, kwargs))

    def flush(self, timeout):
        """Wait up to `timeout` seconds for everything queued so far to be written."""
        if self._thread is None:
            return
        done = threading.Event()
        self._queue.put(done)
        done.wait(timeout)

    def _run(self):
        while True:
            item = self._queue.get()
            if isinstance(item, threading.Event):
                item.set()
                continue
            level, message, args, kwargs = item
            try:
                logger.log(level, message, *args, **kwargs)
            except Exception:
                pass


class Watchdog:
    """Owns the heartbeats, the supervisor thread and the hang log.

    Everything that touches the process arrives as a callable, so a test drives the policy
    without a server: `begin_restart(trigger)` starts the graceful restart,
    `hand_over()` releases the instance lock and starts a successor (optional; it runs
    before the exit, on a worker thread, with a deadline), `exit_process()` ends the process
    (`terminate_process`), `mcp_port()` names the port to
    probe (None = no HTTP side) and `mcp_host()` the address the listener is bound to,
    `leaving()` / `restarting()` read lifecycle state. `arm_dead_man(seconds)` and
    `dump_stacks()` default to faulthandler on the hang file. `suspended()` is the
    cumulative seconds the machine has slept (None = unknown).

    The supervisor thread never logs directly (see `_BackgroundLog`): the dump, the restart
    or the exit comes first, and the reason goes into the hang file before anything else.

    `bounded=True` (what `start()` uses) moves everything that creates a kernel object or
    can wait on one (the probe's socket, the restart call, the hand-over) onto worker
    threads made up front, so a tick never waits longer than its deadlines. Left off, those
    calls run inline on the calling thread, which is how the unit tests step a tick.
    """

    def __init__(self, cfg: Config, *, hang_file, begin_restart, exit_process,
                 hand_over=None, bounded=False,
                 mcp_port=lambda: None, mcp_host=lambda: None, leaving=lambda: False,
                 restarting=lambda: False, probe=default_probe, arm_dead_man=None,
                 dump_stacks=None, clock=time.monotonic, suspended=suspended_seconds):
        self.cfg = cfg
        self.hang_file = hang_file
        self._begin_restart = begin_restart
        self._exit_process = exit_process
        self._hand_over_fn = hand_over
        self._bounded = bounded
        self._probe_call = _BoundedCall("watchdog-probe")
        self._restart_call = _BoundedCall("watchdog-restart")
        self._handover_call = _BoundedCall("watchdog-handover")
        self._native = _NativeDeadMan()
        self._mcp_port = mcp_port
        self._mcp_host = mcp_host
        self._suspended = suspended
        self._log = _BackgroundLog()
        self._probe_seen = False
        self._probe_warned = False
        self._leaving = leaving
        self._restarting = restarting
        self._probe = probe
        self._arm = arm_dead_man or self._arm_faulthandler
        self._dump = dump_stacks or self._dump_faulthandler
        self._clock = clock
        self._beats = {}
        self._beat_futures = {}     # keeps each beat task referenced
        self._stop = threading.Event()
        self._thread = None
        self._state = State()
        self.last_decision = None

    # -- heartbeats

    async def _beat(self, beat):
        while True:
            beat.touch()
            await asyncio.sleep(self.cfg.heartbeat)

    def attach_loop(self, name, loop):
        """Start a heartbeat on `loop`. Callable from any thread; attaching a name again
        (a new uvicorn instance) replaces the old heartbeat."""
        beat = Heartbeat(self._clock)
        self._beats[name] = beat
        self._beat_futures[name] = asyncio.run_coroutine_threadsafe(self._beat(beat), loop)

    def age_of(self, name):
        beat = self._beats.get(name)
        return beat.age() if beat is not None else None

    # -- the dead-man timer and the stack dump

    def _arm_faulthandler(self, seconds):
        faulthandler.dump_traceback_later(seconds, repeat=False, file=self.hang_file, exit=True)
        self._native.arm(seconds)

    def _write(self, text):
        try:
            self.hang_file.write(text)
            self.hang_file.flush()
        except Exception:
            pass

    def _dump_faulthandler(self):
        try:
            faulthandler.dump_traceback(file=self.hang_file, all_threads=True)
        except Exception:
            self._log.log(logging.WARNING, "watchdog: could not write the stack dump",
                          exc_info=True)

    def dump(self, why):
        """Write a header naming the threads, then every thread's stack, to the hang log."""
        names = "".join(f"  0x{t.ident:x} {t.name}\n" for t in threading.enumerate()
                        if t.ident is not None)
        self._write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} pid {os.getpid()}: {why}\n"
                    f"threads (id, name):\n{names}")
        self._dump()

    # -- the supervisor

    def start(self):
        if self._thread is not None:
            return
        if self._bounded:
            # Every thread the supervisor will ever need, made now while thread creation
            # still works.
            for worker in (self._log, self._probe_call, self._restart_call,
                           self._handover_call):
                worker.start()
        self._arm(self.cfg.dead_man)
        self._thread = threading.Thread(target=self._run, name="watchdog", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        try:
            faulthandler.cancel_dump_traceback_later()
        except Exception:
            pass
        self._native.cancel()

    def _run(self):
        last = self._clock()
        last_slept = self._suspended()
        while not self._stop.wait(self.cfg.tick):
            now = self._clock()
            slept_now = self._suspended()
            slept = (None if slept_now is None or last_slept is None
                     else max(0.0, slept_now - last_slept))
            try:
                self.tick(now, now - last, slept)
            except Exception:
                self._log.log(logging.ERROR, "watchdog tick failed", exc_info=sys.exc_info())
            last, last_slept = now, slept_now

    def observe(self, now, gap, slept=None):
        port = self._mcp_port()
        probe_ok = self._run_probe(port) if port else None
        if probe_ok:
            self._probe_seen = True
        return Observation(now=now, gap=gap, ws_age=self.age_of("ws"),
                           mcp_age=self.age_of("mcp"), probe_ok=probe_ok,
                           leaving=bool(self._leaving()), restarting=bool(self._restarting()),
                           slept=slept, probe_seen=self._probe_seen)

    def _run_probe(self, port):
        host = self._mcp_host()
        if not self._bounded:
            return self._probe(port, self.cfg.probe_timeout, host)
        finished, value = self._probe_call.call(
            lambda: self._probe(port, self.cfg.probe_timeout, host),
            self.cfg.probe_timeout + PROBE_SLACK_SECONDS)
        # A probe that did not come back is the verdict a frozen server earns, not a reason
        # to wait: its worker stays busy and the next tick fails at once too.
        return bool(value) if finished else False

    def _ages(self, obs):
        def fmt(age):
            return "none" if age is None else f"{age:.1f}s"
        return f"heartbeat ages: WebSocket loop {fmt(obs.ws_age)}, MCP HTTP loop {fmt(obs.mcp_age)}"

    def tick(self, now, gap, slept=None):
        """One supervisor tick: observe, decide, act. Public so a test can step it.
        `slept` is how much of `gap` the machine spent suspended (None = unknown)."""
        obs = self.observe(now, gap, slept)
        decision = decide(obs, self._state, self.cfg)
        self._state = decision.state
        self.last_decision = decision
        if decision.rearm:
            self._arm(self.cfg.dead_man)
        if (obs.probe_ok is False and not obs.probe_seen and loops_beating(obs, self.cfg)
                and not self._probe_warned):
            self._probe_warned = True
            self._log.log(logging.WARNING,
                          "watchdog: GET /health over loopback has not answered yet; not "
                          "treating that as a freeze while both loops keep beating")
        why = f"{decision.reason} ({self._ages(obs)})" if decision.reason else ""
        if decision.action is Action.NONE:
            if decision.stall:
                # Forgiving this tick would hide an intermittent freeze: it thaws long
                # enough for every other check to look healthy.
                self.dump(f"stall: {why}")
                self._log.log(logging.WARNING, "watchdog: %s; stacks go to %s", why,
                              getattr(self.hang_file, "name", "the hang log"))
            elif decision.reason and decision.state.bad_ticks == 1:
                self._log.log(logging.WARNING, "watchdog: %s", why)
            return decision
        self._act(decision, why)
        return decision

    def _act(self, decision, why):
        """Dump first, act second, log last: the stacks and the reason go straight into the
        hang file, and nothing here waits on a lock the frozen thread may hold."""
        if decision.action is Action.FORCE_EXIT:
            self.dump(f"forced exit: {why}")
            self._log.log(logging.CRITICAL,
                          "watchdog: still stuck %.0fs after the restart was requested "
                          "(%s); handing the server over and exiting", self.cfg.grace, why)
            self._log.flush(0.5)
            self._hand_over()
            self._exit_process()
            return
        self.dump(f"{why} ({decision.state.bad_ticks} checks, {len(decision.state.stalls)} "
                  f"stalls; {decision.action.value})")
        if decision.action is Action.RESTART:
            self._start_restart()
        self._log.log(logging.CRITICAL, "watchdog: %s for %d checks; stacks go to %s", why,
                      decision.state.bad_ticks, getattr(self.hang_file, "name", "the hang log"))

    def _start_restart(self):
        def run():
            try:
                self._begin_restart("watchdog")
            except Exception as e:
                # The grace period still ends in a forced exit, so a restart that cannot
                # even start does not leave the freeze in place.
                self._log.log(logging.ERROR, "watchdog: the restart could not start (%s: %s)",
                              type(e).__name__, e)

        if not self._bounded:
            run()
            return
        finished, _ = self._restart_call.call(run, RESTART_CALL_SECONDS)
        if not finished:
            self._log.log(logging.ERROR, "watchdog: starting the restart did not return "
                          "within %.0fs", RESTART_CALL_SECONDS)

    def _hand_over(self):
        """Release the instance lock and start a successor, then let the caller end the
        process. Bounded: whatever is stuck in here (a CreateProcess waiting on the same
        contention that froze the server) must not keep the process alive. A successor that
        was not started is no worse than before: the next launcher starts one."""
        if self._hand_over_fn is None:
            return

        def run():
            try:
                self._hand_over_fn()
            except Exception as e:
                self._log.log(logging.ERROR, "watchdog: the hand-over failed (%s: %s)",
                              type(e).__name__, e)

        if not self._bounded:
            run()
            return
        finished, _ = self._handover_call.call(run, self.cfg.handover_wait)
        self._log.log(logging.CRITICAL if not finished else logging.WARNING,
                      "watchdog: the hand-over %s",
                      "finished" if finished
                      else f"did not finish within {self.cfg.handover_wait:.0f}s; exiting anyway")


# --- Process setup --------------------------------------------------------------------

def open_hang_log(log_file):
    """Open the hang log for appending and turn on faulthandler's crash dumps into it.

    Returns the file object, which must stay referenced for the life of the process
    (faulthandler writes to its descriptor from signal handlers). None when it cannot be
    opened; the server then runs without crash dumps rather than refusing to start.
    """
    path = hang_log_path(log_file)
    try:
        try:
            if os.path.getsize(path) > HANG_LOG_MAX_BYTES:
                os.replace(path, path + ".old")
        except OSError:
            pass
        fh = open(path, "a", buffering=1, encoding="utf-8")
        faulthandler.enable(file=fh, all_threads=True)
        return fh
    except Exception as e:
        logger.warning("Could not open the hang log %s: %s", path, e)
        return None


def start(cfg: Config, *, hang_file, ws_loop, **hooks) -> Watchdog:
    """Build a Watchdog, attach the running WebSocket loop and start the supervisor."""
    hooks.setdefault("bounded", True)
    dog = Watchdog(cfg, hang_file=hang_file, **hooks)
    dog.attach_loop("ws", ws_loop)
    dog.start()
    return dog
