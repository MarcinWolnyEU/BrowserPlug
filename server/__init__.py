"""BrowserPlug server package.

Importing the package imports nothing from it. server.py, mcp_tools.py and the other
modules are imported by name (`from server.server import BrowserPlugServer`), so that
loading one pure policy module never drags in the whole server, and no module has
import-time side effects.
"""
