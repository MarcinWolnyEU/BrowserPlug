"""server/runtime/watchdog.py: the decision table, the heartbeats and the supervisor.

The policy is a pure function, so most of this is a table: one observation per supervisor
tick in, an action, a re-arm verdict and the next state out. The numbers under test are the
shipped defaults (tick 5 s, stale 15 s, 3 bad ticks, grace 30 s), because the point of the
table is to show what ordinary slowness does NOT trigger.
"""

import asyncio
import dataclasses
import http.server
import logging
import os
import socket
import subprocess
import sys
import threading
import time

import pytest

import test_imports  # noqa: F401
from server.runtime import watchdog
from browserplug_shared.mcp_port import HEALTH_SERVICE
from server.runtime.watchdog import Action, Config, Observation, State, decide
from wait_helper import wait_until

CFG = Config()


def obs(now=0.0, gap=5.0, ws=0.5, mcp=0.5, probe=True, leaving=False, restarting=False,
        slept=None, probe_seen=True):
    return Observation(now=now, gap=gap, ws_age=ws, mcp_age=mcp, probe_ok=probe,
                       leaving=leaving, restarting=restarting, slept=slept,
                       probe_seen=probe_seen)


def run(observations, cfg=CFG, state=None):
    """Feed observations through `decide`, returning every Decision."""
    state = state or State()
    out = []
    for o in observations:
        d = decide(o, state, cfg)
        state = d.state
        out.append(d)
    return out


# --- ordinary operation never acts ---------------------------------------------------

def test_a_healthy_tick_rearms_the_dead_man_and_does_nothing_else():
    d = decide(obs(), State(), CFG)
    assert d.action is Action.NONE and d.rearm is True and d.state == State()


def test_a_loop_that_is_not_there_or_a_server_with_no_http_side_is_not_judged():
    """--stdio and --no-mcp have no MCP loop and no port to probe; before the lifespan has
    run, the MCP loop has no heartbeat yet."""
    d = decide(obs(ws=0.2, mcp=None, probe=None), State(), CFG)
    assert d.action is Action.NONE and d.rearm is True


def test_a_loop_just_inside_the_stale_limit_is_healthy_for_as_long_as_it_stays_there():
    """A long call that keeps a loop busy for under STALE_SECONDS (a navigation waits on
    the extension and does not hold the loop at all, so this is the worst case) never
    accumulates into an action."""
    ticks = [obs(now=5.0 * i, ws=CFG.stale, mcp=CFG.stale) for i in range(500)]
    assert all(d.action is Action.NONE and d.rearm for d in run(ticks))


def test_a_stall_shorter_than_the_threshold_is_forgotten():
    seq = [obs(now=0, ws=20), obs(now=5, ws=25), obs(now=10, ws=0.5), obs(now=15, ws=20),
           obs(now=20, ws=25), obs(now=25, ws=0.5)]
    decisions = run(seq)
    assert [d.action for d in decisions] == [Action.NONE] * 6
    assert decisions[2].state == State() and decisions[5].state == State()


def test_a_probe_that_has_never_answered_is_not_a_freeze_while_both_loops_beat():
    """`--ws-host ::` once made the probe fail for ever (it only knew 127.0.0.1) and the
    watchdog restarted a healthy server every half minute."""
    ticks = [obs(now=5.0 * i, probe=False, probe_seen=False) for i in range(50)]
    assert all(d.action is Action.NONE and d.rearm and d.reason == "" for d in run(ticks))


def test_a_probe_that_never_answered_still_counts_when_a_loop_is_stuck_too():
    third = run([obs(now=i * 5, ws=60, probe=False, probe_seen=False) for i in range(3)])[-1]
    assert third.action is Action.RESTART and "WebSocket loop" in third.reason


def test_a_probe_that_answered_once_and_fails_now_restarts():
    third = run([obs(now=i * 5, probe=False, probe_seen=True) for i in range(3)])[-1]
    assert third.action is Action.RESTART and "/health" in third.reason


def test_a_single_failed_probe_costs_one_rearm_and_nothing_more():
    seq = [obs(probe=False), obs(probe=True)]
    first, second = run(seq)
    assert first.action is Action.NONE and first.rearm is False
    assert second.action is Action.NONE and second.rearm is True and second.state == State()


# --- a frozen loop is acted on only after it persists --------------------------------

def test_two_bad_ticks_only_count_and_the_third_restarts():
    seq = [obs(now=0, ws=30), obs(now=5, ws=35), obs(now=10, ws=40)]
    first, second, third = run(seq)
    assert (first.action, second.action) == (Action.NONE, Action.NONE)
    assert first.rearm is False and second.rearm is False
    assert first.state.bad_ticks == 1 and second.state.bad_ticks == 2
    assert third.action is Action.RESTART and third.rearm is False
    assert "WebSocket loop" in third.reason
    assert third.state.escalated_at == 10


def test_the_mcp_loop_is_named_when_it_is_the_stuck_one():
    third = run([obs(now=i * 5, mcp=60) for i in range(3)])[-1]
    assert third.action is Action.RESTART
    assert "MCP HTTP loop" in third.reason and "WebSocket" not in third.reason


def test_a_probe_that_keeps_failing_with_fresh_heartbeats_restarts_too():
    third = run([obs(now=i * 5, probe=False) for i in range(3)])[-1]
    assert third.action is Action.RESTART and "/health" in third.reason


def test_the_number_of_bad_ticks_is_configurable():
    cfg = Config(bad_ticks=1)
    assert decide(obs(ws=60), State(), cfg).action is Action.RESTART


def test_after_the_restart_the_watchdog_waits_out_the_grace_period_then_forces_an_exit():
    seq = [obs(now=0, ws=60), obs(now=5, ws=60), obs(now=10, ws=60)]
    state = run(seq)[-1].state
    waiting = decide(obs(now=10 + CFG.grace - 1, ws=60), state, CFG)
    assert waiting.action is Action.NONE and waiting.rearm is False
    due = decide(obs(now=10 + CFG.grace, ws=60), waiting.state, CFG)
    assert due.action is Action.FORCE_EXIT


