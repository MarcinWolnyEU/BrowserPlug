"""The launcher on a BARE interpreter: the registration `<uv's python> server/launcher.py`.

An MCP client registers the launcher with uv's own Python, which has no site-packages, so no
psutil. Everything else in the unit suite runs the launcher on the runner's environment,
where psutil is installed, and would stay green if the launcher quietly needed it again. This
file starts the real `server/launcher.py` stub on the base interpreter with `-I -S` (no
PYTHON* variables, no cwd on sys.path, no site-packages), against a stub /health, and checks
the three things that registration must keep doing without psutil:

* adopt a server that is this checkout's (a real same-user process whose command line names
  this checkout's server.py), with no warning and no spawn, and relay a frame;
* send the spawning client's pid (the test process itself: nothing sits between it and the
  launcher on a bare interpreter) on every request, so the server can name the client;
* refuse a /health that a same-user stranger answers, instead of trusting it as it did when
  it had no psutil.

Nothing here can reach the live server: BROWSERPLUG_MCP_PORT pins the stub's port (a pin
disables the seek), the state directory is a scratch one, and BROWSERPLUG_SERVER_PYTHON names
a file that does not exist, so a launcher that failed to adopt cannot start a server either.
"""

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from browserplug_shared.version import __version__ as BROWSERPLUG_VERSION

ROOT = Path(__file__).resolve().parents[2]
STUB_LAUNCHER = ROOT / "server" / "launcher.py"
CLIENT_PID_HEADER = "X-BrowserPlug-Client-Pid"
LAUNCHER_PID_HEADER = "X-BrowserPlug-Launcher-Pid"
INITIALIZE = {"jsonrpc": "2.0", "id": 7, "method": "initialize",
              "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                         "clientInfo": {"name": "bare-interpreter-test", "version": "0"}}}
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
HOST = Path(__file__).resolve().parent / "health_stub_host.py"


@pytest.fixture(scope="module")
def bare_python():
    """uv's (or the system's) real interpreter, never a venv's redirector, and proof that no
    third-party package can be imported on it. A box without one fails loudly: the test
    would prove nothing on the runner's own environment."""
    base = getattr(sys, "_base_executable", None) or sys.executable
    assert os.path.isfile(base), f"no base interpreter to run the launcher on: {base!r}"
    flags = [base, "-I", "-S"]
    out = subprocess.run([*flags, "-c", "import sys; print(sys.prefix == sys.base_prefix)"],
                         capture_output=True, text=True, timeout=60)
    assert out.stdout.strip() == "True", (
        f"{base} is a virtual environment's interpreter, not a bare one: {out.stderr}")
    out = subprocess.run([*flags, "-c", "import psutil"], capture_output=True, text=True,
                         timeout=60)
    assert out.returncode != 0, f"psutil imports on {base} even with -I -S; nothing is bare here"
    return flags


class StubServer:
    """A BrowserPlug-shaped /health in a process of its own (health_stub_host.py), so the
    socket the launcher connects to belongs to a process the launcher can inspect.

    `names_server`: the process's command line names this checkout's server/server.py, which
    is what the launcher accepts as the shared server. `claimed_pid`: the server_pid /health
    quotes (default: the host's own). The host records every request and posted frame to a
    file, read back through `requests` and `posts`."""

    def __init__(self, base_python, tmp_path, *, names_server=True, claimed_pid=None):
        self.record = tmp_path / f"stub-record-{id(self)}.jsonl"
        self.record.write_text("", encoding="utf-8")
        argv = [*base_python, str(HOST), str(self.record),
                "self" if claimed_pid is None else str(claimed_pid)]
        if names_server:
            argv.append(str(ROOT / "server" / "server.py"))
        env = {**os.environ, "HEALTH_STUB_VERSION": BROWSERPLUG_VERSION}
        self.process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                        env=env, creationflags=CREATE_NO_WINDOW)
        self.port = int(self.process.stdout.readline())

    def _entries(self):
        return [json.loads(line) for line in
                self.record.read_text(encoding="utf-8").splitlines() if line]

    @property
    def requests(self):
        """(method, path, client pid header, launcher pid header) of every request."""
        return [(e["method"], e["path"], e["client"], e["launcher"])
                for e in self._entries() if e["kind"] == "request"]

    @property
    def posts(self):
        return [e["frame"] for e in self._entries() if e["kind"] == "post"]

    def close(self):
        self.process.kill()
        self.process.wait()
        self.process.stdout.close()


@pytest.fixture
def stub_factory(bare_python, tmp_path):
    made = []

    def make(**kw):
        made.append(StubServer(bare_python, tmp_path, **kw))
        return made[-1]
    yield make
    for stub in made:
        stub.close()


@pytest.fixture
def server_py_process():
    """A same-user process whose command line names THIS checkout's server/server.py: what
    the launcher accepts as the shared server. It only sleeps."""
    child = subprocess.Popen([getattr(sys, "_base_executable", sys.executable), "-c",
                              "import time; time.sleep(300)",
                              str(ROOT / "server" / "server.py")],
                             stdin=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)
    yield child
    child.kill()
    child.wait()


