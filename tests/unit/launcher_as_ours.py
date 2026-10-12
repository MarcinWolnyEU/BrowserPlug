"""Run launcher/launcher.py with ONE pid declared to be this checkout's server.

The in-process e2e fixture (test_e2e_stdio_path.py) serves from the pytest process, whose
command line names no server entry point, so the launcher's ownership check
(mcp_port.server_pid_is_ours) rightly refuses to adopt it. Production stays as it is: this
wrapper patches the check in the launcher's own process, and only for the pid named in
BROWSERPLUG_TEST_OURS_PID. Any other pid still goes through the real check.

    python launcher_as_ours.py <path to launcher.py>
"""

import os
import runpy
import sys

launcher = os.path.abspath(sys.argv[1])
project_root = os.path.dirname(os.path.dirname(launcher))
sys.path.insert(0, os.path.join(project_root, "shared-py"))   # what launcher.py itself does

from browserplug_shared import mcp_port  # noqa: E402  (before the launcher binds the name)

_real = mcp_port.server_pid_is_ours
_ours = int(os.environ["BROWSERPLUG_TEST_OURS_PID"])


def _server_pid_is_ours(pid, project_root, probe=None, **kw):
    return True if pid == _ours else _real(pid, project_root, probe, **kw)


mcp_port.server_pid_is_ours = _server_pid_is_ours
sys.argv = [launcher]
runpy.run_path(launcher, run_name="__main__")