def test_the_watchdogs_own_restart_does_not_hide_the_forced_exit():
    """begin_restart sets the lifecycle's `stopping` flag, so `leaving` is true by the
    time the grace period runs out; the exit must still happen."""
    state = run([obs(now=i * 5, ws=60) for i in range(3)])[-1].state
    due = decide(obs(now=10 + CFG.grace, ws=60, leaving=True, restarting=True), state, CFG)
    assert due.action is Action.FORCE_EXIT


def test_a_loop_that_comes_back_after_the_restart_started_cancels_the_escalation():
    state = run([obs(now=i * 5, ws=60) for i in range(3)])[-1].state
    back = decide(obs(now=20, ws=0.3), state, CFG)
    assert back.action is Action.NONE and back.rearm is True and back.state == State()


def test_a_restart_already_under_way_gets_a_dump_not_a_second_restart():
    seq = [obs(now=i * 5, ws=60, restarting=True) for i in range(3)]
    assert run(seq)[-1].action is Action.DUMP


def test_a_stop_that_is_under_way_is_left_alone_unless_it_hangs_for_good():
    """Heartbeats go quiet while a normal shutdown tears the loops down. That is not a
    freeze, so nothing is escalated; the dead-man timer (not re-armed) is the backstop."""
    decisions = run([obs(now=i * 5, ws=60, mcp=60, leaving=True) for i in range(20)])
    assert all(d.action is Action.NONE and d.rearm is False for d in decisions)


# --- a suspended machine is not a frozen server --------------------------------------

def test_a_tick_that_came_far_too_late_is_forgiven_and_resets_the_count():
    state = State(bad_ticks=2)
    d = decide(obs(gap=CFG.tick + CFG.gap_forgive + 1, ws=3600, mcp=3600, probe=False),
               state, CFG)
    assert d.action is Action.NONE and d.rearm is True and d.state == State()


def test_a_slightly_late_tick_is_not_forgiven():
    d = decide(obs(gap=CFG.tick + CFG.gap_forgive - 1, ws=60), State(bad_ticks=2), CFG)
    assert d.action is Action.RESTART


# --- a stall is not a suspend ---------------------------------------------------------

LATE = CFG.tick + CFG.gap_forgive + 8          # a supervisor 8 s later than the forgiveness


def stall(now):
    """A late tick the machine did not sleep through, every loop already beating again."""
    return obs(now=now, gap=LATE, slept=0.0)


def test_a_late_tick_the_machine_slept_through_is_still_forgiven():
    d = decide(obs(gap=LATE, slept=LATE - 1, ws=3600, mcp=3600, probe=False),
               State(bad_ticks=2), CFG)
    assert d.action is Action.NONE and d.rearm is True and d.state == State() and not d.stall


def test_a_late_tick_on_a_platform_that_cannot_tell_is_forgiven_as_before():
    d = decide(obs(gap=LATE, slept=None, ws=3600), State(bad_ticks=2), CFG)
    assert d.action is Action.NONE and d.rearm is True and d.state == State()


def test_a_late_tick_the_machine_was_awake_for_is_a_stall_and_counts():
    d = decide(stall(100), State(), CFG)
    assert d.stall and d.action is Action.NONE and d.rearm is False
    assert "stalled" in d.reason and d.state.stalls == (100,)


def test_stalls_with_healthy_ticks_between_them_still_add_up_to_a_restart():
    """The reviewer's shape: 18 s frozen, a short thaw, frozen again. Every tick between the
    stalls is healthy and resets the count of bad ticks, so only the stalls can act."""
    state, actions = State(), []
    for i, now in enumerate((100, 125, 150)):
        d = decide(stall(now), state, CFG)
        actions.append(d.action)
        state = d.state
        if i < 2:
            healthy = decide(obs(now=now + 5), state, CFG)
            assert healthy.rearm and healthy.state.bad_ticks == 0
            assert healthy.state.stalls == state.stalls, "a healthy tick keeps the stall history"
            state = healthy.state
    assert actions == [Action.NONE, Action.NONE, Action.RESTART]


def test_stalls_far_apart_are_forgotten():
    state = State()
    for now in (100, 100 + CFG.dead_man, 100 + 2 * CFG.dead_man):
        d = decide(stall(now), state, CFG)
        assert d.action is Action.NONE
        state = decide(obs(now=now + 5), d.state, CFG).state
    assert len(state.stalls) == 1


def test_a_restart_the_stalls_began_ends_in_a_forced_exit_if_the_stalls_continue():
    state = State()
    for now in (100, 125, 150):
        state = decide(stall(now), state, CFG).state
    due = decide(stall(150 + CFG.grace), state, CFG)
    assert due.action is Action.FORCE_EXIT


# --- configuration -------------------------------------------------------------------

def test_the_defaults_leave_room_between_a_stall_and_the_actions():
    """The ordering the module docstring promises: detection, then the forced exit, then
    the dead-man timer, each well after the one before."""
    detect = CFG.stale + CFG.tick * (CFG.bad_ticks - 1)
    forced = detect + CFG.grace
    assert detect <= 40 and forced < CFG.dead_man
    assert CFG.heartbeat * 10 <= CFG.stale, "a loop must miss many beats before it is stale"
    assert CFG.gap_forgive >= CFG.tick


def test_environment_overrides_and_garbage_falls_back():
    cfg = Config.from_env({"BROWSERPLUG_WATCHDOG_TICK": "0.5",
                           "BROWSERPLUG_WATCHDOG_STALE": "3",
                           "BROWSERPLUG_WATCHDOG_BAD_TICKS": "2",
                           "BROWSERPLUG_WATCHDOG_GRACE": "4",
                           "BROWSERPLUG_WATCHDOG_DEAD_MAN": "9",
                           "BROWSERPLUG_WATCHDOG_GAP_FORGIVE": "1.5"})
    assert (cfg.tick, cfg.stale, cfg.bad_ticks, cfg.grace, cfg.dead_man) == (0.5, 3, 2, 4, 9)
    assert cfg.gap_forgive == 1.5
    assert cfg.heartbeat <= cfg.tick
    junk = Config.from_env({"BROWSERPLUG_WATCHDOG_TICK": "soon",
                            "BROWSERPLUG_WATCHDOG_STALE": "-3",
                            "BROWSERPLUG_WATCHDOG_BAD_TICKS": "0"})
    assert junk == Config()


