"""Unit-test isolation for server state that lives on disk.

Merely constructing a BrowserPlugServer touches the user's real state directory: it MINTS
and PERSISTS a WebSocket token when none is configured (ws_token.py), and it READS the
settings in config.json (config_store.py). Unit tests must not read, write or depend on
either, so the whole state directory is redirected into tmp for the session.

BROWSERPLUG_STATE_DIR is the one knob that moves both files, which is exactly why it
exists. BROWSERPLUG_WS_TOKEN_FILE is set too, so a test that reasons about the token path
alone still has one to point at.

Session-scoped (not per-test) on purpose: the token's STABILITY across restarts is part of
the contract, and tests of it construct several servers. Tests that need a pristine file
point BROWSERPLUG_WS_TOKEN_FILE at their own tmp_path.
"""

import os

import pytest

from browserplug_shared.config_store import STATE_DIR_ENV
from browserplug_shared.mcp_era import ERA_ENV
from browserplug_shared.ws_token import WS_HOST_ENV, WS_TOKEN_ENV, WS_TOKEN_FILE_ENV


@pytest.fixture(scope="session", autouse=True)
def isolated_ws_token_file(tmp_path_factory):
    state_dir = tmp_path_factory.mktemp("ws-token-state")
    previous = {name: os.environ.get(name) for name in (STATE_DIR_ENV, WS_TOKEN_FILE_ENV)}
    os.environ[STATE_DIR_ENV] = str(state_dir)
    os.environ[WS_TOKEN_FILE_ENV] = str(state_dir / "ws-token")
    # A token exported in the developer's shell would otherwise turn STRICT auth on for
    # every server built in this suite.
    had_env_token = os.environ.pop(WS_TOKEN_ENV, None)
    # Same for the bind: BROWSERPLUG_WS_HOST outranks every other source, so a value in
    # the developer's shell would silently pin ws_host (and its `source`) for the suite.
    had_env_host = os.environ.pop(WS_HOST_ENV, None)
    # And the protocol era: BROWSERPLUG_MCP_ERA is the documented rollback knob, and exported
    # in the developer's shell it would move every server in the suite off the default era.
    # A test that needs an era sets it itself.
    had_env_era = os.environ.pop(ERA_ENV, None)
    yield os.environ[WS_TOKEN_FILE_ENV]
    for name, value in previous.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
    if had_env_token is not None:
        os.environ[WS_TOKEN_ENV] = had_env_token
    if had_env_host is not None:
        os.environ[WS_HOST_ENV] = had_env_host
    if had_env_era is not None:
        os.environ[ERA_ENV] = had_env_era


@pytest.fixture(autouse=True)
def no_background_scans(monkeypatch):
    """The Kilo and Copilot detectors ask handle_scan for another process's open files,
    which queues a child-process scan on a worker thread. A unit test that builds a server
    with such a client row must not start threads or processes for its made-up pids; with
    the worker off the queue just fills, and the lookup answers None (unknown)."""
    from server.usage_panels import handle_scan
    monkeypatch.setattr(handle_scan, "_ensure_worker", lambda: None)
    handle_scan._reset()
    yield
    handle_scan._reset()
