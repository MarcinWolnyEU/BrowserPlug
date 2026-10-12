#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ['psutil>=7.2.2; sys_platform == "darwin"']
# ///
"""Forwarding stub: MCP client configs run this path (`<python> server/launcher.py`, or
`uv run --script server/launcher.py`); runpy runs launcher/launcher.py as __main__."""
import os
import runpy

if __name__ == "__main__":
    runpy.run_path(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "launcher", "launcher.py"), run_name="__main__")
