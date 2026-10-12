"""The proxy transport's rules, as data and pure functions (stdlib only).

The proxy (server/proxy/front.py) is the escape hatch from the stdlib relay. It runs as a
child of the launcher with the server environment's python, so the launcher stays light
and the proxy may use the MCP SDK. Everything here is plain values that tests can pin
without an event loop: the command line the launcher builds, the timeouts, the tool-name
cache, the handshake gate's refusal text, the /health identity headers and the /timing
payload.

The command line is a contract between two processes that may come from different builds:
the launcher constructs it from its own constants (launcher/launcher.py may not import
server/), and `parse_args` reads it. tests/unit/test_proxy_policy.py builds the launcher's
argv and parses it here.
"""

import argparse
import json
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Optional
from urllib.parse import urlsplit

from browserplug_shared.version import (HEALTH_IDENTITY_HEADER, HEALTH_IDENTITY_VALUE,
                                        LAUNCHER_PID_HEADER, LAUNCHER_VERSION_HEADER)

# How long the tool names from the last tools/list resolve a tools/call before the proxy
# lists again. A call to a name outside the cache is answered here, never sent: that is
# where the renamed-tool hint comes from on this path. 120 s (the old proxy's library
# default was 300) so a re-shaped tool surface, e.g. the extension's optional "proxy"
# permission toggled, reaches a running session within two minutes even if the client
# ignores tools/list_changed; a new session, and any tools/list, refresh it at once.
TOOLS_CACHE_TTL_SECONDS = 120.0

# The /health poll that turns a change of (server_pid, tools_signature) into
# notifications/tools/list_changed; the relay polls at the same cadence.
TOOLS_POLL_SECONDS = 3

# The handshake gate's bound on the backend probe: under a client's ~30 s connect budget,
# far above a healthy loopback initialize.
PROBE_TIMEOUT_SECONDS = 10

# The backend's httpx2 timeouts: mcp's create_mcp_http_client defaults (30 s, 300 s for an
# SSE read), copied by value because the module that holds them is private to the SDK.
BACKEND_TIMEOUT_SECONDS = 30.0
BACKEND_READ_TIMEOUT_SECONDS = 300.0

# When the client's stdio session ends with calls in flight, how long their backend
# exchanges get to send the cancel and the DELETE before the proxy exits anyway. Loopback
# needs milliseconds; an MCP client waits 2 s after closing stdin before it kills us.
BACKEND_DRAIN_SECONDS = 1.0

# The /health poll and the /timing report: short, so a slow server costs a skipped poll.
SIDE_CHANNEL_TIMEOUT_SECONDS = 2.0

# JSON-RPC internal error, the code an unreachable backend fails the handshake with.
INTERNAL_ERROR = -32603


@dataclass(frozen=True)
class ProxyConfig:
    """What the launcher hands the proxy: where the backend is and who is asking.

    `client_pid` is the MCP client that spawned the launcher (launcher/client_pid.py),
    sent on every backend request; `launcher_pid` and `launcher_version` identify the
    launcher process on the /health poll and the /timing report, so the server's map
    shows the launcher node the client spawned and the build it runs.
    """

    backend_url: str
    client_pid: int
    launcher_pid: int
    launcher_version: str
    timeout: float = BACKEND_TIMEOUT_SECONDS
    read_timeout: float = BACKEND_READ_TIMEOUT_SECONDS

    @property
    def origin(self) -> str:
        parts = urlsplit(self.backend_url)
        return f"{parts.scheme}://{parts.netloc}"

    @property
    def health_url(self) -> str:
        return f"{self.origin}/health"

    @property
    def timing_url(self) -> str:
        return f"{self.origin}/timing"


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="server/proxy/__main__.py",
        description="BrowserPlug's proxy transport: MCP on stdio, forwarded to the shared "
                    "server. The launcher starts it; there is no reason to run it by hand.")
    ap.add_argument("--backend-url", required=True,
                    help="the shared server's MCP endpoint, http://127.0.0.1:<port>/mcp")
    ap.add_argument("--client-pid", type=int, required=True)
    ap.add_argument("--launcher-pid", type=int, required=True)
    ap.add_argument("--launcher-version", required=True)
    ap.add_argument("--timeout", type=float, default=BACKEND_TIMEOUT_SECONDS)
    ap.add_argument("--read-timeout", type=float, default=BACKEND_READ_TIMEOUT_SECONDS)
    return ap


