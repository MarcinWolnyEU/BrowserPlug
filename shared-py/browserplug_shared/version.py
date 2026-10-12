#!/usr/bin/env python3
"""The BrowserPlug component version: ONE number for the server AND the launcher.

The extension has always carried a version (manifest.json, reported in
extension_hello and echoed by /health). The server and the launcher had none, so a
browser running a newer build than the server it talks to was indistinguishable
from a matched pair, and the wire contract between them is exactly the thing that
changes between builds.

This module is that missing number, and it is deliberately the ONLY place it
lives on the Python side: server.py and launcher.py both import it, so they can
never disagree (the same rule mcp_port.py enforces for the port policy).

KEEP IT IN LOCKSTEP with both extension manifests and package.json:
    extension-gecko/manifest.json     "version"
    extension-chromium/manifest.json  "version"
    package.json                      "version"
    shared-py/browserplug_shared/version.py                 __version__      <- this file
Parity invariant H (tests/parity/test_parity.py::test_H_version_lockstep) fails
the build if any of the four drifts, so bumping one means bumping all four.

Stdlib-only and import-cheap on purpose: launcher.py imports it before it has
decided whether a server even exists.
"""

__version__ = "1.0.0"

# Alias for readers that import the module rather than the symbol.
BROWSERPLUG_VERSION = __version__

# How a launcher tells the server which build it is. The launcher stamps both on
# its /health poll (server/launcher.py _watch_tools, every few seconds for the whole
# life of the process); the server records them per launcher pid and compares them
# with its own __version__; see server/reporting/version_check.py.
#
# The pid rides along because /health is answered before any MCP session exists:
# without it the server would know a version but not whose it is.
LAUNCHER_VERSION_HEADER = "X-BrowserPlug-Launcher-Version"
LAUNCHER_PID_HEADER = "X-BrowserPlug-Launcher-Pid"

# Sent by a launcher on its /health poll to ask for the identity-only reply (service,
# server_pid, version, ports, tools_signature). The poll runs every few seconds for the life of
# every launcher and needs none of the report, whose psutil scans are the costly part. The
# full /health stays what the popup and the options page read.
HEALTH_IDENTITY_HEADER = "X-BrowserPlug-Health"
HEALTH_IDENTITY_VALUE = "identity"

# Which client process spawned a launcher. Both launcher transports send it on every backend
# request; the server keys the client (and its browser selection) on it, because the TCP peer
# is the launcher itself. Read in server/web/http_endpoints.py.
CLIENT_PID_HEADER = "X-BrowserPlug-Client-Pid"
