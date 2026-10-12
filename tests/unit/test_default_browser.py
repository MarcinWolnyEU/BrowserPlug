"""The default-browser setting: what it stores, who it routes to, and when the server
may start a browser itself.

The property worth pinning: a pin is a PREFERENCE and falls back silently, while the
`browsers` tool's per-session selection is a decision and does not. Get that backwards
and closing one browser either strands every client (a pin that errors) or sends work
into the wrong logged-in profile (a selection that falls back).

The second one: a pinned browser must survive being closed. Its name and program path
are snapshotted at pick time, because a closed browser is not in the registry to be
asked, and both the options page's list and the launch depend on that snapshot.
"""
from types import SimpleNamespace

import pytest

from server.browsers import default_browser as db
from browserplug_shared.config_store import read_section, reset


@pytest.fixture(autouse=True)
def clean_config():
    """config.json is real state in the session's tmp state dir; one test's settings
    would otherwise be the next test's starting point."""
    reset()
    yield
    reset()


def fake_browser(instance_id, slot=1, edition="Firefox", exe_path=r"C:\ff\firefox.exe",
                 is_remote=False, engine="gecko", remote_addr=None, pid=100):
    # `name` is the agent-facing "<edition> pid <n>"; the picker shows `edition`.
    name = f"remote-{remote_addr}" if is_remote and remote_addr else f"{edition} pid {pid}"
    return SimpleNamespace(instance_id=instance_id, slot=slot, name=name, edition=edition,
                           engine=engine, exe_path=exe_path, is_remote=is_remote,
                           remote_addr=remote_addr, describe=lambda: f"[{slot}] {name}")


class FakeServer:
    def __init__(self, browsers=()):
        self._browsers = list(browsers)

    def live_browsers(self):
        return list(self._browsers)


# --- normalize --------------------------------------------------------------------

def test_defaults_are_first_started_and_launch_nothing():
    settings = db.defaults()
    assert settings["mode"] == db.MODE_FIRST_STARTED
    assert settings["instance_id"] is None
    assert settings["auto_launch"] is False


@pytest.mark.parametrize("raw", [None, "nonsense", 17, [], {"mode": "sideways"}])
def test_unusable_config_reads_as_the_default_never_as_an_error(raw):
    """A hand-edited config must not stop the server from starting."""
    assert db.normalize(raw)["mode"] == db.MODE_FIRST_STARTED


def test_pinned_without_an_instance_id_is_not_pinned():
    settings = db.normalize({"mode": "pinned", "auto_launch": True})
    assert settings["mode"] == db.MODE_FIRST_STARTED
    assert settings["auto_launch"] is False


def test_first_started_never_keeps_auto_launch_set():
    """The checkbox only exists beside a pinned browser; a stored True there would be a
    switch the options page never shows."""
    settings = db.normalize({"mode": db.MODE_FIRST_STARTED, "auto_launch": True})
    assert settings["auto_launch"] is False


def test_launch_args_survive_a_mode_that_drops_the_pin():
    settings = db.normalize({"mode": "pinned", "launch_args": "-P work"})
    assert settings["launch_args"] == "-P work"


def test_write_then_read_round_trips():
    db.write(db.normalize({"mode": "pinned", "instance_id": "abc", "name": "Zen",
                           "exe_path": r"C:\zen\zen.exe", "auto_launch": True}))
    stored = db.read()
    assert stored["instance_id"] == "abc"
    assert stored["auto_launch"] is True
    assert read_section(db.CONFIG_SECTION)["name"] == "Zen"


# --- routing ----------------------------------------------------------------------

def test_first_started_routes_to_the_first_connected_survivor():
    live = [fake_browser("a", slot=1), fake_browser("b", slot=2)]
    assert db.preferred(live, db.defaults()).instance_id == "a"


def test_a_pinned_browser_wins_over_first_connected():
    live = [fake_browser("a", slot=1), fake_browser("b", slot=2)]
    settings = db.normalize({"mode": "pinned", "instance_id": "b"})
    assert db.preferred(live, settings).instance_id == "b"


def test_a_pinned_browser_that_is_gone_falls_back_rather_than_failing():
    """The pin is a preference. Erroring here would take the default away from every
    client the moment that browser was closed."""
    live = [fake_browser("a", slot=1)]
    settings = db.normalize({"mode": "pinned", "instance_id": "gone"})
    assert db.preferred(live, settings).instance_id == "a"


def test_nothing_connected_resolves_to_nothing():
    assert db.preferred([], db.defaults()) is None


# --- the options page's payload ---------------------------------------------------

def test_payload_marks_the_asking_browser_as_current():
    me = fake_browser("me", slot=1)
    other = fake_browser("other", slot=2, edition="Zen Browser", pid=200)
    payload = db.payload(FakeServer([me, other]), requester=me)
    by_id = {b["instance_id"]: b for b in payload["browsers"]}
    assert by_id["me"]["is_current"] is True
    assert by_id["other"]["is_current"] is False


