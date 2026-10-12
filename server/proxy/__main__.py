"""The proxy transport's process: `<server env python> server/proxy/__main__.py <args>`.

launcher/launcher.py starts this (BROWSERPLUG_LAUNCHER_RELAY=0) with its own stdin and
stdout, so the client's MCP session runs here; the arguments are policy.parse_args's.
`python -m server.proxy` from the repo root works too. Run by file path, sys.path[0] is
server/proxy/, so it is dropped; the repo root and shared-py/ go first either way, computed
from __file__ and never from the cwd. Importing this module runs nothing
(tests/unit/test_import_side_effects.py imports every server module).
"""
import os
import sys

if __name__ == "__main__":
    _HERE = os.path.dirname(os.path.abspath(__file__))
    _ROOT = os.path.dirname(os.path.dirname(_HERE))
    if __package__ in (None, ""):
        sys.path[:] = [p for p in sys.path
                       if os.path.normcase(os.path.abspath(p or os.curdir))
                       != os.path.normcase(_HERE)]
    for _path in (os.path.join(_ROOT, "shared-py"), _ROOT):
        if _path not in sys.path:
            sys.path.insert(0, _path)

    from server.proxy.front import main
    sys.exit(main())