@pytest.mark.parametrize("value,on", [(None, True), ("1", True), ("0", False),
                                      ("off", False), ("False", False)])
def test_the_watchdog_can_be_switched_off(value, on):
    env = {} if value is None else {watchdog.ENABLE_ENV: value}
    assert watchdog.enabled(env) is on


def test_the_hang_log_sits_next_to_the_server_log():
    assert watchdog.hang_log_path("C:/x/browserplug-server.log") == (
        "C:/x/browserplug-server.hang.log")
    assert watchdog.hang_log_path("/tmp/noext") == "/tmp/noext.hang.log"


# --- the supervisor, stepped by hand ---------------------------------------------------

class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class Rig:
    """A Watchdog with everything that touches the process replaced and recorded."""

    def __init__(self, cfg=CFG, port=1, probe=True, host="localhost", **hooks):
        self.clock = FakeClock()
        self.events = []
        self.logs = []
        self.probe_ok = probe
        self.probed = []
        self.dog = watchdog.Watchdog(
            cfg, hang_file=None, clock=self.clock,
            begin_restart=hooks.get("begin_restart") or (lambda t: self.events.append(("restart", t))),
            exit_process=lambda: self.events.append(("exit",)),
            hand_over=hooks.get("hand_over"),
            mcp_port=lambda: port,
            mcp_host=lambda: host,
            leaving=hooks.get("leaving", lambda: False),
            restarting=hooks.get("restarting", lambda: False),
            probe=self._probe,
            arm_dead_man=lambda seconds: self.events.append(("arm", seconds)),
            dump_stacks=lambda: self.events.append(("dump",)))
        self.dog._write = lambda text: self.events.append(
            ("header", text.splitlines()[1]) if text.startswith("\n===")
            else ("note", text.strip()))
        self.dog._log = self       # records instead of logging, in order with the events
        for name in ("ws", "mcp"):
            self.dog._beats[name] = watchdog.Heartbeat(self.clock)

    def _probe(self, port, timeout, bound=None):
        self.probed.append((port, bound))
        return self.probe_ok

    def log(self, level, message, *args, **kwargs):
        self.logs.append((level, message % args if args else message))
        self.events.append(("log", level))

    def flush(self, timeout):
        self.events.append(("flush",))

    def beat(self, *names):
        for n in names:
            self.dog._beats[n].touch()

    def step(self, seconds=5.0, beat=("ws", "mcp"), slept=None):
        self.clock.t += seconds
        self.beat(*beat)
        return self.dog.tick(self.clock.t, seconds, slept)

    def kinds(self):
        return [e[0] for e in self.events]


def test_fresh_heartbeats_rearm_the_dead_man_every_tick():
    rig = Rig()
    for _ in range(3):
        rig.step()
    assert rig.events == [("arm", CFG.dead_man)] * 3


def test_a_silent_loop_gets_a_dump_and_a_restart_named_watchdog_and_no_rearm():
    rig = Rig()
    for _ in range(5):
        rig.step(beat=("mcp",))
    assert "restart" not in rig.kinds() and "dump" not in rig.kinds(), (
        "two bad ticks are only counted")
    rig.step(beat=("mcp",))
    assert "restart" in rig.kinds() and "dump" in rig.kinds()
    assert rig.kinds().index("dump") < rig.kinds().index("restart"), "stacks first"
    assert ("restart", "watchdog") in rig.events
    assert "arm" not in rig.kinds()[rig.kinds().index("dump"):]


def test_the_dump_header_names_the_reason():
    rig = Rig()
    for _ in range(6):
        rig.step(beat=("mcp",))
    assert any(e[0] == "header" and "WebSocket loop" in e[1] for e in rig.events)


def test_a_restart_that_cannot_start_still_ends_in_a_forced_exit():
    def broken(trigger):
        raise RuntimeError("no interpreter")

    rig = Rig(begin_restart=broken)
    for _ in range(6):
        rig.step(beat=("mcp",))
    assert "exit" not in rig.kinds()
    for _ in range(int(CFG.grace / CFG.tick)):
        rig.step(beat=("mcp",))
    acts = [k for k in rig.kinds() if k not in ("header", "log", "flush")]
    assert acts[-2:] == ["dump", "exit"], "the stacks are written before the process goes"


def test_a_server_that_recovers_after_the_restart_began_is_not_killed():
    rig = Rig()
    for _ in range(6):
        rig.step(beat=("mcp",))
    for _ in range(int(CFG.grace / CFG.tick) + 2):
        rig.step()               # both loops beat again
    assert "exit" not in rig.kinds()


def test_a_probe_that_fails_while_the_heartbeats_are_fine_also_holds_the_dead_man_off():
    rig = Rig(probe=True)
    rig.step()
    rig.events.clear()
    rig.probe_ok = False
    rig.step()
    assert "arm" not in rig.kinds()
    rig.probe_ok = True
    rig.step()
    assert [k for k in rig.kinds() if k != "log"] == ["arm"]


def test_a_probe_that_never_answered_is_warned_about_once_and_never_acted_on():
    rig = Rig(probe=False)
    for _ in range(20):
        rig.step()
    assert rig.kinds().count("arm") == 20, "the dead-man stays armed: both loops beat"
    assert "restart" not in rig.kinds() and "dump" not in rig.kinds()
    assert [lvl for lvl, _ in rig.logs] == [logging.WARNING]
    assert "has not answered" in rig.logs[0][1]


def test_a_probe_that_answered_once_then_fails_restarts():
    rig = Rig()
    rig.step()
    rig.probe_ok = False
    for _ in range(3):
        rig.step()
    assert ("restart", "watchdog") in rig.events


