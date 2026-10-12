"""A stand-in BrowserPlug server in a process of its own, for test_launcher_base_interpreter.py.

    python health_stub_host.py <record file> <claimed server_pid or "self"> [<argv marker> ...]

It listens on a free 127.0.0.1 port, prints that port as its first stdout line, and answers
GET /health as a BrowserPlug server whose `server_pid` is the claimed pid ("self": this
process). POST /mcp takes a frame (an initialize gets a result and a session id). Every request
and every posted frame is appended to the record file as one JSON line, so the test process
can read what the launcher sent. The extra arguments only sit on the command line: the
launcher decides what a process is by the .py paths in its argv, and a test names
server/server.py there to make this process look like the checkout's server, or leaves it out
to make it a stranger.

Standard library only: it runs on the bare interpreter the tests use.
"""

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CLIENT_PID_HEADER = "X-BrowserPlug-Client-Pid"
LAUNCHER_PID_HEADER = "X-BrowserPlug-Launcher-Pid"

record_path = sys.argv[1]
claimed = os.getpid() if sys.argv[2] == "self" else int(sys.argv[2])
lock = threading.Lock()


def record(entry):
    with lock, open(record_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


class Handler(BaseHTTPRequestHandler):
    def _reply(self, status, body=b"", headers=None):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _note(self):
        record({"kind": "request", "method": self.command, "path": self.path,
                "client": self.headers.get(CLIENT_PID_HEADER),
                "launcher": self.headers.get(LAUNCHER_PID_HEADER)})

    def do_GET(self):
        self._note()
        if self.path.startswith("/health"):
            self._reply(200, json.dumps({
                "service": "browserplug", "status": "ok", "server_pid": claimed,
                "server_version": os.environ.get("HEALTH_STUB_VERSION", "0"),
                "tools_signature": "stub"}).encode())
        else:
            self._reply(404)

    def do_POST(self):
        self._note()
        frame = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        record({"kind": "post", "frame": frame})
        if frame.get("id") is None:
            self._reply(202)
            return
        result = {"protocolVersion": "2025-11-25", "capabilities": {},
                  "serverInfo": {"name": "stub", "version": "0"}}
        self._reply(200, json.dumps({"jsonrpc": "2.0", "id": frame["id"],
                                     "result": result}).encode(),
                    {"Mcp-Session-Id": "stub-session"})

    def do_DELETE(self):
        self._note()
        self._reply(200)

    def log_message(self, *args):
        pass


httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
print(httpd.server_address[1], flush=True)
httpd.serve_forever()
