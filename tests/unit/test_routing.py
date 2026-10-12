"""routing.py: the one client-key spelling and the resolution rules, with no server."""

from types import SimpleNamespace

from server.browsers import routing
from server.browsers.routing_notice import DepartedBrowser


def browser(instance_id, slot=1):
    return SimpleNamespace(instance_id=instance_id, slot=slot,
                           describe=lambda: f"[{slot}] {instance_id}")


def test_the_launcher_pid_header_wins_over_everything(monkeypatch):
    monkeypatch.setattr(routing, "process_started", lambda pid: {41: 1000, 7: 2000}.get(int(pid)))
    key = routing.client_key("41", "sid", {"sid": {"pid": 7}})
    assert key == routing.pid_client_key(41) == "pid:41@1000"


def test_a_session_borrows_the_pid_the_asgi_wrapper_resolved_for_it(monkeypatch):
    monkeypatch.setattr(routing, "process_started", lambda pid: {41: 1000, 7: 2000}.get(int(pid)))
    assert routing.client_key(None, "sid", {"sid": {"pid": 7}}) == "pid:7@2000"


def test_a_pid_that_cannot_be_looked_up_keeps_the_bare_spelling(monkeypatch):
    monkeypatch.setattr(routing, "process_started", lambda pid: None)
    assert routing.client_key("41") == "pid:41"


def test_a_session_with_no_known_pid_is_keyed_by_the_session():
    assert routing.client_key(None, "sid", {}) == "session:sid"
    assert routing.client_key(None, "sid", None) == "session:sid"


def test_no_http_context_is_the_local_client():
    assert routing.client_key() == routing.LOCAL_CLIENT_KEY == "local"


def test_the_connection_map_and_the_tool_side_spell_a_pid_alike(monkeypatch):
    """server.get_connections_info asks for pid_client_key(pid); a tool call carrying the
    same pid must land on the same key or the map cannot show the selection."""
    monkeypatch.setattr(routing, "process_started", lambda pid: 555)
    assert routing.client_key("1234") == routing.pid_client_key("1234")


def test_a_selected_browser_that_left_is_an_error_not_a_fallback():
    live = [browser("ff-2", 2), browser("ff-3", 3)]
    selection = {"pid:1": "ff-1"}
    departed = {"ff-1": DepartedBrowser("[1] Firefox", instance_id="ff-1", slot=1)}
    out = routing.resolve_target(live, selection, departed, "pid:1")
    assert isinstance(out, str) and "[1] Firefox is no longer connected" in out
    assert routing.routed_browser(live, selection, "pid:1") is None
    assert selection == {"pid:1": "ff-1"}, "with two browsers left the selection stays"


def test_the_last_browser_standing_drops_a_stale_selection():
    live = [browser("ff-2", 2)]
    selection = {"pid:1": "ff-1"}
    assert routing.resolve_target(live, selection, {}, "pid:1") is live[0]
    assert selection == {}


def test_nothing_connected_is_the_legacy_error():
    assert routing.resolve_target([], {}, {}, "local") == "No extension connection available"