def test_the_probe_is_told_which_address_the_listener_is_bound_to():
    rig = Rig(host="::")
    rig.step()
    assert rig.probed == [(1, "::")]


# --- a stall the tick would have forgiven ------------------------------------------------

def test_a_stalled_supervisor_dumps_and_warns_with_the_heartbeat_ages_even_when_it_does_not_act():
    rig = Rig()
    rig.step(seconds=LATE, slept=0.0)
    assert "dump" in rig.kinds() and "restart" not in rig.kinds()
    assert any(e[0] == "header" and "stall" in e[1] for e in rig.events)
    level, text = rig.logs[0]
    assert level == logging.WARNING and "stalled" in text and "heartbeat ages" in text


def test_a_suspend_is_not_dumped_or_logged():
    rig = Rig()
    rig.step(seconds=LATE, slept=LATE - 2)
    assert rig.kinds() == ["arm"] and rig.logs == []


def test_stalls_with_thaws_between_them_restart_the_server():
    """What the reviewer's stutter did: 18 s stalls, a healthy tick after each."""
    rig = Rig()
    for _ in range(3):
        rig.step(seconds=LATE, slept=0.0)
        rig.step()
    assert ("restart", "watchdog") in rig.events


# --- the escalation never waits on logging -----------------------------------------------

def test_the_dump_and_the_restart_come_before_the_log_line():
    rig = Rig()
    for _ in range(6):
        rig.step(beat=("mcp",))
    kinds = rig.kinds()
    first_critical = next(i for i, e in enumerate(rig.events)
                          if e == ("log", logging.CRITICAL))
    assert kinds.index("dump") < kinds.index("restart") < first_critical
    assert any(e[0] == "header" and "WebSocket loop" in e[1] for e in rig.events), (
        "the reason is written to the hang file itself")


def test_the_forced_exit_follows_the_dump_and_the_log_gets_a_moment_before_it():
    rig = Rig(begin_restart=lambda t: None)
    for _ in range(6 + int(CFG.grace / CFG.tick)):
        rig.step(beat=("mcp",))
    tail = rig.events[-4:]
    assert [e[0] for e in tail] == ["dump", "log", "flush", "exit"], tail


class BlockedHandler(logging.Handler):
    """A log handler that never returns: what a frozen thread holding the handler lock does
    to every other thread that logs."""

    def __init__(self):
        super().__init__()
        self.gate = threading.Event()
        self.entered = threading.Event()

    def emit(self, record):
        self.entered.set()
        self.gate.wait(30)


def test_a_freeze_holding_the_logging_lock_does_not_stop_the_restart_or_the_exit():
    """The supervisor thread must never wait on logging: the freeze it is acting on may be
    holding the handler lock. Real logging, real background log thread, a blocked handler."""
    blocked = BlockedHandler()
    wd_logger = logging.getLogger("server.runtime.watchdog")
    old_level = wd_logger.level
    wd_logger.addHandler(blocked)
    wd_logger.setLevel(logging.DEBUG)
    events = []
    cfg = Config(heartbeat=0.02, tick=0.05, stale=0.2, bad_ticks=2, grace=0.3,
                 dead_man=60.0, gap_forgive=5.0)
    dog = watchdog.Watchdog(cfg, hang_file=None,
                            begin_restart=lambda t: events.append("restart"),
                            exit_process=lambda: events.append("exit"),
                            arm_dead_man=lambda s: None,
                            dump_stacks=lambda: events.append("dump"),
                            mcp_port=lambda: 1, probe=lambda p, t, b=None: True)
    dog._write = lambda text: None
    dog._beats["ws"] = watchdog.Heartbeat()
    dog._beats["ws"].last -= 100              # a loop that stopped beating long ago
    try:
        blocked.gate.clear()
        dog.start()
        # Provoke the first record from the main thread's point of view: the log thread
        # blocks inside the handler, as a frozen writer would make every logger block.
        threading.Thread(target=wd_logger.warning, args=("held",), daemon=True).start()
        assert blocked.entered.wait(5)
        wait_until(lambda: "restart" in events and "exit" in events,
                   "the escalation got past the blocked log handler (restart, then exit)")
        assert events.index("dump") < events.index("restart") < events.index("exit")
    finally:
        blocked.gate.set()
        dog.stop()
        wd_logger.removeHandler(blocked)
        wd_logger.setLevel(old_level)


def test_no_port_means_no_probe():
    rig = Rig(port=None, probe=False)
    rig.step()
    assert rig.kinds() == ["arm"]


# --- real loops and a real supervisor thread -------------------------------------------

class LoopThread:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()

    def block(self, seconds):
        self.loop.call_soon_threadsafe(time.sleep, seconds)

    def close(self):
        async def cancel_tasks():
            for task in asyncio.all_tasks():
                if task is not asyncio.current_task():
                    task.cancel()
            await asyncio.sleep(0.05)

        asyncio.run_coroutine_threadsafe(cancel_tasks(), self.loop).result(5)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)
        self.loop.close()


def test_a_heartbeat_on_a_real_loop_stays_fresh_until_the_loop_is_blocked():
    cfg = Config(heartbeat=0.02, tick=0.05, stale=0.3)
    loop = LoopThread()
    try:
        dog = watchdog.Watchdog(cfg, hang_file=None, begin_restart=lambda t: None,
                                exit_process=lambda: None)
        dog.attach_loop("ws", loop.loop)
        wait_until(lambda: dog.age_of("ws") is not None, "the loop's first beat was seen")
        # Polled, not sampled once: a runner that descheduled this thread for 200 ms between
        # two beats would fail a single reading. A loop that stopped beating never recovers.
        wait_until(lambda: dog.age_of("ws") < 0.2, "a free loop beats")
        time.sleep(0.3)
        wait_until(lambda: dog.age_of("ws") < 0.2, "the loop kept beating")
        loop.block(1.0)
        wait_until(lambda: dog.age_of("ws") > 0.5, "a blocked loop stopped beating")
        wait_until(lambda: dog.age_of("ws") < 0.2, "the loop beat again once free")
        assert dog.age_of("mcp") is None
    finally:
        loop.close()


