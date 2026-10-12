"""server/proxy/policy.py: the proxy transport's plain rules, and the launcher's half of the
command-line contract.

The launcher (stdlib only, never imports server/) builds the proxy child's argv, and the
proxy parses it here. The two can come from different builds of the checkout, so the test
that matters most is the round trip: what launcher.proxy_command builds, parse_args reads.
"""

import json
import os
import sys

import pytest

from browserplug_shared.version import (HEALTH_IDENTITY_HEADER, HEALTH_IDENTITY_VALUE,
                                        LAUNCHER_PID_HEADER, LAUNCHER_VERSION_HEADER,
                                        __version__ as BROWSERPLUG_VERSION)
from launcher import launcher
from launcher.client_pid import client_pid
from server.proxy import policy


def _config(**over):
    values = dict(backend_url="http://127.0.0.1:2830/mcp", client_pid=4242, launcher_pid=5151,
                  launcher_version="1.2.3")
    values.update(over)
    return policy.ProxyConfig(**values)


# ---- the command line ------------------------------------------------------------------

def test_the_launchers_command_parses_into_the_config_the_proxy_runs_with():
    command = launcher.proxy_command(sys.executable, 2833)
    assert command[:2] == [sys.executable, launcher.PROXY_ENTRY]
    config = policy.parse_args(command[2:])
    assert config.backend_url == launcher.backend_url(2833) == "http://127.0.0.1:2833/mcp"
    assert config.client_pid == client_pid(), "the client that spawned the launcher"
    assert config.launcher_pid == os.getpid(), "the launcher itself, not the proxy child"
    assert config.launcher_version == BROWSERPLUG_VERSION
    assert (config.timeout, config.read_timeout) == (30.0, 300.0)


def test_the_proxy_entry_is_the_package_main_by_absolute_path():
    assert os.path.isabs(launcher.PROXY_ENTRY)
    assert os.path.isfile(launcher.PROXY_ENTRY)
    assert launcher.PROXY_ENTRY.replace("\\", "/").endswith("server/proxy/__main__.py")


def test_a_command_missing_a_required_value_is_refused():
    with pytest.raises(SystemExit) as exit_:
        policy.parse_args(["--backend-url", "http://127.0.0.1:1/mcp"])
    assert exit_.value.code == 2


def test_the_derived_urls_share_the_backends_origin():
    config = _config(backend_url="http://127.0.0.1:2915/mcp")
    assert config.origin == "http://127.0.0.1:2915"
    assert config.health_url == "http://127.0.0.1:2915/health"
    assert config.timing_url == "http://127.0.0.1:2915/timing"


# ---- the numbers -----------------------------------------------------------------------

def test_the_tool_cache_ttl_is_shorter_than_the_old_default():
    """A re-shaped tool surface reaches a running session within ~2 min even if its client
    ignores tools/list_changed (the old proxy library's default was 300 s)."""
    assert policy.TOOLS_CACHE_TTL_SECONDS == 120
    assert policy.TOOLS_CACHE_TTL_SECONDS < 300


def test_the_timeouts_are_mcps_defaults_and_the_probe_fits_a_connect_budget():
    assert (policy.BACKEND_TIMEOUT_SECONDS, policy.BACKEND_READ_TIMEOUT_SECONDS) == (30.0, 300.0)
    assert policy.PROBE_TIMEOUT_SECONDS == 10, "under a client's ~30 s connect budget"
    assert policy.TOOLS_POLL_SECONDS == 3
    assert policy.SIDE_CHANNEL_TIMEOUT_SECONDS == 2.0
    assert policy.INTERNAL_ERROR == -32603


# ---- the tool-name cache ---------------------------------------------------------------

class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_an_empty_cache_is_stale_and_knows_nothing():
    cache = policy.ToolNameCache(clock=_Clock())
    assert cache.is_fresh() is False
    assert cache.knows("tabs") is None


def test_a_stored_list_answers_until_the_ttl_runs_out():
    clock = _Clock()
    cache = policy.ToolNameCache(ttl=120, clock=clock)
    cache.store(["tabs", "navigate"])
    assert cache.is_fresh() and cache.knows("tabs") is True and cache.knows("nope") is False
    clock.now += 119.9
    assert cache.is_fresh()
    clock.now += 0.1
    assert cache.is_fresh() is False, "stale at exactly the TTL"
    assert cache.knows("tabs") is True, "a stale list still answers; the caller re-lists"


def test_storing_again_restarts_the_clock_and_replaces_the_names():
    clock = _Clock()
    cache = policy.ToolNameCache(ttl=10, clock=clock)
    cache.store(["a"])
    clock.now += 9
    cache.store(["b"])
    clock.now += 9
    assert cache.is_fresh() and cache.knows("a") is False and cache.knows("b") is True


# ---- texts and payloads ----------------------------------------------------------------

def test_the_gate_refusal_names_the_backend_the_cause_and_where_to_look():
    text = policy.gate_refusal_text("http://127.0.0.1:2830/mcp", "Client failed to connect: x")
    assert text == ("BrowserPlug launcher: no MCP server answered at http://127.0.0.1:2830/mcp "
                    "(Client failed to connect: x). The launcher's stderr log names the "
                    "startup log and preflight.py.")


def test_the_connect_failure_text_is_the_old_proxys():
    assert (policy.connect_failure_text(OSError("All connection attempts failed"))
            == "Client failed to connect: All connection attempts failed")


def test_the_surface_key_is_server_pid_and_signature():
    assert policy.surface_key({"server_pid": 7, "tools_signature": "s", "x": 1}) == (7, "s")
    assert policy.surface_key({}) == (None, None)


def test_the_identity_headers_name_the_launcher_not_the_proxy():
    headers = policy.identity_headers(_config())
    assert headers == {LAUNCHER_PID_HEADER: "5151", LAUNCHER_VERSION_HEADER: "1.2.3",
                       HEALTH_IDENTITY_HEADER: HEALTH_IDENTITY_VALUE}


def test_the_timing_payload_is_what_the_server_matches_on():
    body = json.loads(policy.timing_payload(_config(), "tabs", 1752600000.5, 123.456))
    assert body == {"source": "launcher", "launcher_pid": 5151, "client_pid": 4242,
                    "tool": "tabs", "started_epoch": 1752600000.5, "total_ms": 123.5}