def test_a_pinned_browser_stays_listed_after_it_is_closed():
    """Dropping it would render as "First started" for a server that is still pinned,
    and the next save would clear a setting the user never touched."""
    db.write(db.normalize({"mode": "pinned", "instance_id": "gone", "name": "Zen Browser",
                           "exe_path": r"C:\zen\zen.exe"}))
    payload = db.payload(FakeServer([fake_browser("a")]))
    entry = next(b for b in payload["browsers"] if b["instance_id"] == "gone")
    assert entry["connected"] is False
    assert entry["name"] == "Zen Browser"
    assert entry["can_launch"] is True


def test_the_picker_names_a_browser_without_its_process_id():
    """ExtBrowser.name is "Firefox Developer Edition pid 48116": the right answer for an
    agent choosing between sockets, the wrong one for a person choosing a browser."""
    entry = db.payload(FakeServer([
        fake_browser("a", edition="Firefox Developer Edition", pid=48116)]))["browsers"][0]
    assert entry["name"] == "Firefox Developer Edition"


def test_a_remote_browser_keeps_its_address_in_the_picker():
    """Two remote browsers are otherwise indistinguishable, and neither has a local
    process to tell them apart."""
    entry = db.payload(FakeServer([
        fake_browser("r", edition="Firefox", is_remote=True,
                     remote_addr="10.0.0.5")]))["browsers"][0]
    assert entry["name"] == "Firefox (10.0.0.5)"


def test_a_remote_browser_cannot_be_launched():
    remote = fake_browser("r", exe_path=None, is_remote=True, remote_addr="10.0.0.5")
    payload = db.payload(FakeServer([remote]))
    assert payload["browsers"][0]["can_launch"] is False


# --- writes from the options page -------------------------------------------------

def test_setting_a_connected_browser_snapshots_how_to_describe_and_start_it():
    server = FakeServer([fake_browser("b", edition="Zen Browser", pid=200,
                                      exe_path=r"C:\zen\zen.exe")])
    payload, error = db.apply_request(server, {"mode": "pinned", "instance_id": "b"})
    assert error is None
    stored = db.read()
    # The picker's name, not the agent-facing one: no pid.
    assert (stored["name"], stored["exe_path"]) == ("Zen Browser", r"C:\zen\zen.exe")
    assert payload["settings"]["instance_id"] == "b"


def test_pinning_a_browser_nobody_has_seen_is_refused():
    _, error = db.apply_request(FakeServer([]), {"mode": "pinned", "instance_id": "ghost"})
    assert error and "not connected" in error


def test_resaving_a_pinned_but_closed_browser_keeps_its_snapshot():
    db.write(db.normalize({"mode": "pinned", "instance_id": "gone", "name": "Zen Browser",
                           "exe_path": r"C:\zen\zen.exe"}))
    _, error = db.apply_request(FakeServer([]), {"mode": "pinned", "instance_id": "gone",
                                                 "auto_launch": True})
    assert error is None
    assert db.read()["exe_path"] == r"C:\zen\zen.exe"


def test_auto_launch_is_refused_when_the_server_cannot_start_that_browser():
    server = FakeServer([fake_browser("r", exe_path=None, is_remote=True,
                                      remote_addr="10.0.0.5")])
    _, error = db.apply_request(server, {"mode": "pinned", "instance_id": "r",
                                         "auto_launch": True})
    assert error and "cannot open it" in error


def test_going_back_to_first_started_clears_the_pin():
    db.write(db.normalize({"mode": "pinned", "instance_id": "b", "name": "Zen",
                           "exe_path": r"C:\zen\zen.exe", "auto_launch": True}))
    _, error = db.apply_request(FakeServer([]), {"mode": db.MODE_FIRST_STARTED})
    assert error is None
    stored = db.read()
    assert stored["mode"] == db.MODE_FIRST_STARTED
    assert stored["instance_id"] is None
    assert stored["auto_launch"] is False


INJECTED = "--renderer-cmd-prefix=calc.exe"


def test_launch_args_from_a_websocket_write_never_reach_the_launch_command(tmp_path, caplog):
    """apply_request runs for any authenticated WebSocket peer, a phone included. Its
    launch_args used to be stored and then handed to Popen as argv."""
    exe = tmp_path / "chrome.exe"
    exe.write_text("")
    server = FakeServer([fake_browser("b", exe_path=str(exe))])
    with caplog.at_level("WARNING"):
        _, error = db.apply_request(server, {"mode": "pinned", "instance_id": "b",
                                             "auto_launch": True, "launch_args": INJECTED})
    assert error is None
    assert db.read()["launch_args"] == ""
    argv, reason = db.launch_command(db.read())
    assert reason is None
    assert argv == [str(exe)]
    assert any("launch_args" in r.getMessage() and r.levelname == "WARNING"
               for r in caplog.records)


