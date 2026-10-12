"""L2 with real processes: kill the shared server under a live launcher, see it recover.

The launcher is the real `server/launcher.py` (through a two-line wrapper), the server is a
real `server/server.py` on the sandbox's ports (test_server_process_lifecycle.Sandbox fences
it off from the live server: scratch state dir, scratch token, ports outside 2830..2942 and
8772). The one substitution is the launcher's spawn target. The launcher spawns
`server.py --mcp-port N --mcp-port-pinned` and leaves the WebSocket port at its default,
8772, which belongs to the user's live server; the stub script forwards the same arguments
to the real server.py and adds the sandbox's WebSocket port. Everything the recovery
depends on (the seek, the spawn, the wait, the session rebuild) runs for real.
"""

import json
import queue
import subprocess
import sys
import threading
import time

import psutil

import test_imports  # noqa: F401
from browserplug_shared.mcp_port import MCP_PORT_ENV

from test_server_process_lifecycle import ROOT, SERVER_SCRIPT, Sandbox, sandbox  # noqa: F401

STUB = """\
import subprocess, sys
sys.exit(subprocess.call([sys.executable, {server!r}, *sys.argv[1:], "--port", {ws!r}]))
"""

WRAPPER = """\
import sys
sys.path.insert(0, {root!r})
import launcher.launcher as launcher
launcher.SERVER = {stub!r}
sys.exit(launcher.main())
"""


class LauncherProcess:
    def __init__(self, box, tmp_path):
        stub = tmp_path / "spawn_stub.py"
        stub.write_text(STUB.format(server=str(SERVER_SCRIPT), ws=str(box.ws_port)))
        wrapper = tmp_path / "launcher_wrapper.py"
        wrapper.write_text(WRAPPER.format(root=str(ROOT), stub=str(stub)))
        env = box.env()
        env[MCP_PORT_ENV] = str(box.mcp_port)
        self.proc = subprocess.Popen(
            [sys.executable, str(wrapper)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=env, cwd=str(ROOT))
        self.frames, self.stderr = queue.Queue(), []
        threading.Thread(target=self._pump, args=(self.proc.stdout, self.frames.put),
                         daemon=True).start()
        threading.Thread(target=self._pump, args=(self.proc.stderr, self.stderr.append),
                         daemon=True).start()

    @staticmethod
    def _pump(stream, sink):
        for line in stream:
            sink(line.decode("utf-8", "replace").rstrip())

    def send(self, frame):
        self.proc.stdin.write(json.dumps(frame).encode() + b"\n")
        self.proc.stdin.flush()

    def reply(self, msg_id, timeout=90.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                frame = json.loads(self.frames.get(timeout=1.0))
            except queue.Empty:
                continue
            if frame.get("id") == msg_id:
                return frame
        raise AssertionError(f"no reply to {msg_id} in {timeout:.0f}s; launcher stderr:\n"
                             + "\n".join(self.stderr[-15:]))

    def handshake(self):
        self.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "recovery-test", "version": "0"}}})
        assert "error" not in self.reply(1), self.stderr
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def stop(self):
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.proc.stdout.close()
        self.proc.stderr.close()


def kill_server(box):
    """End the server the way a crash does: no goodbye, the whole process pair."""
    pid = box.health()["server_pid"]
    victims = []
    for port in (box.ws_port, box.mcp_port):
        owner = box.__class__ and __import__("server.runtime.lifecycle", fromlist=["x"]) \
            .pid_listening_on_port(port)
        if owner:
            box.seen_pids.add(owner)
    box.seen_pids.add(pid)
    for p in list(box.seen_pids):
        try:
            proc = psutil.Process(p)
            victims += proc.children(recursive=True) + [proc]
        except psutil.Error:
            pass
    for proc in victims:
        try:
            proc.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(victims, timeout=10)
    deadline = time.monotonic() + 10
    while box.health(timeout=0.5) is not None:
        assert time.monotonic() < deadline, "the killed server still answers"
        time.sleep(0.1)
    box.seen_pids.clear()
    return pid


def test_the_relay_finds_or_respawns_the_server_after_it_dies(sandbox, tmp_path):
    sandbox.start()
    first_pid = sandbox.wait_healthy()["server_pid"]
    launcher = LauncherProcess(sandbox, tmp_path)
    try:
        launcher.handshake()
        launcher.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert "error" not in launcher.reply(2)

        assert kill_server(sandbox) == first_pid

        # An idempotent request made while the server is gone: the launcher spawns one
        # (through the stub), rebuilds its session on it, and answers this same request.
        launcher.send({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
        answer = launcher.reply(3)
        assert "error" not in answer and answer["result"]["tools"], (
            answer, launcher.stderr[-10:], sandbox.diagnostics())
        second_pid = sandbox.wait_healthy()["server_pid"]
        assert second_pid != first_pid

        # A call that may have run is NOT replayed: it gets an error that says so, and the
        # next request finds the server the recovery already restored.
        kill_server(sandbox)
        launcher.send({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                       "params": {"name": "page_act", "arguments": {}}})
        refusal = launcher.reply(4)
        assert "error" in refusal, refusal
        assert "may have" in refusal["error"]["message"] or \
            "did not run" in refusal["error"]["message"], refusal
        launcher.send({"jsonrpc": "2.0", "id": 5, "method": "ping"})
        assert "error" not in launcher.reply(5)
    finally:
        launcher.stop()