def test_the_supervisor_thread_restarts_then_exits_for_a_loop_that_stays_blocked():
    cfg = Config(heartbeat=0.02, tick=0.05, stale=0.2, bad_ticks=2, grace=0.4,
                 dead_man=60.0, gap_forgive=5.0)
    loop = LoopThread()
    events = []
    dumps = []
    try:
        dog = watchdog.Watchdog(
            cfg, hang_file=None,
            begin_restart=lambda t: events.append(("restart", t)),
            exit_process=lambda: events.append(("exit",)),
            arm_dead_man=lambda s: None, dump_stacks=lambda: dumps.append(1))
        dog._write = lambda text: None
        dog.attach_loop("ws", loop.loop)
        wait_until(lambda: dog.age_of("ws") is not None, "the loop's first beat was seen")
        dog.start()
        loop.block(3.0)
        wait_until(lambda: ("restart", "watchdog") in events, "the supervisor restarted")
        wait_until(lambda: ("exit",) in events, "the supervisor forced the exit")
        assert events.index(("restart", "watchdog")) < events.index(("exit",))
        assert len(dumps) >= 2, "one dump with the restart and one before the forced exit"
    finally:
        dog.stop()
        loop.close()


def test_a_busy_but_not_blocked_loop_is_left_alone():
    """Many short callbacks keep a loop busy; it still beats between them.

    The next callback is queued only after the previous one finished. Queueing one every
    60 ms regardless made the loop's queue grow whenever time.sleep(0.05) ran long (macOS
    sleeps 5-10 ms over, so the loop was saturated, not busy) and the heartbeat, which
    waits behind everything already queued, went stale."""
    cfg = Config(heartbeat=0.02, tick=0.05, stale=0.3, bad_ticks=2, grace=0.2,
                 dead_man=60.0, gap_forgive=5.0)
    loop = LoopThread()
    events = []
    try:
        dog = watchdog.Watchdog(cfg, hang_file=None,
                                begin_restart=lambda t: events.append(t),
                                exit_process=lambda: events.append("exit"),
                                arm_dead_man=lambda s: None, dump_stacks=lambda: None)
        dog.attach_loop("ws", loop.loop)
        dog.start()
        finished = threading.Event()

        def busy():
            time.sleep(0.05)
            finished.set()
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            finished.clear()
            loop.loop.call_soon_threadsafe(busy)
            assert finished.wait(2.0)
            time.sleep(0.01)
        assert events == []
    finally:
        dog.stop()
        loop.close()


# --- where the probe goes --------------------------------------------------------------

@pytest.mark.parametrize("bound,expected", [
    (None, ("127.0.0.1",)),
    ("0.0.0.0", ("127.0.0.1",)),
    ("localhost", ("127.0.0.1", "::1")),
    ("::", ("::1", "127.0.0.1")),
    ("[::]", ("::1", "127.0.0.1")),
    ("::1", ("::1",)),
    ("127.0.0.1", ("127.0.0.1",)),
])
def test_the_probe_goes_to_the_loopback_of_the_family_the_listener_is_bound_to(bound, expected):
    assert watchdog.probe_hosts(bound) == expected


