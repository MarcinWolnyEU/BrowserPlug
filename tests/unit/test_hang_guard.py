"""The server must not freeze, or sit half dead holding its lock (2026-10-02, 14 minutes).

Synchronous work on one event loop froze the whole process: a regex over a client's own argv,
run by the identity lookup on the uvicorn loop, held the GIL for minutes. Both loops stopped,
the process kept the single-instance lock, and every client waited on it.

Process tests here run real `server/server.py` processes on sandbox ports (the same fenced
Sandbox as test_server_process_lifecycle: scratch state dir, scratch log, test ports):

  * a client whose argv is hostile must not delay /health or the WebSocket port's /reachable;
  * a loop wedged for good must end in a hang-log dump and a process that no longer holds its
    lock, by each of the three routes: the watchdog's restart, its forced exit, and the
    dead-man timer that works when the GIL itself is held.

The in-process tests at the end hold server_info to the same rule as /health: what walks the
process table runs on a worker thread.
"""

import asyncio
import json
import os
import subprocess
import sys
import textwrap
import threading
import time
import urllib.request
from types import SimpleNamespace

import pytest

import test_imports  # noqa: F401
from server import preflight
from server.server import BrowserPlugServer
from server.tools.server_info import make_server_info_tool
from test_server_process_lifecycle import Sandbox, ROOT, SERVER_SCRIPT, exit_code

WATCHDOG_FAST = {
    "BROWSERPLUG_WATCHDOG_TICK": "0.5",
    "BROWSERPLUG_WATCHDOG_STALE": "2",
    "BROWSERPLUG_WATCHDOG_BAD_TICKS": "3",
    "BROWSERPLUG_WATCHDOG_GRACE": "4",
    "BROWSERPLUG_WATCHDOG_DEAD_MAN": "120",
}


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    from browserplug_shared.config_store import STATE_DIR_ENV
    box = Sandbox(tmp_path)
    monkeypatch.setenv(STATE_DIR_ENV, str(box.state))
    try:
        yield box
    finally:
        box.kill_everything()


def get_json(url, timeout=3.0):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    start = time.monotonic()
    with opener.open(url, timeout=timeout) as r:
        return json.loads(r.read()), time.monotonic() - start


def probe_both(box, timeout=3.0):
    """(seconds for /health on the MCP loop, seconds for /reachable on the WebSocket loop)."""
    _, mcp = get_json(f"http://127.0.0.1:{box.mcp_port}/health?identity=1", timeout)
    body, ws = get_json(f"http://127.0.0.1:{box.ws_port}/reachable", timeout)
    assert body.get("reachable") is True
    return mcp, ws


# --- the hostile client ---------------------------------------------------------------

# A direct-HTTP MCP client that names itself (its own pid) in the header the stdio launcher
# uses, so the server describes THIS process: its argv goes through the secret masking. The
# argv is a few long runs of keyword-bearing identifier characters, the shape that took the
# old pattern minutes (4000 characters: 51 s). Even with each argument cut to 1024 characters
# first, the old pattern needs about 0.8 s per argument and the description reads about four
# of them, so a stall of 3 s tells the old pattern from the linear one with a wide margin.
HOSTILE_CLIENT = textwrap.dedent('''
    import http.client, json, os, sys
    c = http.client.HTTPConnection("127.0.0.1", int(sys.argv[1]), timeout=120)
    c.request("POST", "/mcp", json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
              {"Content-Type": "application/json", "Mcp-Session-Id": "f" * 32,
               "MCP-Protocol-Version": "2025-06-18",
               "Accept": "application/json, text/event-stream",
               "X-BrowserPlug-Client-Pid": str(os.getpid())})
    print(c.getresponse().status, flush=True)
''')


