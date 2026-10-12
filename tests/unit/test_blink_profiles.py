"""Two profiles of one Chromium install must stay two browsers (review finding C2).

Every profile of one Chrome install shares the main chrome.exe, so the pre-hello pid
match (top-most same-exe ancestor) is the same pid for all of them. For a blink engine
only the hello's instance_id may decide identity; a gecko process is one profile, so its
pid match stays. The process walk is faked: the sanctioned mock.
"""

import asyncio
from unittest.mock import MagicMock

import pytest

from test_browser_registry import (FakeWS, connect, disconnect, hello, server,  # noqa: F401
                                   wait_until)
from server.browsers.browser_registry import BrowserRegistry

CHROME = {"pid": 7000, "friendly": "Chrome", "engine": "blink", "brand": "Chrome"}
FIREFOX = {"pid": 4101, "friendly": "Firefox", "engine": "gecko", "brand": "Firefox"}


@pytest.mark.asyncio
async def test_two_blink_profiles_sharing_an_ancestor_pid_are_two_browsers(server):  # noqa: F811
    first = FakeWS(peer=("127.0.0.1", 1111))
    t1 = await connect(server, first, identity=CHROME)
    await hello(server, first, instanceId="profile-1", version="1.0.0")

    second = FakeWS(peer=("127.0.0.1", 2222))
    t2 = await connect(server, second, identity=CHROME)
    await hello(server, second, instanceId="profile-2", version="1.0.0")

    assert sorted(server.browsers) == ["profile-1", "profile-2"]
    assert server.browsers["profile-1"].ws is first
    assert server.browsers["profile-2"].ws is second
    assert first.close_code is None, "profile 1 must not be closed by profile 2 connecting"
    assert second.close_code is None

    await disconnect(second, t2)
    await disconnect(first, t1)


@pytest.mark.asyncio
async def test_pre_hello_blink_socket_gets_its_own_provisional_record(server):  # noqa: F811
    first = FakeWS(peer=("127.0.0.1", 1111))
    t1 = await connect(server, first, identity=CHROME)
    await hello(server, first, instanceId="profile-1", version="1.0.0")
    second = FakeWS(peer=("127.0.0.1", 2222))
    t2 = await connect(server, second, identity=CHROME)   # no hello yet

    assert server.browsers["profile-1"].ws is first
    assert first.close_code is None
    assert len(server.browsers) == 2

    await disconnect(second, t2)
    await disconnect(first, t1)


@pytest.mark.asyncio
async def test_gecko_pid_match_is_kept(server):  # noqa: F811
    old = FakeWS(peer=("127.0.0.1", 1111))
    t1 = await connect(server, old, identity=FIREFOX)
    await hello(server, old, instanceId="ff-a", version="1.0.0")
    new = FakeWS(peer=("127.0.0.1", 2222))
    t2 = await connect(server, new, identity=FIREFOX)

    assert list(server.browsers) == ["ff-a"]
    assert server.browsers["ff-a"].ws is new
    assert old.close_code is not None

    await disconnect(new, t2)
    await asyncio.wait_for(t1, timeout=2.0)


def test_displace_never_closes_a_socket_owned_by_a_different_instance_id():
    closed = []
    reg = BrowserRegistry(
        make_browser=MagicMock(), probe_identity=lambda ws: {},
        fail_pending=MagicMock(), reassign_pending_owner=MagicMock(),
        drop_gate=MagicMock(), close_socket=closed.append,
        refresh_browsers_tool=MagicMock(), refresh_proxy_tool_visibility=MagicMock(),
        hello_window=lambda: 1.0)
    old_ws, new_ws = MagicMock(close_code=None), MagicMock(close_code=None)
    browser = MagicMock(instance_id="profile-1", ws=old_ws, provisional=False)

    reg.displace(browser, new_ws, claimed_instance_id="profile-2")

    assert closed == []
    assert browser.ws is old_ws
    reg._fail_pending.assert_not_called()
