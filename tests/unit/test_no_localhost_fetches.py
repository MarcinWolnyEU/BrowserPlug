"""No server code builds an http(s)://localhost URL.

A connect to "localhost" tries ::1 first. The server binds 127.0.0.1 (and 0.0.0.0 in LAN
mode), so on Windows the ::1 attempt times out for about two seconds before the IPv4 one
succeeds. Every URL the server side builds must use the 127.0.0.1 literal
(`mcp_port.connect_host` / `mcp_port.health_url`). Bind probes keep the name "localhost",
which is a different thing and is not matched here.
"""

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[2]
# The launcher and the shared port policy used to sit in server/, so they stay in scope.
CODE_DIRS = [ROOT / "server", ROOT / "launcher", ROOT / "shared-py"]
URL = re.compile(r"https?://localhost\b", re.IGNORECASE)


def test_no_server_module_builds_a_localhost_url():
    hits = []
    for path in sorted(p for d in CODE_DIRS for p in d.rglob("*.py")):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if URL.search(line):
                hits.append(f"{path.relative_to(ROOT)}:{n}: {line.strip()}")
    assert not hits, "\n".join(hits)