def test_a_client_with_a_hostile_command_line_does_not_stall_either_loop(sandbox):
    sandbox.start()
    sandbox.wait_healthy()
    client = subprocess.Popen(
        [sys.executable, "-c", HOSTILE_CLIENT, str(sandbox.mcp_port), *["key" * 1300] * 6],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        worst = 0.0
        deadline = time.monotonic() + 8.0
        polls = 0
        while time.monotonic() < deadline:
            mcp, ws = probe_both(sandbox)
            worst = max(worst, mcp, ws)
            polls += 1
            time.sleep(0.1)
        out, _ = client.communicate(timeout=60)
    finally:
        if client.poll() is None:
            client.kill()
    assert out.strip().isdigit(), f"the hostile client got no HTTP answer: {out!r}"
    assert polls >= 20
    # The old pattern stalled a loop for about 3 s; a busy machine adds a few tens of ms to a
    # healthy probe (0.5 s was tripped by a parallel test run), so 1 s sits between the two.
    assert worst < 1.0, (f"a loop stalled for {worst:.1f}s while a client's command line was "
                         f"being described")


# --- a loop wedged for good -----------------------------------------------------------

# Runs server.py after a hook that wedges a loop a moment after the watchdog starts. The
# replacement a restart spawns runs plain server.py, so it is healthy.
#   io   : time.sleep on a loop. The GIL stays free, so the watchdog's thread keeps running.
#   gil  : a regex with catastrophic backtracking. It holds the GIL, the way the incident
#          did, so only faulthandler's C timer can act.
WEDGE_RUNNER = textwrap.dedent('''
    import re, runpy, sys, threading, time
    root, server_py, which, how, mcp_port, ws_port = sys.argv[1:7]
    sys.path[:0] = [root + "/shared-py", root]
    from server.runtime import lifecycle
    
    def wedged_here_with_the_gil():
        re.match(r"(a+)+$", "a" * 60 + "b")

    def wedged_here_in_sleep():
        time.sleep(600)

    block = wedged_here_with_the_gil if how == "gil" else wedged_here_in_sleep
    original = lifecycle.ServerLifecycle.start_watchdog

    def start_watchdog(self):
        original(self)
        def wedge():
            loop = self.ws_loop if which == "ws" else self._mcp_loop
            loop.call_soon_threadsafe(block)
        threading.Timer(2.5, wedge).start()

    lifecycle.ServerLifecycle.start_watchdog = start_watchdog
    sys.argv = [server_py, "--port", ws_port, "--mcp-port", mcp_port]
    runpy.run_path(server_py, run_name="__main__")
''')


def start_wedged(box, which, how, **env):
    """Start a sandbox server whose `which` loop ("ws" or "mcp") wedges 2.5 s after the
    watchdog starts. Returns the process."""
    runner = box.root / "wedge_runner.py"
    runner.write_text(WEDGE_RUNNER, encoding="utf-8")
    argv = [sys.executable, str(runner), str(ROOT), str(SERVER_SCRIPT), which, how,
            str(box.mcp_port), str(box.ws_port)]
    out = open(box.root / "console-wedge.txt", "wb")
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=out,
                            env=box.env(**{**WATCHDOG_FAST, **env}), cwd=str(ROOT))
    out.close()
    box.procs.append(proc)
    box.seen_pids.add(proc.pid)
    return proc


def hang_log(box):
    path = box.root / "server.hang.log"
    return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""


