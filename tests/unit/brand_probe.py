"""Test-only helper: the Gecko-only view of browser_brand()."""
from server.browsers.browser_identity import browser_brand


def gecko_brand(proc_name, exe_path=None):
    """Brand family of a Gecko process, or None for a Chromium browser or a non-browser."""
    family, engine = browser_brand(proc_name, exe_path)
    return family if engine == "gecko" else None
