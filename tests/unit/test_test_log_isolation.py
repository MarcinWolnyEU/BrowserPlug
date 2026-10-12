"""The test suite must never write the live server's log.

Importing `server` attaches a rotating file handler to BROWSERPLUG_LOG_FILE, and an unset
variable falls back to the repo-root browserplug-server.log that the running server
writes. A second writer on that file is what blocks its rotation on Windows.
tests/conftest.py points the variable at a throwaway file before any import; these pin
that it did, in this process and in what a spawned server inherits.
"""

import logging
import os

from browserplug_shared.mcp_port import LOG_FILE_ENV, default_log_path


def _norm(path):
    return os.path.normcase(os.path.abspath(path))


def test_the_suite_points_the_server_log_at_a_throwaway_file():
    configured = os.environ.get(LOG_FILE_ENV, "").strip()
    assert configured, f"{LOG_FILE_ENV} is unset, so importing server logs to the live file"
    assert _norm(configured) != _norm(default_log_path())


def test_no_handler_in_this_process_writes_the_live_log():
    files = [_norm(h.baseFilename) for h in logging.getLogger().handlers
             if getattr(h, "baseFilename", None)]
    assert files, ("no file handler at all: importing server no longer logs to a file, "
                   "so this test no longer proves anything")
    assert _norm(default_log_path()) not in files