def wait_until(predicate, timeout, step=0.2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(step)
    return None


def lock_is_free(port):
    """Whether a fresh interpreter gets the instance lock for `port`. The lock directory is
    the sandbox's, taken from this process's environment (the fixture sets it)."""
    probe = ("import sys; from server.runtime import lifecycle; "
             f"sys.exit(0 if lifecycle.acquire_instance_lock({port}) else 3)")
    env = {**os.environ,
           "PYTHONPATH": os.pathsep.join([str(ROOT), str(ROOT / "shared-py")])}
    done = subprocess.run([sys.executable, "-c", probe], cwd=str(ROOT), capture_output=True,
                          timeout=30, env=env)
    return done.returncode == 0


def test_a_wedged_websocket_loop_ends_in_a_dump_a_dead_process_and_a_successor(sandbox):
    """The watchdog's thread is alive (the GIL is free) but the loop it would restart on is
    the stuck one, so the restart cannot run: after the grace period it dumps, releases the
    lock, spawns a successor and terminates the process. The incident of 2026-10-09 left
    the old process alive and the lock held; here the old one must be gone and a new
    server must answer on the same port."""
    proc = start_wedged(sandbox, "ws", "io")
    old = sandbox.wait_healthy()["server_pid"]
    code = exit_code(proc, timeout=40)
    assert code == 1, sandbox.diagnostics()
    log = hang_log(sandbox)
    assert "forced exit" in log and "WebSocket loop has not run" in log, log[-3000:]
    assert "wedged_here_in_sleep" in log, "the dump must show where the loop was"
    sandbox.wait_gone(old)
    new = wait_until(
        lambda: (sandbox.health(timeout=2.0) or {}).get("server_pid") not in (None, old),
        timeout=60)
    assert new, "no successor answered /health\n" + sandbox.diagnostics()
    assert "Restart: replacement spawned" in sandbox.log_text()


def test_a_wedged_mcp_loop_is_replaced_by_a_restart(sandbox):
    """Only the uvicorn loop is stuck, so the WebSocket loop can run the graceful restart: a
    new server answers on the same port and the old process is gone."""
    # A grace period longer than the restart itself, as in production: the restart must be
    # what ends the freeze here, not the forced exit behind it.
    proc = start_wedged(sandbox, "mcp", "io", BROWSERPLUG_WATCHDOG_GRACE="60")
    first = sandbox.wait_healthy()
    old = first["server_pid"]
    new = wait_until(lambda: (sandbox.health(timeout=2.0) or {}).get("server_pid") not in (None, old),
                     timeout=60)
    assert new, "no replacement answered /health\n" + sandbox.diagnostics()
    sandbox.wait_gone(old)
    log = hang_log(sandbox)
    assert "MCP HTTP loop has not run" in log, log[-3000:]
    assert "Restart: replacement spawned" in sandbox.log_text()
    assert "forced exit" not in log
    proc.wait(timeout=10)


def test_a_loop_that_holds_the_gil_is_ended_by_the_dead_man_timer(sandbox):
    """The incident's own shape: the supervisor thread cannot run at all. faulthandler's C
    timer dumps every thread and exits, and the dump names the call that held the GIL."""
    proc = start_wedged(sandbox, "mcp", "gil", BROWSERPLUG_WATCHDOG_DEAD_MAN="4")
    sandbox.wait_healthy()
    code = exit_code(proc, timeout=40)
    assert code == 1, sandbox.diagnostics()
    log = hang_log(sandbox)
    assert "Timeout (0:00:04)!" in log, log[-3000:]
    assert "wedged_here_with_the_gil" in log, "the dump must show the call that held the GIL"
    assert not sandbox.health()
    assert lock_is_free(sandbox.ws_port)


def test_a_healthy_server_is_not_touched_by_a_watchdog_with_tight_numbers(sandbox):
    """The same fast settings on a server nobody wedges: it must still be the same process
    after the watchdog has had many ticks."""
    sandbox.start(**WATCHDOG_FAST)
    first = sandbox.wait_healthy()
    time.sleep(8)
    again = sandbox.health()
    assert again and again["server_pid"] == first["server_pid"]
    assert "watchdog: " not in sandbox.log_text()


# --- the probe reaches the address the server really listens on ---------------------------

def health_over_ipv6(box, timeout=1.0):
    """/health through [::1]: with `--ws-host ::` uvicorn listens on :: only (IPV6_V6ONLY on
    Windows), so the sandbox's own 127.0.0.1 helpers cannot reach it."""
    try:
        data, _ = get_json(f"http://[::1]:{box.mcp_port}/health?identity=1", timeout)
    except OSError:
        return None
    if data.get("server_pid"):
        box.seen_pids.add(data["server_pid"])
    return data


def test_a_server_bound_to_ipv6_is_not_restarted_by_its_own_watchdog(sandbox):
    """`--ws-host ::` is supported. The probe only knew 127.0.0.1, failed on every tick, and
    restarted a healthy server every 30 to 35 seconds for ever."""
    from server.runtime import watchdog
    sandbox.start("--ws-host", "::", **WATCHDOG_FAST)
    first = wait_until(lambda: health_over_ipv6(sandbox), timeout=60)
    assert first, "the server never answered on [::1]\n" + sandbox.diagnostics()
    assert watchdog.default_probe(sandbox.mcp_port, 3, "::") is True, (
        "the probe must reach a listener bound to ::")
    time.sleep(8)       # 16 ticks; three failed ones used to be enough
    again = health_over_ipv6(sandbox)
    assert again and again["server_pid"] == first["server_pid"], sandbox.diagnostics()
    assert "watchdog: " not in sandbox.log_text()
    assert not hang_log(sandbox)


# --- a freeze that thaws and freezes again --------------------------------------------------

# Holds the GIL (a C call that never lets go) on the uvicorn loop for `hold` seconds, thaws
# for a second, and repeats. Every other check looks healthy at the thaw: the loops beat
# again, the probe answers. Only the supervisor's own lateness shows it.
STUTTER_RUNNER = textwrap.dedent('''
    import ctypes, runpy, sys, threading, time
    root, server_py, mcp_port, ws_port, hold = sys.argv[1:6]
    sys.path[:0] = [root + "/shared-py", root]
    from server.runtime import lifecycle

    def hold_the_gil():
        if sys.platform == "win32":
            ctypes.PyDLL("kernel32").Sleep(int(float(hold) * 1000))
        else:
            ctypes.PyDLL(None).sleep(int(float(hold)))

    original = lifecycle.ServerLifecycle.start_watchdog

    def start_watchdog(self):
        original(self)
        def stutter():
            time.sleep(3)
            for _ in range(12):
                self._mcp_loop.call_soon_threadsafe(hold_the_gil)
                time.sleep(float(hold) + 1.0)
        threading.Thread(target=stutter, daemon=True).start()

    lifecycle.ServerLifecycle.start_watchdog = start_watchdog
    sys.argv = [server_py, "--port", ws_port, "--mcp-port", mcp_port]
    runpy.run_path(server_py, run_name="__main__")
''')


def test_a_server_that_freezes_thaws_and_freezes_again_is_dumped_and_replaced(sandbox):
    """A 3 s stall, a 1 s thaw, repeated. The old supervisor forgave every late tick, so
    nothing was logged, dumped or restarted however long it went on."""
    runner = sandbox.root / "stutter_runner.py"
    runner.write_text(STUTTER_RUNNER, encoding="utf-8")
    argv = [sys.executable, str(runner), str(ROOT), str(SERVER_SCRIPT), str(sandbox.mcp_port),
            str(sandbox.ws_port), "3"]
    out = open(sandbox.root / "console-stutter.txt", "wb")
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=out,
                            cwd=str(ROOT),
                            env=sandbox.env(**{**WATCHDOG_FAST,
                                               "BROWSERPLUG_WATCHDOG_GAP_FORGIVE": "1",
                                               "BROWSERPLUG_WATCHDOG_GRACE": "60"}))
    out.close()
    sandbox.procs.append(proc)
    sandbox.seen_pids.add(proc.pid)
    old = sandbox.wait_healthy()["server_pid"]
    new = wait_until(lambda: (sandbox.health(timeout=2.0) or {}).get("server_pid")
                     not in (None, old), timeout=90)
    assert new, "the stuttering server was never replaced\n" + sandbox.diagnostics()
    sandbox.wait_gone(old)
    log = hang_log(sandbox)
    assert "stalled for about" in log, log[-3000:]
    assert "threads (id, name):" in log, "the stall dumps name the threads"
    assert "forced exit" not in log, "the restart, not the forced exit, must end it"
    text = sandbox.log_text()
    assert "watchdog: the process stalled" in text


