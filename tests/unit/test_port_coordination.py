#!/usr/bin/env python3
"""
Port coordination: the coordination FILE that carries a session's ports to Firefox.

Adopted from the orphaned tests/test_port_coordination.py (never collected by either
`pytest unit/` or `pytest integration/`). It used to fail on
test_multiple_coordination_instances — "assert 40400 != 40400" — because the ports it
called dynamic were fixed constants. That is fixed; the assertions it always made are
now the real contract, so the file earns its place instead of being deleted.

Complements unit/test_port_allocation.py: that one covers the allocator, this one
covers coordination-file round-tripping. No Firefox required.
"""

import json
import os
import tempfile

from firefox_test_utils import FirefoxTestManager
from port_coordinator import FirefoxPortCoordinator, coordinated_test_ports


def test_port_coordination_basic():
    """Ports round-trip through the coordination file, which is cleaned up after."""
    with coordinated_test_ports() as (ports, coord_file):
        assert os.path.exists(coord_file), "Coordination file should exist"

        with open(coord_file, 'r') as f:
            data = json.load(f)
        assert data['websocket_port'] == ports['websocket']
        assert data['mcp_port'] == ports['mcp']

    assert not os.path.exists(coord_file), "Coordination file should be cleaned up"


def test_firefox_coordination():
    """FirefoxPortCoordinator reads the session's ports back out of the file."""
    with coordinated_test_ports() as (ports, coord_file):
        assert FirefoxPortCoordinator.read_ports(coord_file) == ports

        with tempfile.TemporaryDirectory() as temp_profile:
            configured_port = FirefoxPortCoordinator.create_extension_config(
                coord_file, temp_profile)
            assert configured_port == ports['websocket']

            config_path = os.path.join(temp_profile, 'browser-extension-data',
                                       'browserplug@marcinwolny.net', 'config.json')
            assert os.path.exists(config_path), "Extension config should be created"

            with open(config_path, 'r') as f:
                config = json.load(f)
            assert config['port'] == ports['websocket']
            assert config['hostname'] == 'localhost'


def test_firefox_test_manager_coordination():
    """A Firefox manager built from a coordination file targets the session's port."""
    with coordinated_test_ports() as (ports, coord_file):
        firefox = FirefoxTestManager(test_port=ports['websocket'],
                                     coordination_file=coord_file)
        try:
            assert firefox.test_port == ports['websocket']

            # The coordination file is authoritative — it overrides the constructor port
            # (this is what settles the port before the profile cache is consulted).
            stale = FirefoxTestManager(test_port=1234, coordination_file=coord_file)
            stale._setup_extension_test_config()
            assert stale.test_port == ports['websocket']
        finally:
            firefox.cleanup()


def test_multiple_coordination_instances():
    """Overlapping coordinations must not land on the same server.

    This is the assertion that used to read "assert 40400 != 40400".
    """
    with coordinated_test_ports() as (ports1, _):
        with coordinated_test_ports() as (ports2, _):
            assert ports1['websocket'] != ports2['websocket']
            assert ports1['mcp'] != ports2['mcp']
            assert len({ports1['websocket'], ports1['mcp'],
                        ports2['websocket'], ports2['mcp']}) == 4


def test_managers_seed_the_mcp_port_the_extension_bootstraps_from():
    """The extension fetches its first token only from the MCP port its config names (or
    2830..2942), and a test server's MCP port is random: both harness managers must
    carry the coordination file's `mcp` port into the seeded config, or every
    browser-backed test dies with "extension never connected"."""
    from chromium_test_utils import ChromiumTestManager
    with coordinated_test_ports() as (ports, coord_file):
        fx = FirefoxTestManager(test_port=ports['websocket'], coordination_file=coord_file)
        try:
            assert fx._test_storage_config()['mcpPort'] == ports['mcp']
            explicit = FirefoxTestManager(test_port=ports['websocket'], mcp_port=45678)
            assert explicit._test_storage_config()['mcpPort'] == 45678
            bare = FirefoxTestManager(test_port=ports['websocket'])
            assert 'mcpPort' not in bare._test_storage_config()
        finally:
            fx.cleanup()
        cr = ChromiumTestManager(chrome_path='chrome', test_port=ports['websocket'],
                                 coordination_file=coord_file)
        assert cr.mcp_port == ports['mcp']
