"""The shared server as a PROCESS: claiming the instance, racing starters, restarting,
stopping, and rebinding through /ws-bind.

server/runtime/lifecycle.py is the part of the server no in-process test can reach, because its
job is to decide whether a process may start at all, and to end one (`os._exit`). The
tests here therefore start real `server/server.py` processes, and every one of them is
fenced off from the live server:

  * ports come from port_coordinator (free, claimed, outside 2830..2942 and 8772), so
    nothing here can reach or displace the server the user's clients are using;
  * BROWSERPLUG_STATE_DIR, the token file and the log file are scratch paths, so no
    token, config.json or log line lands in the real state directory;
  * BROWSERPLUG_SERVER_PYTHON names this interpreter, so a /restart replacement and any
    launcher a test starts run on it, never building or touching the user's server
    environment (shared-py/browserplug_shared/server_env.py);
  * the single-instance lock is per WebSocket port (`browserplug-instance-<port>.lock`),
    so a test port's lock is a file of its own;
  * anything the tests started is killed at the end, by process tree, whatever a test did.

They assert what the code does today. Where the review (2026-09-29) calls the behaviour a
bug, the docstring says which finding, so the fix phase changes the assertion on purpose.
"""

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import psutil
import pytest

import test_imports  # noqa: F401
from port_coordinator import allocate_port
from server.runtime import lifecycle
from browserplug_shared import device_tokens, request_signing, server_env
from browserplug_shared.config_store import STATE_DIR_ENV
from browserplug_shared.mcp_port import DETACHED_ENV, LOG_FILE_ENV, MCP_PORT_ENV, browserplug_health
from browserplug_shared.ws_token import WS_HOST_ENV, WS_TOKEN_ENV, WS_TOKEN_FILE_ENV

ROOT = Path(__file__).resolve().parents[2]
SERVER_SCRIPT = ROOT / "server" / "server.py"

# A cold start imports the whole server; a busy box takes several seconds over it.
START_TIMEOUT = 45.0


def lock_file_for(ws_port):
    return lifecycle._instance_lock_path(ws_port)


