"""
Test configuration constants for BrowserPlug testing

Ports come from port_coordinator, which allocates a genuinely-free port per named role
on first use and keeps it for the rest of the process. So the values below are stable
within a session (every caller asking for the same role agrees) and unique across
concurrent sessions.
"""

from port_coordinator import get_port_by_type

# Default test configuration - uses dynamic ports
DEFAULT_TEST_CONFIG = {
    'test_timeout': 10.0,    # Default test timeout
    'server_startup_wait': 0.5,  # Time to wait for server startup
}

# Firefox extension test configuration - uses dynamic ports
FIREFOX_TEST_CONFIG = {
    'profile_name': 'browserplug-test-profile',
    # Time for extension to install and connect. A cold Firefox start under
    # load occasionally needs >3s, and a miss skips the whole test — the wait
    # is a ceiling (connect returns as soon as the extension shows up), so a
    # generous value costs nothing on the happy path.
    # Raised 10 -> 25 when the WS gained token auth: the extension can no longer
    # register on its first socket. It must connect, be refused (4401), fetch the
    # token over HTTP, and reconnect — an extra round-trip this budget predates.
    # Under a loaded full-suite run that overran 10s and the tests SKIPPED.
    'extension_install_wait': 25.0,
    'firefox_startup_wait': 5.0,    # Time for Firefox to fully start
}

def get_test_ports(suite_name):
    """Get dynamically allocated port configuration for a specific test suite"""
    return {
        'websocket': get_port_by_type('websocket'),
        'mcp': get_port_by_type('mcp')
    }

def get_available_port_range(suite_name):
    """Get a safe port range for dynamic allocation within a test suite"""
    # Return the websocket port as a single-port range (for backward compatibility)
    websocket_port = get_port_by_type('websocket')
    return (websocket_port, websocket_port)

# Backward compatibility - kept for existing tests
def get_firefox_test_port():
    """Get a dynamically allocated port for Firefox extension tests"""
    ports = get_test_ports('integration_basic')
    return ports['websocket']

# Legacy TEST_PORTS for backward compatibility
def _get_test_ports_dict():
    """Get test ports dictionary with dynamic allocation"""
    return {
        'integration': get_test_ports('integration_basic'),              # Shared by Firefox and WebSocket server tests
        'integration_mcp': get_test_ports('integration_mcp'),            # MCP protocol functionality tests
    }

TEST_PORTS = _get_test_ports_dict()