def parse_args(argv: Iterable[str]) -> ProxyConfig:
    """The launcher's command line as a ProxyConfig. argparse exits 2 on a bad one."""
    ns = _parser().parse_args(list(argv))
    return ProxyConfig(backend_url=ns.backend_url, client_pid=ns.client_pid,
                       launcher_pid=ns.launcher_pid, launcher_version=ns.launcher_version,
                       timeout=ns.timeout, read_timeout=ns.read_timeout)


def gate_refusal_text(backend_url: str, detail: str) -> str:
    """Why the client's handshake failed: nothing answered at the backend URL.

    A failed connect is what an MCP client shows its user; a session that opens empty
    looks connected and stays empty. The launcher's stderr names the startup log and
    preflight.py, so the message points there."""
    return (f"BrowserPlug launcher: no MCP server answered at {backend_url} ({detail}). "
            f"The launcher's stderr log names the startup log and preflight.py.")


def connect_failure_text(cause: BaseException) -> str:
    """How a backend leg that failed below MCP reads to the client, however the failure
    arrived (refused connect, reset stream, a read timeout)."""
    return f"Client failed to connect: {cause}"


class ToolNameCache:
    """The tool names of the last successful tools/list, and when it happened.

    Fresh for TOOLS_CACHE_TTL_SECONDS; a stale or empty cache makes the next tools/call
    list the backend first. A failed tools/list leaves the cache (and its age) as it was.
    """

    def __init__(self, ttl: float = TOOLS_CACHE_TTL_SECONDS,
                 clock: Callable[[], float] = time.monotonic):
        self.ttl = ttl
        self._clock = clock
        self._names: Optional[frozenset] = None
        self._stamp = 0.0

    def store(self, names: Iterable[str]) -> None:
        self._names = frozenset(names)
        self._stamp = self._clock()

    def is_fresh(self) -> bool:
        return self._names is not None and (self._clock() - self._stamp) < self.ttl

    def knows(self, name: str) -> Optional[bool]:
        """True or False from the stored list; None before any list has been stored."""
        if self._names is None:
            return None
        return name in self._names


def surface_key(health: dict) -> tuple:
    """What a tools/list_changed is pushed on: server_pid moves on a restart (new code),
    tools_signature when the same server re-shapes a tool in place."""
    return (health.get("server_pid"), health.get("tools_signature"))


def identity_headers(config: ProxyConfig) -> dict:
    """The /health poll's headers: which launcher this is and which build it runs, plus
    the request for the identity-only reply (server_pid and tools_signature are all the
    poll reads; the full report costs the server process scans)."""
    return {LAUNCHER_PID_HEADER: str(config.launcher_pid),
            LAUNCHER_VERSION_HEADER: config.launcher_version,
            HEALTH_IDENTITY_HEADER: HEALTH_IDENTITY_VALUE}


def timing_payload(config: ProxyConfig, tool: Optional[str], started_epoch: float,
                   total_ms: float) -> bytes:
    """The POST /timing body for one tools/call, serialized here because the request
    signature covers these exact bytes. server CallTelemetry.record_launcher_timing
    matches on tool, started_epoch, total_ms and client_pid."""
    return json.dumps({
        "source": "launcher",
        "launcher_pid": config.launcher_pid,
        "client_pid": config.client_pid,
        "tool": tool,
        "started_epoch": started_epoch,
        "total_ms": round(total_ms, 1),
    }).encode()