# --- off the loop: server_info and the doctor --------------------------------------------

class LoopWatcher:
    """Records the longest gap between 10 ms ticks of the loop it runs on."""

    def __init__(self):
        self.max_gap = 0.0
        self._stop = False

    async def run(self):
        last = time.perf_counter()
        while not self._stop:
            await asyncio.sleep(0.01)
            now = time.perf_counter()
            self.max_gap = max(self.max_gap, now - last)
            last = now

    def start(self):
        self._task = asyncio.ensure_future(self.run())

    async def stop(self):
        self._stop = True
        await self._task


@pytest.mark.asyncio
async def test_server_info_builds_its_report_on_a_worker_thread():
    server = BrowserPlugServer(host="localhost", port=8772, start_mcp=False)
    threads = []
    real = server.get_connections_info

    def slow(*args, **kwargs):
        threads.append(threading.current_thread())
        time.sleep(0.4)
        return real(*args, **kwargs)

    server.get_connections_info = slow
    tool = make_server_info_tool(server.mcp_tools.calls)
    watcher = LoopWatcher()
    watcher.start()
    await asyncio.sleep(0.05)
    text = await tool()
    await watcher.stop()
    assert "Extension" in text
    assert threads and threads[0] is not threading.main_thread()
    assert watcher.max_gap < 0.2, f"server_info blocked the loop for {watcher.max_gap:.2f}s"