class _Health(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = ('{"service": "%s"}' % HEALTH_SERVICE).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class _V6Only(http.server.HTTPServer):
    address_family = socket.AF_INET6


@pytest.fixture
def v6_health_server():
    try:
        server = _V6Only(("::1", 0), _Health)
    except OSError:
        pytest.skip("no IPv6 loopback on this machine")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def test_the_default_probe_reaches_a_server_listening_on_ipv6_only(v6_health_server):
    port = v6_health_server
    assert watchdog.default_probe(port, 3, "::") is True
    assert watchdog.default_probe(port, 3, "localhost") is True
    assert watchdog.default_probe(port, 3, "127.0.0.1") is False, "refused: not on IPv4"


def _loopback_pair_on_one_port(attempts=50):
    """Two listening sockets on one port number: 127.0.0.1 and [::1].

    "localhost" names both, and the probe tries them in turn. The two listeners are separate
    sockets, so another process can take the number on IPv6 between the IPv4 bind and the
    IPv6 one; on a collision the helper picks a new number. It sets no SO_REUSEADDR, which on
    Windows would let a second socket share the port.

    A machine without an IPv6 loopback fails the test; it does not skip. Without [::1] the
    probe has no second address to wait on, so the bug could not show and a green run would
    prove nothing.
    """
    try:
        with socket.socket(socket.AF_INET6) as probe:
            probe.bind(("::1", 0))
    except OSError as e:
        pytest.fail(f"this test needs an IPv6 loopback ([::1]) and the machine has none: {e}")
    last = None
    for _ in range(attempts):
        v4 = socket.socket(socket.AF_INET)
        v6 = socket.socket(socket.AF_INET6)
        try:
            v4.bind(("127.0.0.1", 0))
            v6.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            v6.bind(("::1", v4.getsockname()[1]))
        except OSError as e:
            last = e
            v4.close()
            v6.close()
            continue
        v4.listen()
        v6.listen()
        return v4, v6
    pytest.fail(f"no port was free on both 127.0.0.1 and [::1] after {attempts} tries: {last}")


def _connections_waiting(listener):
    """How many connections the kernel has queued on `listener`, none of them accepted.

    A connect() that succeeds has already completed the handshake, so its attempt is in this
    queue by the time the probe moves on. Counting needs no thread and no timing."""
    listener.setblocking(False)
    count = 0
    while True:
        try:
            conn, _ = listener.accept()
        except (BlockingIOError, InterruptedError):
            return count
        conn.close()
        count += 1


def test_a_listener_that_accepts_and_never_answers_is_a_failed_probe_without_a_second_try():
    """Both addresses of "localhost" hold a listener that never answers, so a probe that
    moved on to the second address after the first timed out would wait twice. A timing check
    cannot see that on POSIX when only one address listens, because a refused [::1] costs
    nothing. The number of connections made can: exactly one, to whichever address the probe
    tried first."""
    v4, v6 = _loopback_pair_on_one_port()
    try:
        port = v4.getsockname()[1]
        assert watchdog.default_probe(port, 1.0, "localhost") is False
        attempts = {"127.0.0.1": _connections_waiting(v4), "::1": _connections_waiting(v6)}
        assert sum(attempts.values()) == 1, \
            f"one timeout, not one per address; connections made per address: {attempts}"
    finally:
        v4.close()
        v6.close()


# --- suspend versus stall, on the real supervisor thread --------------------------------

class JumpClock:
    """time.monotonic plus an offset a test can jump: a supervisor that was late."""

    def __init__(self):
        self.offset = 0.0
        self.slept = 0.0

    def __call__(self):
        return time.monotonic() + self.offset


def test_the_supervisor_thread_counts_stalls_but_forgives_a_real_suspend():
    cfg = Config(heartbeat=0.02, tick=0.05, stale=0.5, bad_ticks=3, grace=60.0,
                 dead_man=60.0, gap_forgive=0.5)
    clock = JumpClock()
    events = []
    dog = watchdog.Watchdog(cfg, hang_file=None, clock=clock, suspended=lambda: clock.slept,
                            begin_restart=lambda t: events.append("restart"),
                            exit_process=lambda: None, arm_dead_man=lambda s: None,
                            dump_stacks=lambda: events.append("dump"))
    dog._write = lambda text: None

    # The jumps are applied by the supervisor thread itself, right after one of its ticks, so
    # its two reads of the world (the clock, then the suspend counter) never straddle a jump
    # made from the test thread. Each jump then waits for the next tick to complete: that
    # tick sampled the clock after the jump, which is what "the supervisor ticked after it"
    # means. A fixed sleep stood in for this, and a starved runner could skip a whole tick.
    ticks = []                  # appended after each tick completes
    todo = []                   # (slept, offset) jumps waiting for the end of a tick
    applied = []                # tick count at which each jump was applied
    real_tick = dog.tick

    def tick_then_jump(now, gap, slept=None):
        try:
            return real_tick(now, gap, slept)
        finally:
            ticks.append(now)
            if todo:
                asleep, offset = todo.pop()
                clock.slept += asleep
                clock.offset += offset
                applied.append(len(ticks))

    dog.tick = tick_then_jump

    def jump(asleep, offset):
        todo.append((asleep, offset))
        wait_until(lambda: not todo, "the supervisor applied the clock jump")
        seen = applied[-1]
        wait_until(lambda: len(ticks) > seen, "a supervisor tick ran after the clock jump")

    try:
        dog.start()
        for _ in range(4):                     # suspends: the clock jumps, the machine slept
            jump(30, 30)
        assert events == [], "a machine that slept is not a frozen server"
        for _ in range(3):                     # stalls: the clock jumps, nobody slept
            jump(0, 10)
        wait_until(lambda: "restart" in events,
                   "three stalls with healthy ticks between them restarted the server")
        assert "dump" in events
    finally:
        dog.stop()


def test_the_platform_suspend_clock_is_a_non_negative_number_or_unknown():
    first = watchdog.suspended_seconds()
    if first is None:
        pytest.skip("no suspend clock on this platform")
    time.sleep(0.05)
    second = watchdog.suspended_seconds()
    assert 0 <= first <= second + 0.01


def test_macos_reads_the_suspend_time_from_the_two_unadjusted_mach_clocks(monkeypatch):
    """CLOCK_MONOTONIC is frequency-adjusted and can drift the difference below zero; the
    RAW pair (continuous minus absolute time) is exact."""
    clocks = {"cont": 1000.0, "up": 400.0, "adjusted": 999.0}
    monkeypatch.setattr(watchdog.sys, "platform", "darwin")
    monkeypatch.setattr(watchdog.time, "CLOCK_MONOTONIC_RAW", "cont", raising=False)
    monkeypatch.setattr(watchdog.time, "CLOCK_UPTIME_RAW", "up", raising=False)
    monkeypatch.setattr(watchdog.time, "CLOCK_MONOTONIC", "adjusted", raising=False)
    monkeypatch.setattr(watchdog.time, "clock_gettime", lambda clock: clocks[clock],
                        raising=False)
    assert watchdog.suspended_seconds() == 600.0


def test_macos_reads_the_awake_clock_first_and_never_reports_negative_suspend(monkeypatch):
    """On a host that never slept the two clocks differ only by the gap between the reads.
    Reading CLOCK_UPTIME_RAW first makes that gap add to the result; a pair that still
    crosses is clamped at 0."""
    reads = []
    values = {"cont": 500.0, "up": 500.0}
    monkeypatch.setattr(watchdog.sys, "platform", "darwin")
    monkeypatch.setattr(watchdog.time, "CLOCK_MONOTONIC_RAW", "cont", raising=False)
    monkeypatch.setattr(watchdog.time, "CLOCK_UPTIME_RAW", "up", raising=False)

    def clock_gettime(clock):
        reads.append(clock)
        return values[clock]

    monkeypatch.setattr(watchdog.time, "clock_gettime", clock_gettime, raising=False)
    assert watchdog.suspended_seconds() == 0.0
    assert reads == ["up", "cont"]
    values["cont"] = 499.999                   # a pair that crossed by a millisecond
    assert watchdog.suspended_seconds() == 0.0


# --- a confirmed freeze ends with the lock released, a successor started, the process gone ---

def test_the_forced_exit_hands_the_server_over_before_the_process_ends():
    """The recovery sequence: stacks, the log line, the hand-over (lock released, successor
    spawned), and only then the end of the process."""
    rig = Rig(begin_restart=lambda t: None,
              hand_over=lambda: rig.events.append(("handover",)))
    for _ in range(6 + int(CFG.grace / CFG.tick)):
        rig.step(beat=("mcp",))
    tail = rig.events[-5:]
    assert [e[0] for e in tail] == ["dump", "log", "flush", "handover", "exit"], tail


def test_a_hand_over_that_raises_still_ends_the_process():
    def broken():
        raise OSError("CreateProcess failed")

    rig = Rig(begin_restart=lambda t: None, hand_over=broken)
    for _ in range(6 + int(CFG.grace / CFG.tick)):
        rig.step(beat=("mcp",))
    assert rig.kinds()[-1] == "exit"
    assert any(lvl == logging.ERROR and "hand-over failed" in text for lvl, text in rig.logs)


def test_no_hand_over_hook_means_the_old_behaviour():
    rig = Rig(begin_restart=lambda t: None)
    for _ in range(6 + int(CFG.grace / CFG.tick)):
        rig.step(beat=("mcp",))
    assert rig.kinds()[-1] == "exit" and "handover" not in rig.kinds()


def test_the_handover_deadline_is_configurable():
    assert Config.from_env({"BROWSERPLUG_WATCHDOG_HANDOVER": "3"}).handover_wait == 3
    assert Config().handover_wait == watchdog.HANDOVER_SECONDS
    assert Config.from_env({"BROWSERPLUG_WATCHDOG_HANDOVER": "soon"}).handover_wait == (
        watchdog.HANDOVER_SECONDS)


# --- the supervisor never waits on what a freeze takes away ---------------------------------

BOUNDED = Config(heartbeat=0.02, tick=0.05, stale=0.2, bad_ticks=2, grace=0.3, dead_man=60.0,
                 probe_timeout=0.2, gap_forgive=5.0, handover_wait=0.4)


def bounded_dog(cfg=BOUNDED, **kw):
    kw.setdefault("begin_restart", lambda t: None)
    kw.setdefault("exit_process", lambda: None)
    dog = watchdog.Watchdog(cfg, hang_file=None, bounded=True, arm_dead_man=lambda s: None,
                            dump_stacks=lambda: None, mcp_port=lambda: 1, **kw)
    dog._write = lambda text: None
    for name in ("ws", "mcp"):
        dog._beats[name] = watchdog.Heartbeat()
    return dog


def test_a_probe_that_never_returns_cannot_hold_a_tick_and_counts_as_a_failure():
    """The 2026-10-09 shape: the probe's socket call waits on a kernel that is not
    answering. The tick answers at the deadline, and the next one answers at once."""
    release = threading.Event()
    calls = []

    def stuck(port, timeout, bound=None):
        calls.append(port)
        release.wait(30)
        return True

    # A second wait on the stuck worker would cost the full probe timeout, so the timeout is
    # made long (1 s) and "no waiting again" is bounded at half of it: a regression misses
    # by a wide margin and a descheduled test thread (tens of ms) cannot reach the bound.
    cfg = dataclasses.replace(BOUNDED, probe_timeout=1.0)
    dog = bounded_dog(cfg, probe=stuck)
    dog._probe_seen = True
    try:
        t0 = time.monotonic()
        obs = dog.observe(1.0, 5.0)
        first = time.monotonic() - t0
        assert obs.probe_ok is False
        assert cfg.probe_timeout * 0.9 <= first < cfg.probe_timeout + 2
        t0 = time.monotonic()
        assert dog.observe(2.0, 5.0).probe_ok is False
        assert time.monotonic() - t0 < cfg.probe_timeout / 2, (
            "the worker is still stuck: no waiting again")
        assert calls == [1], "and no second probe was queued behind the stuck one"
    finally:
        release.set()
    wait_until(lambda: dog.observe(3.0, 5.0).probe_ok is True,
               "probing worked again once the stuck call returned")


def test_a_probe_that_answers_is_reported_as_before():
    dog = bounded_dog(probe=lambda port, timeout, bound=None: True)
    assert dog.observe(1.0, 5.0).probe_ok is True
    dog = bounded_dog(probe=lambda port, timeout, bound=None: False)
    assert dog.observe(1.0, 5.0).probe_ok is False


def test_the_threads_the_supervisor_needs_exist_before_any_freeze():
    dog = bounded_dog(probe=lambda port, timeout, bound=None: True)
    before = set(threading.enumerate())
    try:
        dog.start()
        names = {t.name for t in threading.enumerate() if t not in before}
        assert {"watchdog", "watchdog-log", "watchdog-probe", "watchdog-restart",
                "watchdog-handover"} <= names
    finally:
        dog.stop()


def test_a_hand_over_stuck_in_the_kernel_is_abandoned_at_its_deadline():
    release = threading.Event()
    events = []

    def stuck():
        events.append("handover started")
        release.wait(30)

    dog = bounded_dog(hand_over=stuck, exit_process=lambda: events.append("exit"))
    try:
        t0 = time.monotonic()
        dog._hand_over()
        dog._exit_process()
        elapsed = time.monotonic() - t0
        assert events == ["handover started", "exit"]
        # A timed wait can return a few ms before time.monotonic() agrees (Windows timer ticks).
        assert BOUNDED.handover_wait - 0.05 <= elapsed < BOUNDED.handover_wait + 2
    finally:
        release.set()


def test_a_restart_call_that_never_returns_cannot_hold_the_tick(monkeypatch):
    monkeypatch.setattr(watchdog, "RESTART_CALL_SECONDS", 0.3)
    release = threading.Event()
    dog = bounded_dog(begin_restart=lambda t: release.wait(30))
    t0 = time.monotonic()
    try:
        dog._start_restart()
        assert time.monotonic() - t0 < watchdog.RESTART_CALL_SECONDS + 2
    finally:
        release.set()


def test_a_whole_recovery_with_the_loop_and_the_probe_faked():
    """Both loops silent and the probe failing, with the real supervisor thread: it dumps,
    asks for the restart, then hands over and ends the process, in that order and in
    seconds."""
    events = []
    release = threading.Event()

    def exit_process():
        # A real exit takes the supervisor with it. Stopping the dog here does the same
        # without waiting for the test thread to notice the event: a slow machine let the
        # supervisor tick again in that gap and hand over twice.
        events.append("exit")
        dog.stop()

    dog = watchdog.Watchdog(
        BOUNDED, hang_file=None, bounded=True, arm_dead_man=lambda s: None,
        dump_stacks=lambda: events.append("dump"), mcp_port=lambda: 1,
        probe=lambda port, timeout, bound=None: release.wait(30) and False,
        begin_restart=lambda t: events.append(("restart", t)),
        hand_over=lambda: events.append("handover"),
        exit_process=exit_process)
    dog._write = lambda text: None
    for name in ("ws", "mcp"):
        dog._beats[name] = watchdog.Heartbeat()
        dog._beats[name].last -= 100            # silent for ever
    dog._probe_seen = True
    try:
        dog.start()
        wait_until(lambda: "exit" in events, "the supervisor handed over and exited")
    finally:
        release.set()
        dog.stop()
    order = [e for e in events if e != "dump"]
    assert order == [("restart", "watchdog"), "handover", "exit"], events
    assert events.index("dump") < events.index(("restart", "watchdog"))


def test_an_exit_that_returns_does_not_hand_the_server_over_again():
    """`decide` answers FORCE_EXIT on every tick while the problem lasts. If the exit hook
    comes back (TerminateProcess failed), the next tick must not spawn a second successor.
    Stepped by hand, so no timing is involved."""
    rig = Rig(begin_restart=lambda t: None,
              hand_over=lambda: rig.events.append(("handover",)))
    for _ in range(6 + int(CFG.grace / CFG.tick) + 5):      # five ticks past the first exit
        rig.step(beat=("mcp",))
    assert rig.kinds().count("handover") == 1, rig.events
    assert rig.kinds().count("exit") == 1, rig.events


def test_the_supervisor_thread_hands_over_and_exits_once_even_if_the_exit_returns():
    """The exit hook returns (a test double does not end the process) and the supervisor
    gets several more ticks before the test looks, as it did on the macOS CI runner on
    2026-11-10. Counting probe calls, not sleeping, says when those ticks have happened."""
    events = []
    probes = []
    dog = watchdog.Watchdog(
        BOUNDED, hang_file=None, bounded=True, arm_dead_man=lambda s: None,
        dump_stacks=lambda: None, mcp_port=lambda: 1,
        probe=lambda port, timeout, bound=None: probes.append(1) or False,
        begin_restart=lambda t: events.append(("restart", t)),
        hand_over=lambda: events.append("handover"),
        exit_process=lambda: events.append("exit"))
    dog._write = lambda text: None
    for name in ("ws", "mcp"):
        dog._beats[name] = watchdog.Heartbeat()
        dog._beats[name].last -= 100
    dog._probe_seen = True
    try:
        dog.start()
        wait_until(lambda: "exit" in events, "the supervisor exited", timeout=60)
        seen = len(probes)
        wait_until(lambda: len(probes) >= seen + 6,
                   "the supervisor kept ticking after an exit that returned", timeout=60)
    finally:
        dog.stop()
        dog._thread.join(5)
    assert events.count("handover") == 1 and events.count("exit") == 1, events


# --- ending the process ---------------------------------------------------------------------

def _repo_env():
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return root, dict(os.environ, PYTHONPATH=os.pathsep.join(
        [root, os.path.join(root, "shared-py")]))


def run_child(code, timeout=60):
    root, env = _repo_env()
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          timeout=timeout, env=env, cwd=root)