class Sandbox:
    """Everything one test's server processes share: ports, state, logs, and cleanup."""

    def __init__(self, root):
        self.root = Path(root)
        self.ws_port = allocate_port()
        self.mcp_port = allocate_port()
        self.state = self.root / "state"
        self.state.mkdir()
        self.log = self.root / "server.log"
        self.procs = []
        self.seen_pids = set()

    def env(self, **extra):
        env = {k: v for k, v in os.environ.items()
               if k not in (WS_TOKEN_ENV, WS_HOST_ENV, MCP_PORT_ENV, DETACHED_ENV)}
        env.update({
            STATE_DIR_ENV: str(self.state),
            WS_TOKEN_FILE_ENV: str(self.state / "ws-token"),
            LOG_FILE_ENV: str(self.log),
            "PYTHONIOENCODING": "utf-8",
            server_env.SERVER_PYTHON_ENV: sys.executable,
        })
        env.update(extra)
        return env

    def start(self, *args, wait_for_port=False, **env):
        """Spawn `server.py` on this sandbox's ports. Its console output goes to a file."""
        argv = [sys.executable, str(SERVER_SCRIPT), "--port", str(self.ws_port),
                "--mcp-port", str(self.mcp_port), *args]
        if wait_for_port:
            argv.append("--wait-for-port")
        out = open(self.root / f"console-{len(self.procs)}.txt", "wb")
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=out,
                                env=self.env(**env), cwd=str(ROOT))
        out.close()     # the child holds its own handle
        self.procs.append(proc)
        self.seen_pids.add(proc.pid)
        return proc

    def health(self, timeout=1.0):
        data = browserplug_health(self.mcp_port, timeout=timeout)
        if data and data.get("server_pid"):
            self.seen_pids.add(data["server_pid"])
        return data

    def wait_healthy(self, timeout=START_TIMEOUT, not_pid=None):
        """The /health of a server on this sandbox's port; `not_pid` waits out an old one."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            data = self.health()
            if data and data["server_pid"] != not_pid:
                return data
            time.sleep(0.2)
        raise AssertionError(f"no healthy server on :{self.mcp_port} within {timeout:.0f}s\n"
                             + self.diagnostics())

    def wait_gone(self, pid, timeout=20.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not psutil.pid_exists(pid):
                return
            time.sleep(0.1)
        raise AssertionError(f"process {pid} is still alive after {timeout:.0f}s")

    def log_text(self):
        parts = []
        for path in [self.log, *sorted(self.root.glob("console-*.txt")),
                     *sorted(self.root.glob("*startup*")), *sorted(self.root.glob("*restart*"))]:
            try:
                parts.append(f"--- {path.name}\n" + path.read_text(encoding="utf-8",
                                                                     errors="replace"))
            except OSError:
                pass
        return "\n".join(parts)

    def diagnostics(self):
        return self.log_text()[-6000:]

    def post(self, path, body=b"", headers=None, timeout=10):
        """POST to this sandbox's MCP port, proxy-blind like every client in the tree."""
        req = urllib.request.Request(f"http://127.0.0.1:{self.mcp_port}{path}", data=body,
                                     method="POST", headers=headers or {})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def signed(self, method, path, body=None):
        """The X-BrowserPlug-Auth header of a device token minted from this sandbox's
        master token (the server writes it on first start). A request with a body passes
        it: the signature covers its bytes."""
        master = (self.state / "ws-token").read_text(encoding="utf-8").strip()
        label, secret = device_tokens.parse(device_tokens.issue(master))
        return {request_signing.AUTH_HEADER: request_signing.sign(label, secret, method, path, body=body)}

    def kill_everything(self):
        """Kill every process this sandbox started or saw serving, with its children.
        Restart replacements are detached from their parent, so the pids the health probe
        reported are what finds them, plus whoever listens on the sandbox's ports."""
        for port in (self.ws_port, self.mcp_port):
            pid = lifecycle.pid_listening_on_port(port)
            if pid:
                self.seen_pids.add(pid)
        for proc in self.procs:
            self.seen_pids.add(proc.pid)
        victims = []
        for pid in self.seen_pids:
            try:
                p = psutil.Process(pid)
                victims += p.children(recursive=True) + [p]
            except psutil.Error:
                pass
        for p in victims:
            try:
                p.kill()
            except psutil.Error:
                pass
        psutil.wait_procs(victims, timeout=10)
        for proc in self.procs:
            try:
                proc.wait(timeout=5)    # reap, or the runner warns about a live child
            except subprocess.TimeoutExpired:
                pass
        try:
            os.unlink(lock_file_for(self.ws_port))
        except OSError:
            pass


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    box = Sandbox(tmp_path)
    # The instance lock lives in the state dir, so this process (which takes and probes
    # locks on the sandbox's ports) must look at the same directory the servers use.
    monkeypatch.setenv(STATE_DIR_ENV, str(box.state))
    try:
        yield box
    finally:
        box.kill_everything()