def _launcher_env(tmp_path, port):
    env = {name: value for name, value in os.environ.items()
           if not name.upper().startswith(("PYTHON", "UV_", "BROWSERPLUG_"))
           and name.upper() != "VIRTUAL_ENV"}
    env.update({
        "BROWSERPLUG_MCP_PORT": str(port),                      # a pin: no seek, ever
        "BROWSERPLUG_STATE_DIR": str(tmp_path / "state"),
        "BROWSERPLUG_LOG_FILE": str(tmp_path / "state" / "launcher-test-server.log"),
        # Must not exist: a launcher that fails to adopt then fails its spawn at once.
        "BROWSERPLUG_SERVER_PYTHON": str(tmp_path / "no-such-python"),
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    return env


def _start(bare_python, tmp_path, port):
    return subprocess.Popen([*bare_python, str(STUB_LAUNCHER)], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env=_launcher_env(tmp_path, port), cwd=str(tmp_path),
                            creationflags=CREATE_NO_WINDOW)


def _read_line(stream, seconds=60):
    """One line from `stream`, or None when it does not come in time."""
    line = []
    reader = threading.Thread(target=lambda: line.append(stream.readline()), daemon=True)
    reader.start()
    reader.join(seconds)
    return line[0] if line else None


def _finish(proc, seconds=60):
    """Close stdin as a client does, wait for the exit and return (exit code, stderr text)."""
    proc.stdin.close()
    try:
        code = proc.wait(seconds)
    except subprocess.TimeoutExpired:
        proc.kill()
        code = proc.wait()
        pytest.fail("the launcher did not exit after its stdin closed")
    finally:
        text = proc.stderr.read().decode("utf-8", "replace")
        proc.stdout.close()
        proc.stderr.close()
    return code, text


def test_it_adopts_this_checkouts_server_and_relays_a_frame_without_psutil(
        bare_python, tmp_path, stub_factory):
    stub = stub_factory()
    proc = _start(bare_python, tmp_path, stub.port)
    try:
        proc.stdin.write(json.dumps(INITIALIZE).encode() + b"\n")
        proc.stdin.flush()
        reply = _read_line(proc.stdout)
    finally:
        code, stderr = _finish(proc)
    assert reply is not None, f"the launcher relayed nothing. stderr:\n{stderr}"
    answer = json.loads(reply)
    assert answer["id"] == INITIALIZE["id"] and answer["result"]["serverInfo"]["name"] == "stub"
    assert code == 0, stderr
    assert f"shared server healthy on :{stub.port}, relaying stdio" in stderr
    for word in ("ignoring", "WARNING", "psutil", "spawning", "ERROR", "Traceback"):
        assert word not in stderr, f"{word!r} in the launcher's log:\n{stderr}"
    assert INITIALIZE in stub.posts


def test_every_request_carries_the_pid_of_the_process_that_started_the_launcher(
        bare_python, tmp_path, stub_factory):
    """Nothing sits between this process and a launcher run on a bare interpreter (no venv
    redirector, no uv), so the client is this process, which is what the server's session row
    and its /timing match are keyed on."""
    stub = stub_factory()
    proc = _start(bare_python, tmp_path, stub.port)
    try:
        proc.stdin.write(json.dumps(INITIALIZE).encode() + b"\n")
        proc.stdin.flush()
        assert _read_line(proc.stdout) is not None
    finally:
        _finish(proc)
    posts = [pid for method, _path, pid, _launcher in stub.requests if method == "POST"]
    assert posts and set(posts) == {str(os.getpid())}
    # Whichever other request names a client names the same one (the relay's /health poll
    # runs every few seconds, so a short run may carry none).
    named = {pid for _method, _path, pid, _launcher in stub.requests if pid}
    assert named == {str(os.getpid())}
    launchers = {launcher for _m, _p, _c, launcher in stub.requests if launcher}
    assert str(os.getpid()) not in launchers


def test_it_refuses_a_health_that_a_same_user_stranger_answers(
        bare_python, tmp_path, stub_factory):
    """/health names this test process: same user, a BrowserPlug-shaped reply, and a command
    line that names no server.py. Without psutil the launcher used to trust that."""
    stub = stub_factory(names_server=False, claimed_pid=os.getpid())
    proc = _start(bare_python, tmp_path, stub.port)
    time.sleep(0.5)             # no frame is sent: a launcher that gave up on the port
    code, stderr = _finish(proc)  # would relay one into the squatter
    assert f"ignoring a BrowserPlug-looking /health from pid {os.getpid()}" in stderr, stderr
    assert "shared server healthy" not in stderr
    assert "spawn failed" in stderr and "BROWSERPLUG_SERVER_PYTHON" in stderr
    assert code == 0, stderr
    assert stub.posts == []


def test_it_refuses_a_listener_that_quotes_the_real_servers_pid(
        bare_python, tmp_path, stub_factory, server_py_process):
    """The pid /health names is only a claim. A stranger that binds the port and quotes the
    pid of a real server process of this checkout passes every check on that pid (same user,
    command line naming server.py); only the socket's owner gives it away."""
    stub = stub_factory(names_server=False, claimed_pid=server_py_process.pid)
    proc = _start(bare_python, tmp_path, stub.port)
    time.sleep(0.5)
    code, stderr = _finish(proc)
    assert f"ignoring a BrowserPlug-looking /health from pid {server_py_process.pid}" in stderr, \
        stderr
    assert "shared server healthy" not in stderr
    assert stub.posts == []