def test_terminate_process_ends_the_process_with_the_code_and_runs_no_cleanup():
    done = run_child("import atexit; atexit.register(print, 'cleanup ran');"
                     "from server.runtime import watchdog; watchdog.terminate_process(7)")
    assert done.returncode == 7
    assert "cleanup ran" not in done.stdout


GIL_HOLDER = """
import re, sys, time
from server.runtime import watchdog
native = watchdog._NativeDeadMan(cushion=0)
{body}
"""


def test_the_native_dead_man_ends_a_process_whose_gil_is_held():
    """The timer's callback is TerminateProcess itself, so it needs nothing from Python: a
    regex that holds the GIL for minutes does not stop it."""
    native = watchdog._NativeDeadMan(cushion=0)
    assert native.available == (sys.platform == "win32")
    if not native.available:
        return
    t0 = time.monotonic()
    done = run_child(GIL_HOLDER.format(body=(
        "assert native.arm(1.0)\n"
        "re.match(r'(a+)+$', 'a' * 60 + 'b')\n"
        "print('survived')")))
    assert "survived" not in done.stdout
    assert time.monotonic() - t0 < 30


def test_the_native_dead_man_leaves_a_process_that_keeps_re_arming_it():
    native = watchdog._NativeDeadMan(cushion=0)
    if not native.available:
        return
    done = run_child(GIL_HOLDER.format(body=(
        "end = time.monotonic() + 4\n"
        "while time.monotonic() < end:\n"
        "    assert native.arm(1.0)\n"
        "    time.sleep(0.1)\n"
        "native.cancel()\n"
        "print('survived')")))
    assert done.returncode == 0 and "survived" in done.stdout, (done.stdout, done.stderr)


def test_a_cancelled_native_dead_man_does_not_fire():
    native = watchdog._NativeDeadMan(cushion=0)
    if not native.available:
        return
    done = run_child(GIL_HOLDER.format(body=(
        "assert native.arm(0.5)\n"
        "native.cancel()\n"
        "time.sleep(2)\n"
        "print('survived')")))
    assert done.returncode == 0 and "survived" in done.stdout, (done.stdout, done.stderr)


def test_faulthandlers_timer_arms_the_native_one_too():
    rig_native = []

    class Fake:
        def arm(self, seconds):
            rig_native.append(seconds)

        def cancel(self):
            rig_native.append("cancel")

    dog = watchdog.Watchdog(CFG, hang_file=open(os.devnull, "w"), begin_restart=lambda t: None,
                            exit_process=lambda: None)
    dog._native = Fake()
    try:
        dog._arm_faulthandler(1000)
        dog.stop()
    finally:
        import faulthandler
        faulthandler.cancel_dump_traceback_later()
        dog.hang_file.close()
    assert rig_native == [1000, "cancel"]
