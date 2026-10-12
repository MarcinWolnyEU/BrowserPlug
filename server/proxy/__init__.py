"""The launcher's proxy transport (BROWSERPLUG_LAUNCHER_RELAY=0), built on the MCP SDK.

launcher/launcher.py runs `server/proxy/__main__.py` as a child on its own stdio, with the
server environment's python. The child serves MCP to the client over stdio and forwards to
the shared server over streamable HTTP. Importing this package imports nothing.
"""