class PortSquatter:
    """A listening socket on a port, the way an unrelated program holds one. It accepts and
    drops every connection: the server under test probes the port by connecting, and a
    listener that never accepts fills its backlog after a few probes, after which the
    probe times out and reads as "nothing here"."""

    def __init__(self, port):
        self.sock = socket.socket()
        # Exclusive on Windows: the bind must fail loudly if anything else has the port.
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        self.sock.bind(("127.0.0.1", port))
        self.sock.listen(8)
        self._thread = threading.Thread(target=self._accept_forever, daemon=True)
        self._thread.start()

    def _accept_forever(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return      # release() closed the listener
            conn.close()

    def release(self):
        self.sock.close()
        self._thread.join(timeout=5)


def exit_code(proc, timeout=45.0):
    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        raise AssertionError(f"process {proc.pid} did not exit within {timeout:.0f}s") from None


# --- the instance lock, in process ----------------------------------------------------

@pytest.fixture
def instance_lock_port():
    """A port of the test's own to lock, with this process's lock handle put back."""
    port = allocate_port()
    lifecycle.release_instance_lock()
    try:
        yield port
    finally:
        lifecycle.release_instance_lock()
        try:
            os.unlink(lock_file_for(port))
        except OSError:
            pass


def other_process_can_lock(port, timeout=30):
    """Whether a fresh interpreter gets the lock for `port` (it is a per-process authority)."""
    probe = ("import sys; from server.runtime import lifecycle; "
             f"sys.exit(0 if lifecycle.acquire_instance_lock({port}) else 3)")
    proc = subprocess.run([sys.executable, "-c", probe], cwd=str(ROOT), timeout=timeout,
                          capture_output=True, env={**os.environ, "PYTHONPATH": os.pathsep.join([str(ROOT), str(ROOT / "shared-py")])})
    assert proc.returncode in (0, 3), proc.stderr.decode(errors="replace")
    return proc.returncode == 0


def test_the_lock_is_held_against_other_processes_until_released(instance_lock_port):
    port = instance_lock_port
    assert lifecycle.acquire_instance_lock(port) is True
    assert other_process_can_lock(port) is False, "a second process must not get the lock"
    assert lifecycle.acquire_instance_lock(port) is True, "asking again is idempotent"

    lifecycle.release_instance_lock()

    assert other_process_can_lock(port) is True, "released means free for the next process"


def test_release_without_a_lock_is_a_no_op(instance_lock_port):
    lifecycle.release_instance_lock()
    lifecycle.release_instance_lock()


def test_the_lock_is_per_websocket_port(instance_lock_port):
    """Two servers on different WebSocket ports are different instances; that is what
    lets these tests run beside the live server."""
    other = allocate_port()
    assert lifecycle.acquire_instance_lock(instance_lock_port) is True
    assert other_process_can_lock(other) is True
    assert lock_file_for(other) != lock_file_for(instance_lock_port)
    try:
        os.unlink(lock_file_for(other))
    except OSError:
        pass


def test_waiting_for_the_lock_returns_when_the_holder_lets_go(instance_lock_port):
    """A restart replacement waits for the outgoing server's lock (--wait-for-port)."""
    port = instance_lock_port
    holder_ready, holder_release = threading.Event(), threading.Event()
    result = {}

    def hold_in_another_process():
        # A separate process, because the lock is per process: this thread's own handle
        # would satisfy the waiter trivially.
        code = ("import sys, time; from server.runtime import lifecycle; "
                f"assert lifecycle.acquire_instance_lock({port}); "
                "print('held', flush=True); sys.stdin.readline()")
        proc = subprocess.Popen([sys.executable, "-c", code], cwd=str(ROOT),
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                env={**os.environ, "PYTHONPATH": os.pathsep.join([str(ROOT), str(ROOT / "shared-py")])})
        assert proc.stdout.readline().strip() == b"held"
        holder_ready.set()
        holder_release.wait(30)
        proc.stdin.write(b"\n")
        proc.stdin.flush()
        proc.wait(timeout=30)
        proc.stdin.close()
        proc.stdout.close()

    holder = threading.Thread(target=hold_in_another_process)
    holder.start()
    try:
        assert holder_ready.wait(30)
        # Held: a short wait gives up and says so.
        started = time.monotonic()
        assert lifecycle.acquire_instance_lock_waiting(
            port, time.monotonic() + 0.6) is False
        assert time.monotonic() - started >= 0.5, "it really waited out its deadline"

        # Released mid-wait: the waiter gets it.
        threading.Timer(0.8, holder_release.set).start()
        result["got"] = lifecycle.acquire_instance_lock_waiting(port, time.monotonic() + 20)
    finally:
        holder_release.set()
        holder.join(30)
    assert result["got"] is True


def test_an_uncreatable_lock_file_does_not_block_startup(instance_lock_port, monkeypatch, tmp_path):
    """If the lock file cannot even be opened, startup goes on and the port checks decide
    (documented in acquire_instance_lock). Pinned because it is a deliberate fail-open."""
    monkeypatch.setattr(lifecycle, "_instance_lock_path", lambda port: str(tmp_path))
    assert lifecycle.acquire_instance_lock(instance_lock_port) is True
    assert lifecycle._INSTANCE_LOCK_FH is None, "nothing was locked, so nothing to release"


# --- starting, and racing to start -----------------------------------------------------

def test_a_started_server_answers_on_its_own_ports_and_the_lock_is_taken(sandbox):
    proc = sandbox.start()
    health = sandbox.wait_healthy()

    assert health["service"] == "browserplug"
    assert health["mcp_port"] == sandbox.mcp_port and health["ws_port"] == sandbox.ws_port
    assert proc.poll() is None
    # /health can answer a beat before the WebSocket listener binds. The old connect probe
    # took ~2 s per call and so hid that gap; the bind probe is instant, so wait for it.
    deadline = time.monotonic() + 10
    while not lifecycle.is_port_in_use(sandbox.ws_port) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert lifecycle.is_port_in_use(sandbox.ws_port), "the WebSocket port is held"
    assert other_process_can_lock(sandbox.ws_port) is False, "and so is the instance lock"


def test_a_second_start_beside_a_healthy_server_exits_quietly(sandbox):
    """A launcher that spawns a server while another already serves must not disturb it:
    the loser sees the lock taken, finds the incumbent healthy, and exits 0."""
    sandbox.start()
    incumbent = sandbox.wait_healthy()

    late = sandbox.start()

    assert exit_code(late) == 0
    assert "already holds the instance lock" in sandbox.log_text()
    assert sandbox.health()["server_pid"] == incumbent["server_pid"], "the incumbent never moved"


def test_two_servers_started_together_leave_exactly_one(sandbox):
    """Racing launchers. Whichever process takes the lock serves; the other exits. Its
    exit code depends on whether the winner is answering /health yet when it looks (0 when
    it is, 1 when it is not), so both are allowed and neither may leave a second server."""
    first, second = sandbox.start(), sandbox.start()
    winner = sandbox.wait_healthy()

    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and all(p.poll() is None for p in (first, second)):
        time.sleep(0.1)
    exited = [p for p in (first, second) if p.poll() is not None]
    assert len(exited) == 1, ("exactly one of the two exits: "
                            + sandbox.diagnostics())
    assert exited[0].returncode in (0, 1)
    assert sandbox.health()["server_pid"] == winner["server_pid"]
    assert sum(p.poll() is None for p in (first, second)) == 1


def test_a_lock_held_by_a_non_server_is_refused_loudly(sandbox):
    """The lock is taken but nothing healthy answers: exit 1 and say why, rather than run
    a second instance beside whatever holds it."""
    assert lifecycle.acquire_instance_lock(sandbox.ws_port)
    try:
        proc = sandbox.start()
        assert exit_code(proc) == 1
    finally:
        lifecycle.release_instance_lock()
    assert "Could not acquire the BrowserPlug single-instance lock" in sandbox.log_text()
    assert sandbox.health() is None


@pytest.mark.slow
def test_a_replacement_waits_for_the_lock_then_takes_over(sandbox):
    """--wait-for-port is what /restart passes: wait for the outgoing server's lock (up to
    25 s) instead of refusing as a duplicate."""
    assert lifecycle.acquire_instance_lock(sandbox.ws_port)
    try:
        replacement = sandbox.start(wait_for_port=True)
        time.sleep(4.0)     # long enough to have imported everything and reached the wait
        assert replacement.poll() is None, "it waits instead of exiting"
        assert sandbox.health() is None, "and serves nothing while the lock is held"
    finally:
        lifecycle.release_instance_lock()

    health = sandbox.wait_healthy()
    assert health["service"] == "browserplug"
    assert replacement.poll() is None
    assert "is the replacement" in sandbox.log_text()


def test_a_busy_websocket_port_stops_a_normal_start(sandbox):
    """We hold the lock but a stranger holds the extension port. It cannot move, so the
    server refuses instead of starting deaf."""
    squatter = PortSquatter(sandbox.ws_port)
    try:
        proc = sandbox.start()
        assert exit_code(proc) == 1
    finally:
        squatter.release()
    assert f"port {sandbox.ws_port}" in sandbox.log_text()
    assert "still in use by another process" in sandbox.log_text()


def test_a_replacement_waits_for_the_ports_to_free_then_binds_them(sandbox):
    """The other half of the handover: the lock is free but the outgoing process still
    holds the ports (10 s of grace in main()). Freed inside the grace, it binds them."""
    squatter = PortSquatter(sandbox.ws_port)
    replacement = sandbox.start(wait_for_port=True)
    try:
        time.sleep(5.0)
        assert replacement.poll() is None, "still waiting on the port"
    finally:
        squatter.release()

    assert sandbox.wait_healthy()["ws_port"] == sandbox.ws_port
    assert replacement.poll() is None


@pytest.mark.slow
def test_a_replacement_gives_up_when_the_ports_never_free(sandbox):
    """Past the grace the replacement still refuses, so the outgoing server's death does
    not leave a half-started successor. Slow by construction (the grace is 10 s)."""
    squatter = PortSquatter(sandbox.ws_port)
    try:
        replacement = sandbox.start(wait_for_port=True)
        assert exit_code(replacement, timeout=60) == 1
    finally:
        squatter.release()
    assert "still in use by another process" in sandbox.log_text()


# --- /restart, /shutdown, /ws-bind on a real process ----------------------------------

@pytest.mark.slow
def test_restart_replaces_the_process_on_the_same_ports(sandbox):
    """POST /restart: the server answers, drains, spawns a replacement that waits for the
    ports, and exits; the replacement comes up on the SAME ports with a new pid. Both
    endpoints demand a signed request (review S2).

    /shutdown follows, on the replacement: the process ends and frees its ports and lock."""
    sandbox.start()
    before = sandbox.wait_healthy()

    status, body = sandbox.post("/restart", headers=sandbox.signed("POST", "/restart"))

    assert status == 200
    assert json.loads(body) == {"status": "restarting", "pid": before["server_pid"]}
    after = sandbox.wait_healthy(not_pid=before["server_pid"])
    assert after["server_pid"] != before["server_pid"]
    assert (after["mcp_port"], after["ws_port"]) == (sandbox.mcp_port, sandbox.ws_port)
    sandbox.wait_gone(before["server_pid"])
    assert "is the replacement" in sandbox.log_text()

    status, body = sandbox.post("/shutdown", headers=sandbox.signed("POST", "/shutdown"))

    assert status == 200 and json.loads(body)["pid"] == after["server_pid"]
    sandbox.wait_gone(after["server_pid"])
    assert sandbox.health() is None
    assert not lifecycle.is_port_in_use(sandbox.mcp_port)
    assert not lifecycle.is_port_in_use(sandbox.ws_port)
    assert other_process_can_lock(sandbox.ws_port) is True, "the lock went with the process"


def test_shutdown_ends_the_process_with_status_zero(sandbox):
    proc = sandbox.start()
    sandbox.wait_healthy()

    status, body = sandbox.post("/shutdown", headers=sandbox.signed("POST", "/shutdown"))

    assert status == 200
    assert exit_code(proc, timeout=20) == 0
    assert sandbox.health() is None


@pytest.mark.slow
def test_ws_bind_moves_a_real_server_to_the_new_bind_and_it_survives_the_restart(sandbox):
    """The /ws-bind success path across a real restart, without exposing anything.

    The in-process tests stub the restart (`_restart_onto` would end the runner with
    os._exit), so they prove the answer and the persisted file but not that the
    replacement honours what was written. This does, on a test-port server with a scratch
    state directory. The new bind is 127.0.0.1, a loopback spelling that differs from the
    default "localhost": the bind changes and is read back from the file by the
    replacement, and no socket ever listens beyond this machine. (0.0.0.0 would work the
    same way and is what the in-process tests use; it stays out of here on purpose.)"""
    sandbox.start()
    before = sandbox.wait_healthy()
    assert before["ws_host"] == "localhost"
    assert before["ws_host_source"] == "default"

    bind_body = json.dumps({"host": "127.0.0.1"}).encode()
    status, body = sandbox.post(
        "/ws-bind", body=bind_body,
        headers=sandbox.signed("POST", "/ws-bind", body=bind_body))

    assert status == 200, body
    reply = json.loads(body)
    assert reply["status"] == "restarting" and reply["ws_host"] == "127.0.0.1"
    assert reply["ws_accepts_remote"] is False
    after = sandbox.wait_healthy(not_pid=before["server_pid"])
    assert after["ws_host"] == "127.0.0.1"
    assert after["ws_host_source"] == "file", "the replacement read the bind back from disk"
    config = json.loads((sandbox.state / "config.json").read_text(encoding="utf-8"))
    assert config["ws"]["host"] == "127.0.0.1"


def test_ws_bind_without_the_token_changes_nothing_on_a_real_server(sandbox):
    sandbox.start()
    before = sandbox.wait_healthy()

    status, _ = sandbox.post("/ws-bind", body=json.dumps({"host": "127.0.0.1"}).encode())

    assert status == 403
    assert sandbox.health()["server_pid"] == before["server_pid"], "no restart happened"
    assert not (sandbox.state / "config.json").exists()
