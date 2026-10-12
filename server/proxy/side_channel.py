"""The proxy's two plain-HTTP conversations with the shared server: /health and /timing.

watch_tools polls /health and announces a tool-list change to the client when
(server_pid, tools_signature) moves: server_pid changes on a /restart (new code),
tools_signature when the same server re-shapes a tool in place (the extension's optional
"proxy" permission adds or drops the `proxy` argument). It pushes
notifications/tools/list_changed on the stdio session for a handshake-era client and
publishes ToolsListChanged on the front's subscription bus for a 2026-07-28 one. Nothing
the server sends reaches the client through the proxy (each backend session lives for one
request), so this poll is the only way a running session learns of either. The poll also carries the launcher's pid and build (policy.identity_headers): it
runs for the launcher's whole life, so it reports a version even for a client that never
calls a tool.

report_timing posts one tools/call's stdio-side duration, which only this side of the hop
can see (stdio request in, stdio response out); the server merges it into the call's
timings. Best effort: a lost report costs a diagnostics line, never the call.

Both use a fresh short-timeout httpx2 client per request with trust_env=False: an
HTTP_PROXY in the environment would send these loopback requests to a proxy that cannot
reach them, and the tool-surface watch would never see a change.
"""

import asyncio
import sys

import httpx2
from mcp.server.subscriptions import ToolsListChanged

from browserplug_shared.local_auth import signed_headers
from server.proxy import policy

# Read at every iteration, so a test can set it to 0.
TOOLS_POLL_SECONDS = policy.TOOLS_POLL_SECONDS


def log(*args):
    """One line on stderr, the launcher's log (stdout is the MCP channel)."""
    print("[browserplug-launcher]", *args, file=sys.stderr, flush=True)


async def report_timing(config: policy.ProxyConfig, tool, started_epoch, total_ms):
    """POST one tools/call's timing to /timing, signed with the local token. Swallows every
    failure, silently: a dead or restarting server must not cost the call path anything."""
    try:
        payload = policy.timing_payload(config, tool, started_epoch, total_ms)
        async with httpx2.AsyncClient(timeout=policy.SIDE_CHANNEL_TIMEOUT_SECONDS,
                                      trust_env=False) as h:
            await h.post(config.timing_url,
                         headers={**signed_headers("POST", "/timing", payload),
                                  "Content-Type": "application/json"},
                         content=payload)
    except Exception:
        pass


async def watch_tools(holder, config: policy.ProxyConfig, bus=None):
    """Announce each move of the tool surface to the client, in the era it speaks.

    A handshake-era client gets tools/list_changed pushed on holder["session"]. A
    2026-07-28 client gets ToolsListChanged published on `bus` (front.ProxyServer.bus),
    which reaches its open subscriptions/listen streams: at that era the SDK drops the
    bare push, and a handshake-era connection cannot open a listen stream, so neither
    client sees two copies. Each delivery fails on its own without stopping the other.

    The first /health read is the baseline, not a change. A failed read is skipped and
    keeps the baseline, so a server that comes back with the same surface stays quiet and
    one that comes back with a new pid is announced. The session is re-read from `holder`
    on every push (front.py swaps the live one in); a change seen before any session or
    listen stream exists is absorbed, not replayed. Runs until cancelled.
    """
    last_key = None
    while True:
        try:
            await asyncio.sleep(TOOLS_POLL_SECONDS)
            try:
                async with httpx2.AsyncClient(timeout=policy.SIDE_CHANNEL_TIMEOUT_SECONDS,
                                              trust_env=False) as h:
                    health = (await h.get(config.health_url,
                                          headers=policy.identity_headers(config))).json()
            except Exception:
                continue
            key = policy.surface_key(health)
            if key == last_key:
                continue
            if last_key is not None:
                session = holder.get("session")
                if session is not None and hasattr(session, "send_tool_list_changed"):
                    try:
                        await session.send_tool_list_changed()
                        log(f"tool surface changed (pid/sig {key}); sent tools/list_changed")
                    except Exception as e:
                        log("send_tool_list_changed failed:", e)
                if bus is not None:
                    try:
                        await bus.publish(ToolsListChanged())
                    except Exception as e:
                        log("publishing ToolsListChanged failed:", e)
            last_key = key
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log("watch_tools error:", e)