@pytest.mark.asyncio
async def test_server_info_runs_its_preflight_checks_on_a_worker_thread(monkeypatch):
    server = BrowserPlugServer(host="localhost", port=8772, start_mcp=False)
    seen = []

    def slow_checks(*args, **kwargs):
        seen.append(threading.current_thread())
        time.sleep(0.4)
        return []

    monkeypatch.setattr(preflight, "run_checks", slow_checks)
    tool = make_server_info_tool(server.mcp_tools.calls)
    watcher = LoopWatcher()
    watcher.start()
    await asyncio.sleep(0.05)
    await tool()
    await watcher.stop()
    assert seen and seen[0] is not threading.main_thread()
    assert watcher.max_gap < 0.2, f"the pre-flight checks blocked the loop for {watcher.max_gap:.2f}s"


@pytest.mark.asyncio
async def test_the_doctor_endpoint_runs_its_checks_off_the_loop(monkeypatch):
    seen = []

    def slow_checks(**kwargs):
        seen.append(threading.current_thread())
        time.sleep(0.4)
        return []

    monkeypatch.setattr(preflight, "run_checks", slow_checks)
    server = SimpleNamespace(mcp_port=2830, get_health_info=lambda: {"service": "browserplug"})
    sent = []

    async def send(message):
        sent.append(message)

    watcher = LoopWatcher()
    watcher.start()
    await asyncio.sleep(0.05)
    await preflight.handle_doctor_request(
        {"type": "http", "method": "GET", "path": "/doctor", "client": ("127.0.0.1", 5555)},
        send, server)
    await watcher.stop()
    assert sent[0]["status"] == 200
    assert seen and seen[0] is not threading.main_thread()
    assert watcher.max_gap < 0.2, f"/doctor blocked the loop for {watcher.max_gap:.2f}s"


@pytest.mark.asyncio
async def test_the_doctor_endpoint_builds_the_health_report_off_the_loop(monkeypatch):
    seen = []

    def slow_health():
        seen.append(threading.current_thread())
        time.sleep(0.4)
        return {"service": "browserplug"}

    monkeypatch.setattr(preflight, "run_checks", lambda **kwargs: [])
    server = SimpleNamespace(mcp_port=2830, get_health_info=slow_health)
    sent = []

    async def send(message):
        sent.append(message)

    watcher = LoopWatcher()
    watcher.start()
    await asyncio.sleep(0.05)
    await preflight.handle_doctor_request(
        {"type": "http", "method": "GET", "path": "/doctor", "client": ("127.0.0.1", 5555)},
        send, server)
    await watcher.stop()
    assert sent[0]["status"] == 200
    assert seen and seen[0] is not threading.current_thread()
    assert watcher.max_gap < 0.2, f"/doctor blocked the loop for {watcher.max_gap:.2f}s"
