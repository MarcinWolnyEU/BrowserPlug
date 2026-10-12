"""/health reports a damaged running environment (server/reporting/reports.py).

`running_env_damage()` itself is tested in test_server_env.py and through preflight. This
is the glue: the report asks it on every build, and adds `server_env_damaged` only when it
answers, so a healthy report keeps the exact shape the report goldens pin.
"""

import socket

import test_imports  # noqa: F401  (path setup)
from browserplug_shared import server_env
from server.server import BrowserPlugServer


def _server():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    return BrowserPlugServer(host="localhost", port=port, start_mcp=False, ws_token="a" * 43)


def test_health_carries_the_damage_running_env_damage_reports(monkeypatch):
    monkeypatch.setattr(server_env, "running_env_damage", lambda: "pyvenv.cfg gone")
    assert _server().get_health_info()["server_env_damaged"] == "pyvenv.cfg gone"


def test_a_healthy_environment_leaves_the_key_out(monkeypatch):
    asked = []
    monkeypatch.setattr(server_env, "running_env_damage", lambda: asked.append(1))
    assert "server_env_damaged" not in _server().get_health_info()
    assert asked, "the report must ask on every build"
