"""The routing tables that grow with every client and browser the server has seen.

`browser_selection`, `RoutingMemory._served` and the registry's `departed` used to grow
for the life of the process. Each is now bounded (age and/or count), and the tests move
time with an injected clock instead of sleeping.
"""

from types import SimpleNamespace

import pytest

import test_imports  # noqa: F401
from server.browsers.client_state import CLIENT_TTL_SECONDS, MAX_DEPARTED, ExpiringMap
from server.browsers.routing_notice import RoutingMemory
from server.server import BrowserPlugServer


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def server():
    return BrowserPlugServer(host="localhost", port=8772, start_mcp=False)


def fake_browser(n):
    return SimpleNamespace(instance_id=f"ff-{n}", slot=n, ws=object(),
                           last_disconnect_at=None, last_close_code=1006,
                           last_close_reason="", describe=lambda n=n: f"[{n}] Firefox")


# --- the departed record ---------------------------------------------------------------

def test_the_departed_record_is_capped_and_evicts_the_oldest(server):
    for n in range(MAX_DEPARTED + 10):
        server.registry.remove(fake_browser(n))
    assert len(server._departed) == MAX_DEPARTED
    assert "ff-0" not in server._departed, "the oldest departure goes first"
    assert f"ff-{MAX_DEPARTED + 9}" in server._departed


def test_a_browser_that_leaves_again_counts_as_the_newest_departure(server):
    for n in range(MAX_DEPARTED):
        server.registry.remove(fake_browser(n))
    server.registry.remove(fake_browser(0))          # ff-0 again: now the freshest
    server.registry.remove(fake_browser(1000))       # forces one eviction
    assert "ff-0" in server._departed
    assert "ff-1" not in server._departed


# --- ExpiringMap -----------------------------------------------------------------------

def test_an_entry_unseen_for_the_ttl_is_dropped_on_the_next_sweep():
    clock = Clock()
    m = ExpiringMap(ttl=100, clock=clock)
    m["a"] = 1
    clock.advance(60)
    m["b"] = 2
    clock.advance(60)                     # a is 120 s old, b 60 s
    assert m.sweep() == ["a"]
    assert dict(m) == {"b": 2}


def test_touching_a_key_keeps_it_alive():
    clock = Clock()
    m = ExpiringMap(ttl=100, clock=clock)
    m["a"] = 1
    clock.advance(90)
    m.touch("a")
    clock.advance(90)
    m.sweep()
    assert "a" in m


def test_reads_do_not_keep_an_entry_alive():
    clock = Clock()
    m = ExpiringMap(ttl=100, clock=clock)
    m["a"] = 1
    clock.advance(90)
    assert m.get("a") == 1 and "a" in m
    clock.advance(90)
    m.sweep()
    assert "a" not in m


def test_traffic_alone_sweeps_an_overdue_table():
    clock = Clock()
    m = ExpiringMap(ttl=100, clock=clock)
    m["old"] = 1
    clock.advance(500)
    m["new"] = 2                          # the write is what notices the age
    assert dict(m) == {"new": 2}


def test_the_count_cap_evicts_the_least_recently_seen():
    clock = Clock()
    m = ExpiringMap(max_entries=3, clock=clock)
    for k in "abc":
        clock.advance(1)
        m[k] = k
    clock.advance(1)
    m.touch("a")                          # b is now the stalest
    clock.advance(1)
    m["d"] = "d"
    assert sorted(m) == ["a", "c", "d"]


def test_on_drop_names_what_expiry_removed_but_not_what_the_caller_deleted():
    clock = Clock()
    dropped = []
    m = ExpiringMap(ttl=10, clock=clock, on_drop=dropped.extend)
    m["a"], m["b"] = 1, 2
    del m["b"]
    clock.advance(11)
    m.sweep()
    assert dropped == ["a"]


def test_it_stays_a_plain_dict_for_equality_and_mutation():
    m = ExpiringMap(ttl=10, max_entries=5)
    m.update({"a": 1})
    m.setdefault("b", 2)
    assert m == {"a": 1, "b": 2}
    assert m.pop("a") == 1
    m.clear()
    assert m == {} and not m._seen


# --- RoutingMemory ---------------------------------------------------------------------

def test_a_client_not_heard_from_in_a_day_is_forgotten_by_the_routing_memory():
    clock = Clock()
    memory = RoutingMemory(clock=clock)
    memory.served("pid:1@5", "ff-a")
    clock.advance(CLIENT_TTL_SECONDS + 1)
    memory.served("pid:2@6", "ff-b")      # other traffic triggers the sweep
    assert memory.previous("pid:1@5") is None
    assert memory.previous("pid:2@6") == "ff-b"


def test_an_active_client_keeps_its_memory_across_the_ttl():
    clock = Clock()
    memory = RoutingMemory(clock=clock)
    for _ in range(3):
        clock.advance(CLIENT_TTL_SECONDS * 0.6)
        memory.served("pid:1@5", "ff-a")
    assert memory.previous("pid:1@5") == "ff-a"


def test_the_routing_memory_is_capped():
    memory = RoutingMemory(max_entries=4)
    for n in range(10):
        memory.chose(f"pid:{n}", "ff-a")
    assert memory.previous("pid:0") is None
    assert memory.previous("pid:9") == "ff-a"


def test_drop_clients_removes_a_client():
    memory = RoutingMemory()
    memory.chose("pid:1", "ff-a")
    memory.drop_clients(["pid:1", "pid:404"])
    assert memory.previous("pid:1") is None


# --- the server's wiring ---------------------------------------------------------------

def test_the_servers_tables_are_the_bounded_kind(server):
    assert isinstance(server.browser_selection, ExpiringMap)
    assert server.browser_selection.ttl == CLIENT_TTL_SECONDS
    assert isinstance(server._departed, ExpiringMap)
    assert server._departed.max_entries == MAX_DEPARTED


def test_an_expired_selection_takes_its_served_entry_with_it(server):
    clock = Clock()
    server.browser_selection.clock = clock
    server.browser_selection._last_sweep = clock()
    server.routing_memory.chose("pid:1@5", "ff-a")
    server.browser_selection["pid:1@5"] = "ff-a"
    clock.advance(CLIENT_TTL_SECONDS + 1)
    server.browser_selection.sweep()
    assert "pid:1@5" not in server.browser_selection
    assert server.routing_memory.previous("pid:1@5") is None


def test_resolving_a_call_keeps_the_callers_selection_alive(server):
    clock = Clock()
    sel = server.browser_selection
    sel.clock = clock
    sel._last_sweep = clock()
    live = [SimpleNamespace(instance_id=i, slot=n, describe=lambda: "x")
            for n, i in enumerate(("ff-a", "ff-b"), 1)]
    server.live_browsers = lambda: live
    sel["pid:1@5"] = "ff-a"
    for _ in range(3):
        clock.advance(CLIENT_TTL_SECONDS * 0.6)
        assert server.resolve_target_browser("pid:1@5") is live[0]
    assert sel["pid:1@5"] == "ff-a"