def test_a_websocket_write_keeps_the_launch_args_set_locally(tmp_path, caplog):
    exe = tmp_path / "firefox.exe"
    exe.write_text("")
    db.write(db.normalize({"mode": "pinned", "instance_id": "b", "exe_path": str(exe),
                           "auto_launch": True, "launch_args": "-P work"}))
    server = FakeServer([fake_browser("b", exe_path=str(exe))])

    with caplog.at_level("WARNING"):
        db.apply_request(server, {"mode": "pinned", "instance_id": "b", "auto_launch": True,
                                  "launch_args": "-P work"})
    assert db.read()["launch_args"] == "-P work"
    assert not caplog.records, "resending the stored value is not an attack"

    db.apply_request(server, {"mode": "pinned", "instance_id": "b", "auto_launch": True,
                              "launch_args": INJECTED})
    assert db.read()["launch_args"] == "-P work"
    db.apply_request(server, {"mode": db.MODE_FIRST_STARTED})
    assert db.read()["launch_args"] == "-P work", "switching the pin off must not lose them"


@pytest.mark.parametrize("request_body", [None, "text", {"mode": "sideways"}, {}])
def test_a_malformed_write_is_refused_without_touching_the_stored_setting(request_body):
    db.write(db.normalize({"mode": "pinned", "instance_id": "b", "name": "Zen",
                           "exe_path": r"C:\zen\zen.exe"}))
    payload, error = db.apply_request(FakeServer([]), request_body)
    assert payload is None and error
    assert db.read()["instance_id"] == "b"


# --- starting a browser -----------------------------------------------------------

def test_windows_paths_survive_argument_splitting():
    """posix-mode shlex reads every backslash as an escape and turns C:\\Users\\me into
    C:Usersme, which is why the split is posix=False."""
    assert db.split_launch_args(r'-P work --profile "C:\Users\me\p 1"') == [
        "-P", "work", "--profile", r"C:\Users\me\p 1"]


def test_empty_arguments_are_no_arguments():
    assert db.split_launch_args("") == []
    assert db.split_launch_args("   ") == []


def test_unbalanced_quotes_still_produce_arguments():
    assert db.split_launch_args('-P "work') == ["-P", '"work']


def test_launch_command_refuses_every_setting_that_is_not_a_permitted_start():
    assert db.launch_command(db.defaults())[0] is None
    pinned = db.normalize({"mode": "pinned", "instance_id": "b", "exe_path": r"C:\no\such.exe"})
    assert db.launch_command(pinned)[0] is None            # auto_launch off
    pinned["auto_launch"] = True
    argv, reason = db.launch_command(pinned)
    assert argv is None and "no longer at" in reason        # the program is gone


def test_launch_command_names_the_program_and_its_arguments(tmp_path):
    exe = tmp_path / "firefox.exe"
    exe.write_text("")
    settings = db.normalize({"mode": "pinned", "instance_id": "b", "exe_path": str(exe),
                             "auto_launch": True, "launch_args": "-P work"})
    argv, reason = db.launch_command(settings)
    assert reason is None
    assert argv == [str(exe), "-P", "work"]


@pytest.mark.asyncio
async def test_nothing_is_started_when_the_user_did_not_allow_it(monkeypatch):
    started = []
    monkeypatch.setattr(db.subprocess, "Popen", lambda *a, **k: started.append(a))
    assert await db.LaunchGuard().ensure_running(FakeServer([]), db.defaults()) is None
    assert started == []


@pytest.mark.asyncio
async def test_a_start_that_never_connects_is_not_retried_immediately(monkeypatch, tmp_path):
    """A browser that starts but never connects (extension disabled, wrong profile)
    would otherwise open one window per tool call."""
    exe = tmp_path / "firefox.exe"
    exe.write_text("")
    settings = db.normalize({"mode": "pinned", "instance_id": "b", "exe_path": str(exe),
                             "auto_launch": True})
    starts = []
    monkeypatch.setattr(db.subprocess, "Popen", lambda *a, **k: starts.append(a))
    monkeypatch.setattr(db, "launch_wait_seconds", lambda: 1.0)

    guard = db.LaunchGuard()
    assert await guard.ensure_running(FakeServer([]), settings) is None
    assert await guard.ensure_running(FakeServer([]), settings) is None
    assert len(starts) == 1


@pytest.mark.asyncio
async def test_a_browser_that_connects_is_returned(monkeypatch, tmp_path):
    exe = tmp_path / "firefox.exe"
    exe.write_text("")
    settings = db.normalize({"mode": "pinned", "instance_id": "b", "exe_path": str(exe),
                             "auto_launch": True})
    server = FakeServer([])

    def fake_popen(*args, **kwargs):
        server._browsers.append(fake_browser("b"))
        return SimpleNamespace(pid=1)

    monkeypatch.setattr(db.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(db, "launch_wait_seconds", lambda: 5.0)
    browser = await db.LaunchGuard().ensure_running(server, settings)
    assert browser is not None and browser.instance_id == "b"


def test_the_launch_wait_is_clamped(monkeypatch):
    monkeypatch.setenv(db.LAUNCH_WAIT_ENV, "9999")
    assert db.launch_wait_seconds() == db.LAUNCH_WAIT_MAX
    monkeypatch.setenv(db.LAUNCH_WAIT_ENV, "not a number")
    assert db.launch_wait_seconds() == db.LAUNCH_WAIT_DEFAULT
    monkeypatch.delenv(db.LAUNCH_WAIT_ENV, raising=False)
    assert db.launch_wait_seconds() == db.LAUNCH_WAIT_DEFAULT
