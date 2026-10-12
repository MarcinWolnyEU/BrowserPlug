"""The legacy single-browser view must read the ROUTED browser, not the first-connected one.

`BrowserPlugServer.default_browser` is the first browser to connect; routing sends an
unrouted client to `preferred_browser` (the pinned one). Every scalar that describes "the
browser" must come from the second, or /health and server_info describe a browser that
never takes the call. Also pins can_open_file_url (it read `.engine` off the socket, so it
was always false) and server_info's extension_file_access report.
"""


import pytest

import test_imports  # noqa: F401
from test_browser_registry import disconnect
from test_browser_routing import (add_browser, as_client, pinned_to, server, tools,  # noqa: F401
                                  call_browsers)


async def _two(server):
    a, a_task = await add_browser(server, "ff-a", 101, "Firefox")
    b, b_task = await add_browser(server, "ff-b", 102, "Chrome")
    ra, rb = server.registry.live_browsers()
    ra.engine, rb.engine = "gecko", "blink"
    ra.version, rb.version = "1.0.0", "2.0.0"
    ra.file_access = {"can_read_files": False, "browser_major": 152}
    rb.file_access = {"can_read_files": True, "browser_major": 140}
    return (a, a_task, ra), (b, b_task, rb)


@pytest.mark.asyncio
async def test_health_scalars_follow_the_pinned_browser_not_the_first(server):
    (a, at, ra), (b, bt, rb) = await _two(server)
    with pinned_to("ff-b"):
        h = server.get_health_info()
        assert h["extension_version"] == "2.0.0", h["extension_version"]
        assert h["browser_major"] == 140
        assert h["extension_file_access"]["can_read_opened_file_tab"] is True
    await disconnect(a, at)
    await disconnect(b, bt)


@pytest.mark.asyncio
async def test_can_open_file_url_true_on_blink_with_the_toggle_false_on_gecko(server):
    (a, at, ra), (b, bt, rb) = await _two(server)
    with pinned_to("ff-b"):
        assert server.get_health_info()["extension_file_access"]["can_open_file_url"] is True
    with pinned_to("ff-a"):
        fa = server.get_health_info()["extension_file_access"]
        assert fa["can_open_file_url"] is False
    rb.file_access = {"can_read_files": False, "browser_major": 140}
    with pinned_to("ff-b"):
        assert server.get_health_info()["extension_file_access"]["can_open_file_url"] is False
    await disconnect(a, at)
    await disconnect(b, bt)


@pytest.mark.asyncio
async def test_server_info_reports_file_access_and_version_of_the_routed_browser(tools, server):
    (a, at, ra), (b, bt, rb) = await _two(server)
    fn = (await tools.mcp.get_tool("server_info")).fn
    with pinned_to("ff-b"), as_client(777):
        out = await fn()
        assert "(version: 2.0.0)" in out, out
        assert "can_open_file_url: true" in out, out
        assert "can_read_opened_file_tab: true" in out, out
    with as_client(778):
        await call_browsers(tools, action="use", target=1)
        out = await fn()
        assert "(version: 1.0.0)" in out, out
        assert "can_open_file_url: false" in out, out
    await disconnect(a, at)
    await disconnect(b, bt)


@pytest.mark.asyncio
async def test_version_skew_hint_names_the_routed_browsers_version(tools, server):
    (a, at, ra), (b, bt, rb) = await _two(server)
    with pinned_to("ff-b"), as_client(777):
        err = tools.calls.error_of({"error": "Unknown tabs action: frob"})
    assert "(v2.0.0)" in err, err
    await disconnect(a, at)
    await disconnect(b, bt)


def test_no_hand_built_pid_client_key_outside_routing():
    import pathlib, re
    root = pathlib.Path(__file__).resolve().parents[2] / "server"
    offenders = []
    for p in root.rglob("*.py"):
        if p.name == "routing.py":
            continue
        for n, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if re.search(r"""f?["']pid:\{""", line):
                offenders.append(f"{p.name}:{n}: {line.strip()}")
    assert not offenders, offenders
