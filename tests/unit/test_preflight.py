"""Unit tests for server/preflight.py — "is BrowserPlug installed and working?".

The module exists because the diagnostic was the thing that failed: `server_info`
answers over the transport it would diagnose, so a collapsed install replied "The
socket connection was closed unexpectedly". So the property under test here is not
"does it print nicely" — it is that **every precondition is reported individually,
that a failure names its own remedy, and that the report still exists when the
subject is dead**.

The checks are pure functions over injected state, so all three diagnosed scenarios
are reachable with no browser, no server and no network:
  * a checkout whose server environment is missing or broken (uv absent, a stale lock, an
    environment a `uv cache prune` gutted),
  * a valid install with the browser extension disconnected,
  * a running server whose WebSocket has been severed,
plus the fourth condition that looks identical from the client side — a config
written correctly but never LOADED, which must be distinguishable from a config
that is wrong.
"""

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import urllib.request

import pytest

import test_imports  # noqa: F401  (path setup)
from browserplug_shared import server_env
from server import preflight
from server.reporting import reports

START = server_env.START_COMMAND        # `python server/control.py start`


# --- state factories: the injected inputs the pure checks consume -------------------

ENV_DIR = os.path.join("C:", "state", "server-env")


def env_state(**over):
    """A healthy interpreter probe: the server's environment, every dependency present."""
    state = {
        "root": os.path.join("C:", "proj"),
        "mode": "uv",
        "env_dir": ENV_DIR,
        "python": os.path.join(ENV_DIR, "Scripts", "python.exe"),
        "python_exists": True,
        "uv": os.path.join("C:", "bin", "uv.exe"),
        "problem": None,
        "remedy": None,
        "uv_output": "",
        "damaged": None,
        "cfg_home": os.path.join("C:", "Python312"),
        "cfg_home_exists": True,
        "cfg_version": "3.12.5",
        "ran": True,
        "in_process": False,
        "executable": os.path.join(ENV_DIR, "Scripts", "python.exe"),
        "runtime_version": "3.12.5",
        "base_prefix": os.path.join("C:", "Python312"),
        "modules": {name: "1.0" for name in preflight.REQUIRED_PACKAGES},
        "error": None,
    }
    state.update(over)
    return state


@pytest.fixture
def no_server_env(tmp_path, monkeypatch):
    """This process's environment with no seam, no uv and a scratch home, so a CLI run over
    a scratch root is deterministic and reads no real client config."""
    monkeypatch.delenv(server_env.SERVER_PYTHON_ENV, raising=False)
    for name in (server_env.UV_ENV, "UV", "XDG_BIN_HOME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PATH", "")
    for name in ("USERPROFILE", "HOME", "APPDATA", "LOCALAPPDATA"):
        monkeypatch.setenv(name, str(tmp_path))
    return tmp_path


class FakeUv:
    """uv's read-only answers, for inspect_server_env: lock --check, sync --check, find."""

    def __init__(self, lock=(0, "", ""), sync_check=(0, "", "Would use script environment at: "
                                                           "E\nWould make no changes\n"),
                 find=(0, "", "")):
        self.answers = {"lock": lock, "sync_check": sync_check, "find": find}
        self.calls = []

    def __call__(self, argv, *, cwd, timeout, env):
        kind = ("lock" if argv[1:2] == ["lock"] else
                "sync_check" if argv[1:2] == ["sync"] else "find")
        assert kind != "sync_check" or "--check" in argv, f"a mutating sync: {argv}"
        self.calls.append(argv)
        code, out, err = self.answers[kind]
        # `E` stands for the environment directory, which the check must name as its own.
        return code, out, err.replace("environment at: E",
                                      f"environment at: {server_env.server_env_dir()}")


def standalone(root, uv, environ):
    """collect_interpreter's CLI path over a fake uv (no seam)."""
    def inspect(r, environ=None):
        return server_env.inspect_server_env(r, environ=environ, runner=uv)
    return preflight.collect_interpreter(str(root), in_process=False, environ=environ,
                                         inspect=inspect)


def scratch_checkout(tmp_path):
    root = tmp_path / "checkout"
    (root / "server").mkdir(parents=True)
    (root / "server" / "server.py").write_text("# stand-in\n", encoding="utf-8")
    (root / "server" / "server.py.lock").write_text("", encoding="utf-8")
    uv = tmp_path / "bin" / ("uv.exe" if os.name == "nt" else "uv")
    uv.parent.mkdir()
    uv.write_text("", encoding="utf-8")
    return root, {"PATH": str(uv.parent)}


def health(**over):
    """A /health payload: server up, nothing connected to it."""
    payload = {
        "service": "browserplug", "status": "ok",
        "server_pid": 4242, "server_version": "1.7.6",
        "uptime_seconds": 3725, "ws_port": 8772, "mcp_port": 2830,
        "version_warning": None,
        "browsers": [], "extension_connected": False, "extension_instances": [],
        "connections": [], "client_pids": [],
        "generated_epoch": 1_000_000.0,
    }
    payload.update(over)
    return payload


def discovery(health_payload=None, *, probed=True, pinned=None, port=2830, error=None):
    return {"probed": probed, "pinned": pinned,
            "port": port if health_payload else None,
            "health": health_payload, "error": error}


def connected_browser(slot=1, name="Zen Browser", version="1.7.6", age=3.0):
    """The two halves /health reports for a live browser: the roster row and the
    connections row that carries the (heartbeat-inclusive) activity stamp."""
    return {
        "browsers": [{"slot": slot, "name": name, "version": version}],
        "extension_connected": True,
        "connections": [{"kind": "extension", "slot": slot, "label": name,
                         "activity_epoch": 1_000_000.0 - age}],
    }


# --- Scenario 1: a checkout whose server environment is missing or broken -------------

def test_an_unbuilt_server_environment_is_informational_not_a_failure(tmp_path):
    """The next start builds it by itself, so a FAIL (and exit 1) would be false; a cold
    first build can still take long enough to matter, so it is not a plain pass."""
    root, environ = scratch_checkout(tmp_path)
    uv = FakeUv(sync_check=(0, "", "Would create script environment at: E\n"))
    state = standalone(root, uv, environ)
    check = preflight.check_interpreter(state)
    assert check["ok"] is None
    assert "has not been built yet" in check["detail"]
    assert "tens of seconds" in check["remedy"] and "times out" not in check["detail"]
    assert START in check["remedy"]
    deps = preflight.check_dependencies(state)
    assert deps["ok"] is None and "has not been built yet" in deps["detail"]
    assert "fix the `interpreter` item" not in deps["detail"]


@pytest.mark.parametrize("stderr,words", [
    ("Would use script environment at: E\nWould install 1 package\n + idna==3.9\n"
     "error: The environment is outdated; run `uv sync` to update the environment\n",
     "out of date with the lock"),
    ("Would replace script environment at: E\nWould install 2 packages\n"
     "error: The environment is outdated; run `uv sync` to update the environment\n",
     "replace the environment"),
], ids=["out_of_date", "replace"])
def test_an_environment_behind_the_lock_is_informational(tmp_path, stderr, words):
    root, environ = scratch_checkout(tmp_path)
    state = standalone(root, FakeUv(sync_check=(1, "", stderr)), environ)
    check = preflight.check_interpreter(state)
    assert check["ok"] is None
    assert words in check["detail"] and "launcher spawn" in check["detail"]
    assert START in check["detail"] and START in check["remedy"]
    assert preflight.check_dependencies(state)["ok"] is None


def test_a_check_that_fails_outright_is_still_a_failure(tmp_path):
    root, environ = scratch_checkout(tmp_path)
    uv = FakeUv(sync_check=(2, "", "error: No interpreter found for Python >=3.12\n"))
    check = preflight.check_interpreter(standalone(root, uv, environ))
    assert check["ok"] is False
    assert "No interpreter found" in check["detail"]


def test_a_missing_uv_names_the_install(tmp_path):
    root, _environ = scratch_checkout(tmp_path)
    check = preflight.check_interpreter(standalone(root, FakeUv(), {"PATH": ""}))
    assert check["ok"] is False
    assert "uv was not found" in check["detail"]
    assert "https://docs.astral.sh/uv/" in check["remedy"]
    # uv is not the only way: the README's venv route and its variable are named too.
    assert server_env.SERVER_PYTHON_ENV in check["remedy"]
    assert "--requirements-from-script" in check["remedy"]


def test_a_broken_override_python_is_fixed_with_pip_not_uv():
    """Under BROWSERPLUG_SERVER_PYTHON nothing rebuilds the environment: the remedy is a
    pip install into that interpreter, not `python server/control.py start` alone."""
    python = os.path.join("C:", "venv", "Scripts", "python.exe")
    state = env_state(mode="override", python=python, ran=False, error="boom")
    check = preflight.check_interpreter(state)
    assert check["ok"] is False
    assert f"{python} -m pip install --requirements-from-script" in check["remedy"]
    assert server_env.SERVER_PYTHON_ENV in check["remedy"]
    assert server_env.LOCK_COMMAND not in check["remedy"]
    missing = env_state(mode="override", python=python,
                        modules={**{n: "1.0" for n in preflight.REQUIRED_PACKAGES},
                                 "websockets": None})
    assert "--requirements-from-script" in preflight.check_dependencies(missing)["remedy"]


def test_a_stale_lock_names_our_lock_command_not_uvs(tmp_path):
    root, environ = scratch_checkout(tmp_path)
    uv = FakeUv(lock=(1, "", "error: The lockfile at `uv.lock` needs to be updated, but "
                             "`--check` was provided.\n\nhint: To update the lockfile, run "
                             "`uv lock`.\n"))
    check = preflight.check_interpreter(standalone(root, uv, environ))
    assert check["ok"] is False
    assert server_env.LOCK_COMMAND in check["remedy"]
    assert "run `uv lock`." not in check["detail"]


def test_the_standalone_doctor_only_asks_uv_read_only_questions(tmp_path, monkeypatch):
    root, environ = scratch_checkout(tmp_path)
    monkeypatch.setenv("BROWSERPLUG_STATE_DIR", str(tmp_path / "state"))
    env_dir = tmp_path / "state" / server_env.SERVER_ENV_DIR_NAME
    python = env_dir / ("Scripts" if os.name == "nt" else "bin") / (
        "python.exe" if os.name == "nt" else "python")
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    (env_dir / "pyvenv.cfg").write_text("home = x\n", encoding="utf-8")
    (env_dir / ("Lib" if os.name == "nt" else "lib/python3.12") / "site-packages").mkdir(
        parents=True)
    uv = FakeUv()
    state = standalone(root, uv, environ)
    assert state["python"] == str(python) and state["problem"] is None
    assert state["env_dir"] == str(env_dir)
    assert [argv[1:3] for argv in uv.calls] == [["lock", "--check"], ["sync", "--active"]]
    assert all("--check" in argv for argv in uv.calls)


def test_no_environment_dependencies_says_could_not_check_not_missing():
    """With no interpreter, "websockets is missing" would be a guess. The item has to
    degrade to an explicit could-not-check or it sends the reader after the wrong fix."""
    check = preflight.check_dependencies(env_state(python_exists=False, ran=None,
                                                   modules=None, problem="uv was not found"))
    assert check["ok"] is None
    assert "could not check" in check["detail"]
    assert "interpreter" in check["detail"]
    # It must NOT claim anything about the packages themselves.
    assert "not installed" not in check["detail"]


def test_run_checks_on_an_empty_directory_never_raises_and_fails_first(tmp_path):
    checks = preflight.run_checks(str(tmp_path), probe_server=False,
                                  env={"APPDATA": str(tmp_path),
                                       "USERPROFILE": str(tmp_path)})
    assert [c["name"] for c in checks] == list(preflight.CHECKS)
    by_name = {c["name"]: c for c in checks}
    assert by_name["interpreter"]["ok"] is False
    assert by_name["dependencies"]["ok"] is None
    # probe_server=False: the network-backed items are "not checked", never "broken".
    for name in ("mcp_port", "server_process", "extension", "heartbeat"):
        assert by_name[name]["ok"] is None, name


# --- Scenario 2: a valid install with the extension disconnected --------------------

def test_extension_disconnected_is_a_named_condition_not_a_timeout():
    check = preflight.check_extension(discovery(health()))
    assert check["ok"] is False
    # Verbatim: this sentence is the acceptance criterion.
    assert check["detail"] == (
        "No browser extension is connected. The server is up on :2830 and the "
        "WebSocket is listening on :8772 — nothing has dialed it.")
    # The failure being fixed is that this used to surface as a transport
    # error ("socket closed unexpectedly") or a timeout. It must read as neither.
    assert "timeout" not in check["detail"].lower()
    assert "unexpectedly" not in check["detail"].lower()
    assert "popup" in check["remedy"] and "token" in check["remedy"]


def test_extension_connected_lists_the_browsers():
    check = preflight.check_extension(discovery(health(**connected_browser())))
    assert check["ok"] is True
    assert "Zen Browser" in check["detail"] and "1.7.6" in check["detail"]
    assert check["remedy"] is None


def test_server_up_with_no_extension_still_reports_the_server_itself():
    """The whole point: the server item answers even though the browser leg is dead."""
    check = preflight.check_server_process(discovery(health()))
    assert check["ok"] is True
    assert "pid 4242" in check["detail"] and "1.7.6" in check["detail"]
    assert "1h02m" in check["detail"]  # uptime_seconds=3725


def test_version_warning_is_carried_verbatim():
    warning = ("Browser Zen Browser (PID 38288) has a newer version (1.7.7) than the "
               "server (1.7.6), after finishing the current tasks notify the user")
    check = preflight.check_server_process(discovery(health(version_warning=warning)))
    assert warning in check["detail"]


# --- Scenario 3: a running server whose WebSocket has been severed ------------------

SEVERED = health(extension_instances=[{
    "slot": 1, "name": "Zen Browser 1.7.6", "friendly": "Zen Browser",
    "currently_connected": False, "last_connect_hms": "14:02:11", "connect_count": 3,
}])


def test_severed_websocket_is_distinct_from_never_connected():
    severed = preflight.check_extension(discovery(SEVERED))
    never = preflight.check_extension(discovery(health()))

    assert severed["ok"] is False and never["ok"] is False
    # Different cause => different words AND a different remedy. If these collapsed
    # into one string the reader would go set up an extension that is already set up.
    assert severed["detail"] != never["detail"]
    assert severed["remedy"] != never["remedy"]
    assert "REGISTERED" in severed["detail"] and "CLOSED" in severed["detail"]
    assert "14:02:11" in severed["detail"]          # names WHEN it was last alive
    assert "Zen Browser" in severed["detail"]       # names WHICH browser
    assert "reload" in severed["remedy"].lower()


def test_severed_websocket_still_reports_port_and_server_liveness():
    """The diagnosis must state that the server half is fine — otherwise the reader
    restarts the server, which is not the broken leg."""
    check = preflight.check_extension(discovery(SEVERED))
    assert ":2830" in check["detail"] and ":8772" in check["detail"]
    assert "not a transport bug" in check["detail"]


def test_heartbeat_flags_a_half_open_socket():
    stale = health(**connected_browser(age=252.0))
    check = preflight.check_heartbeat(discovery(stale))
    assert check["ok"] is False
    assert "silent for 4m12s" in check["detail"]
    assert "half-open" in check["detail"]
    assert check["remedy"]


def test_heartbeat_is_quiet_while_the_extension_is_probing():
    check = preflight.check_heartbeat(discovery(health(**connected_browser(age=3.0))))
    assert check["ok"] is True
    assert "3s ago" in check["detail"]


def test_heartbeat_without_an_activity_stamp_says_it_could_not_check():
    """An extension too old to report activity must not be accused of being silent."""
    payload = health(**connected_browser())
    payload["connections"] = [{"kind": "extension", "slot": 1}]  # no activity_epoch
    check = preflight.check_heartbeat(discovery(payload))
    assert check["ok"] is None
    assert "could not check" in check["detail"]


def test_heartbeat_skipped_when_no_extension_is_connected():
    check = preflight.check_heartbeat(discovery(health()))
    assert check["ok"] is None
    assert "no extension is connected" in check["detail"]


def test_heartbeat_age_uses_the_servers_own_clock():
    """generated_epoch vs activity_epoch: both server stamps. Reading the local clock
    instead would report a clock-skewed machine's healthy browser as silent for hours."""
    payload = health(generated_epoch=5_000_000.0)
    payload.update(connected_browser())
    payload["connections"] = [{"kind": "extension", "activity_epoch": 5_000_000.0 - 10}]
    check = preflight.check_heartbeat(discovery(payload), now=9_999_999_999.0)
    assert check["ok"] is True
    assert "10s ago" in check["detail"]


# --- The interpreter split that produced the original failure -----------------------

def test_unrunnable_interpreter_names_the_pyvenv_cfg_home_that_does_not_exist():
    state = env_state(ran=False, modules=None, cfg_home=r"C:\Users\other\Python314",
                       cfg_home_exists=False,
                       error="OSError: [WinError 193] not a valid Win32 application")
    check = preflight.check_interpreter(state)
    assert check["ok"] is False
    assert r"C:\Users\other\Python314" in check["detail"]
    assert "does not exist on this machine" in check["detail"]
    assert "built elsewhere" in check["detail"]
    assert "WinError 193" in check["detail"]        # the raw evidence, kept verbatim
    assert START in check["remedy"]


def test_a_venv_that_runs_is_not_failed_just_because_pyvenv_cfg_is_stale():
    """A uv trampoline's pyvenv.cfg can name an interpreter it does not use (the checkout's
    old .venv did). It WORKS, so calling it broken would be a false alarm — but the
    unportability is worth saying, so it lands in the detail, not in `ok`."""
    check = preflight.check_interpreter(env_state(cfg_home=r"C:\gone",
                                                   cfg_home_exists=False))
    assert check["ok"] is True
    assert "does not describe the interpreter it actually runs" in check["detail"]
    assert "another machine" in check["detail"]


def test_healthy_interpreter_reports_version_and_base():
    check = preflight.check_interpreter(env_state())
    assert check["ok"] is True
    assert "3.12.5" in check["detail"]
    assert check["remedy"] is None


def _found(env_dir, python):
    """An inspect_server_env stand-in that found `python` in `env_dir`."""
    return lambda root, environ=None: {"mode": "uv", "python": str(python),
                                       "env_dir": str(env_dir), "problem": None}


def test_collect_interpreter_survives_an_unrunnable_python_on_disk(tmp_path):
    """The real collector against the real failure shape: the exe exists (it was
    copied) and starting it is what fails."""
    env_dir = tmp_path / "env"
    python = env_dir / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.write_text("this is not an interpreter\n", encoding="utf-8")
    (env_dir / "pyvenv.cfg").write_text(
        "home = /nonexistent/python-from-another-box\nversion = 3.14.5\n",
        encoding="utf-8")

    state = preflight.collect_interpreter(str(tmp_path), timeout=10, in_process=False,
                                          inspect=_found(env_dir, python))
    assert state["python_exists"] is True
    assert state["ran"] is False
    assert state["error"]                                  # the spawn failure, verbatim
    assert state["cfg_home"] == "/nonexistent/python-from-another-box"
    assert state["cfg_home_exists"] is False

    check = preflight.check_interpreter(state)
    assert check["ok"] is False and check["remedy"]
    assert "python-from-another-box" in check["detail"]


def test_collect_interpreter_answers_in_process_for_the_running_server(tmp_path):
    """The fast path: asked about the interpreter that is asking, it does not spawn a
    copy of itself (which is what would make server_info pay for this on every call)."""
    state = preflight.collect_interpreter(str(tmp_path), in_process=True)
    assert state["in_process"] is True and state["ran"] is True
    assert state["mode"] == "in_process" and state["python"] == sys.executable
    assert state["runtime_version"].startswith(str(sys.version_info[0]))
    # Real answer, from this interpreter: pytest is importable here, "definitely_not_a_module" is not.
    probe = preflight.collect_interpreter(
        str(tmp_path), packages=("pytest", "definitely_not_a_module"), in_process=True)
    assert probe["modules"]["pytest"]
    assert probe["modules"]["definitely_not_a_module"] is None


def test_in_process_is_chosen_only_for_this_checkout_on_a_venv_with_the_packages(tmp_path):
    root = preflight.repo_root()
    here_is_a_venv = sys.prefix != sys.base_prefix
    assert preflight._answers_in_process(root, ("pytest",)) is here_is_a_venv
    assert preflight._answers_in_process(str(tmp_path), ("pytest",)) is False
    assert preflight._answers_in_process(root, ("definitely_not_a_module",)) is False


def test_the_server_info_path_runs_no_subprocess_and_asks_no_uv(monkeypatch):
    """server_info calls run_checks(probe_server=False) on its loop, on every call."""
    def boom(*a, **k):
        raise AssertionError("the in-process path must not spawn anything")
    monkeypatch.setattr(preflight.subprocess, "run", boom)
    monkeypatch.setattr(server_env, "inspect_server_env", boom)
    monkeypatch.setattr(preflight, "_answers_in_process", lambda root, packages: True)
    checks = {c["name"]: c for c in preflight.run_checks(probe_server=False)}
    assert "this is the interpreter running the check" in checks["interpreter"]["detail"]


def test_a_gutted_running_environment_is_a_failure_with_the_repair(monkeypatch):
    """`uv cache prune` under a running server: it keeps serving on what it imported and
    fails on the next lazy import. /health and server_info both say so."""
    monkeypatch.setattr(server_env, "running_env_damage",
                        lambda *a, **k: "this server runs from E, which has lost its pyvenv.cfg")
    state = preflight.collect_interpreter(in_process=True)
    check = preflight.check_interpreter(state)
    assert check["ok"] is False and "lost its pyvenv.cfg" in check["detail"]
    assert "control.py shutdown" in check["remedy"]


# --- Dependencies: the ModuleNotFoundError that never reaches the client ------------

def test_missing_websockets_names_the_error_the_client_never_sees():
    modules = {name: "1.0" for name in preflight.REQUIRED_PACKAGES}
    modules["websockets"] = None
    check = preflight.check_dependencies(env_state(modules=modules))
    assert check["ok"] is False
    assert "ModuleNotFoundError: websockets" in check["detail"]
    # Why the outside world sees something else entirely — the reason this was hard.
    assert "connect timeout" in check["detail"]
    assert START in check["remedy"]


def test_several_missing_packages_are_all_named():
    modules = {name: "1.0" for name in preflight.REQUIRED_PACKAGES}
    modules["websockets"] = modules["psutil"] = None
    check = preflight.check_dependencies(env_state(modules=modules))
    assert "websockets" in check["detail"] and "psutil" in check["detail"]


def test_dependencies_pass_lists_the_installed_versions():
    check = preflight.check_dependencies(env_state())
    assert check["ok"] is True
    for name in preflight.REQUIRED_PACKAGES:
        assert name in check["detail"]


def test_a_crashed_module_probe_degrades_instead_of_reporting_everything_missing():
    state = env_state(modules={"__probe_error__": "RecursionError()"})
    check = preflight.check_dependencies(state)
    assert check["ok"] is None
    assert "RecursionError" in check["detail"]


# --- The MCP port: found, absent, and the pin that hides a healthy server -----------

def test_mcp_port_found():
    check = preflight.check_mcp_port(discovery(health()))
    assert check["ok"] is True
    assert check["detail"] == "MCP server found on :2830"


def test_mcp_port_absent_names_the_whole_range_and_how_to_start_one():
    check = preflight.check_mcp_port(discovery(None))
    assert check["ok"] is False
    assert ":2830-:2942" in check["detail"]
    assert "BROWSERPLUG_MCP_PORT is unset" in check["detail"]
    assert "server/server.py" in check["remedy"]
    assert "startup.log" in check["remedy"]


def test_a_dead_pin_is_reported_distinctly_from_an_empty_range():
    """The 3456 failure .tmp/dev-docs/ports.md records: a pin survives every restart, so /health
    answers happily off-policy while every client times out. Reporting this as a plain
    "nothing is running" would send the reader to start a second server."""
    dead_pin = preflight.check_mcp_port(discovery(None, pinned=3456))
    empty = preflight.check_mcp_port(discovery(None))
    assert dead_pin["ok"] is False
    assert dead_pin["detail"] != empty["detail"]
    assert "3456" in dead_pin["detail"]
    assert "disables the seek" in dead_pin["detail"]
    assert "unset BROWSERPLUG_MCP_PORT" in dead_pin["remedy"]


def test_a_live_pin_says_so():
    check = preflight.check_mcp_port(discovery(health(), pinned=2830, port=2830))
    assert check["ok"] is True and "pinned by BROWSERPLUG_MCP_PORT=2830" in check["detail"]


def test_a_failed_probe_is_not_reported_as_an_absent_server():
    check = preflight.check_mcp_port(discovery(None, error="OSError: no resolver"))
    assert check["ok"] is None
    assert "could not check" in check["detail"] and "no resolver" in check["detail"]


# --- Client config: wrong vs. merely not reloaded -----------------------------------

def write_config(path, command, *, enabled=True, name="browserplug", key="mcp"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({key: {name: {"type": "local", "command": command,
                                             "enabled": enabled}}}), encoding="utf-8")


def configs_for(tmp_path, command, **kw):
    write_config(tmp_path / "kilo.json", command, **kw)
    return preflight.collect_client_configs(str(tmp_path),
                                            env={"APPDATA": str(tmp_path / "none"),
                                                 "USERPROFILE": str(tmp_path / "none")})


def test_config_pointing_at_a_path_that_does_not_exist_here(tmp_path):
    records = configs_for(tmp_path, [r"C:\build-box\.venv\Scripts\python.exe",
                                     r"C:\build-box\server\launcher.py"])
    check = preflight.check_client_config(records, discovery(health()))
    assert check["ok"] is False
    assert r"C:\build-box\.venv\Scripts\python.exe" in check["detail"]
    assert "does not exist on this machine" in check["detail"]
    # Name the error the CLIENT prints, because it names the command, not the path —
    # which is exactly why this took a session to find the first time.
    assert "ENOENT" in check["detail"]
    assert "RESTART" in check["remedy"] or "restart" in check["remedy"]


def test_the_repos_own_config_moved_with_the_repo_says_so(tmp_path):
    """kilo.json ships in this repo carrying absolute paths. Every checkout anywhere but
    the author's box therefore failed this check on every server_info with a message
    about a config "written on another box" — it was written on this one, at another
    path, and the fix is a prefix, not a rebuild."""
    (tmp_path / ".venv" / "Scripts").mkdir(parents=True)
    (tmp_path / ".venv" / "Scripts" / "python.exe").write_text("x", encoding="utf-8")
    (tmp_path / "server").mkdir()
    (tmp_path / "server" / "launcher.py").write_text("x", encoding="utf-8")
    records = configs_for(tmp_path, [r"V:\MCP\checkout\.venv\Scripts\python.exe",
                                     r"V:\MCP\checkout\server\launcher.py"])

    check = preflight.check_client_config(records, discovery(health()), str(tmp_path))
    assert check["ok"] is False, "for anyone who runs that client it is genuinely broken"
    assert r"written for a checkout at V:\MCP\checkout" in check["detail"]
    assert f"this one is at {tmp_path}" in check["detail"]
    assert "this repo's own sample file" in check["detail"]
    assert "replace the" in check["remedy"] and r"V:\MCP\checkout" in check["remedy"]


def test_a_config_outside_the_repo_keeps_the_written_elsewhere_message(tmp_path):
    """The relocation story is only true of a file this repo ships. A client's own
    registry under %APPDATA% pointing at a dead venv is the general case, and renaming
    it would send the reader after the wrong thing."""
    (tmp_path / "root").mkdir()
    (tmp_path / "root" / ".venv").mkdir()
    write_config(tmp_path / "elsewhere" / "kilo.json",
                 [r"C:\gone\.venv\python.exe", r"C:\gone\server\launcher.py"])
    records = preflight.collect_client_configs(
        str(tmp_path / "elsewhere"), env={"APPDATA": str(tmp_path / "x"),
                                          "USERPROFILE": str(tmp_path / "x")})
    check = preflight.check_client_config(records, discovery(health()),
                                          str(tmp_path / "root"))
    assert check["ok"] is False
    assert "does not exist on this machine" in check["detail"]
    assert "written for a checkout" not in check["detail"]


def test_a_single_missing_path_is_never_called_a_relocation(tmp_path):
    """One path proves nothing: its basename landing under this root is a coincidence
    away, and a coincidence here renames a real failure."""
    (tmp_path / "python.exe").write_text("x", encoding="utf-8")
    records = configs_for(tmp_path, [r"C:\gone\python.exe"])
    check = preflight.check_client_config(records, discovery(health()), str(tmp_path))
    assert "written for a checkout" not in check["detail"]


def test_config_written_correctly_but_never_loaded_is_not_a_misconfiguration(tmp_path):
    """The fourth condition: everything on disk is right, the server is up, and the
    client still reports "server not found" because it has not re-read the file."""
    good = configs_for(tmp_path, [sys.executable, str(tmp_path / "kilo.json")])
    not_loaded = preflight.check_client_config(good, discovery(health()))

    assert not_loaded["ok"] is True                      # nothing is MIS-configured
    assert "has not reloaded the config" in not_loaded["detail"]
    assert "an edit is not picked up live" in not_loaded["detail"]
    assert "restart the client" in not_loaded["detail"].lower()
    assert "nothing on the server side needs changing" in not_loaded["remedy"]

    # ... and it is structurally distinguishable from a config that IS wrong.
    wrong = preflight.check_client_config(
        configs_for(tmp_path, [r"C:\gone\python.exe"]), discovery(health()))
    assert wrong["ok"] is False
    assert wrong["detail"] != not_loaded["detail"]


def test_a_client_already_attached_suppresses_the_restart_hint(tmp_path):
    """If some client is talking to the server, the config is demonstrably being read;
    telling every reader to restart would be wallpaper."""
    attached = health(connections=[{"kind": "mcp_client", "friendly": "Claude Code",
                                    "pid": 999}], client_pids=[999])
    check = preflight.check_client_config(
        configs_for(tmp_path, [sys.executable]), discovery(attached))
    assert check["ok"] is True
    assert "has not reloaded" not in check["detail"]


def test_a_disabled_entry_is_reported_because_nothing_else_reports_it(tmp_path):
    records = configs_for(tmp_path, [sys.executable], enabled=False)
    check = preflight.check_client_config(records, discovery(health()))
    assert check["ok"] is False
    assert "enabled=false" in check["detail"]
    assert "enabled" in check["remedy"]


def test_an_unparseable_config_is_a_failure_with_the_parse_error(tmp_path):
    (tmp_path / "kilo.json").write_text('{"mcp": {"browserplug": {,}}}', encoding="utf-8")
    records = preflight.collect_client_configs(
        str(tmp_path), env={"APPDATA": str(tmp_path / "x"),
                            "USERPROFILE": str(tmp_path / "x")})
    check = preflight.check_client_config(records, discovery(health()))
    assert check["ok"] is False
    assert "does not parse" in check["detail"]
    assert "SILENTLY ignores" in check["detail"]
    assert check["remedy"]


def test_a_broken_config_is_reported_even_when_another_client_is_fine(tmp_path):
    """Each client keeps its OWN registry, so a healthy ~/.claude.json must not mask a
    kilo.json that the Kilo CLI cannot read."""
    (tmp_path / "kilo.json").write_text('﻿{"mcp": {"browserplug": {}}}',
                                         encoding="utf-8")   # BOM: json.load refuses
    (tmp_path / ".claude.json").write_text(json.dumps({"mcpServers": {"browserplug": {
        "command": sys.executable}}}), encoding="utf-8")
    records = preflight.collect_client_configs(
        str(tmp_path), env={"APPDATA": str(tmp_path / "x"),
                            "USERPROFILE": str(tmp_path)})
    assert any(r["declares_browserplug"] for r in records)        # the healthy one parsed
    check = preflight.check_client_config(records, discovery(health()))
    assert check["ok"] is False
    assert "kilo.json" in check["detail"] and "does not parse" in check["detail"]
    assert "byte-order mark" in check["remedy"]


def test_pyvenv_cfg_is_read_through_a_byte_order_mark(tmp_path):
    """A hand-edited pyvenv.cfg carries a BOM; without stripping it, `home` goes
    missing and the report loses the line that explains an environment built elsewhere."""
    (tmp_path / "env").mkdir()
    (tmp_path / "env" / "pyvenv.cfg").write_text(
        "﻿home = /gone/python314\nversion = 3.14.5\n", encoding="utf-8")
    state = preflight.collect_interpreter(
        str(tmp_path), in_process=False,
        inspect=_found(tmp_path / "env", tmp_path / "env" / "Scripts" / "python.exe"))
    assert state["cfg_home"] == "/gone/python314"
    assert state["cfg_home_exists"] is False


def test_a_config_with_no_browserplug_entry_at_all(tmp_path):
    write_config(tmp_path / "kilo.json", ["node", "other.js"], name="something-else")
    records = preflight.collect_client_configs(
        str(tmp_path), env={"APPDATA": str(tmp_path / "x"),
                            "USERPROFILE": str(tmp_path / "x")})
    check = preflight.check_client_config(records, discovery(health()))
    assert check["ok"] is False
    assert "NONE of them declares a browserplug server" in check["detail"]


def _venv_config(tmp_path, root, *, relative="kilo.json", body=None):
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    python = os.path.join(str(root), ".venv", "Scripts", "python.exe")
    launcher = os.path.join(str(root), "server", "launcher.py")
    path.write_text(body if body is not None else json.dumps(
        {"mcpServers": {"browserplug": {"command": python, "args": [launcher]}}}),
        encoding="utf-8")
    return path


def test_configs_still_naming_the_checkouts_venv_are_listed_while_it_exists(tmp_path):
    """The uv cutover deletes <root>/.venv; every client started from it must move first."""
    root = tmp_path / "root"
    (root / ".venv").mkdir(parents=True)
    _venv_config(tmp_path, root, relative=".claude.json")
    refs = preflight.collect_venv_references(str(root), env={"USERPROFILE": str(tmp_path),
                                                             "APPDATA": str(tmp_path / "x")})
    assert [r["client"] for r in refs] == ["Claude Code"] and refs[0]["lines"] == [1]
    check = preflight.check_client_config([], discovery(health()), str(root), refs)
    assert check["ok"] is True                    # it still works today
    assert "still start BrowserPlug from" in check["detail"] and ".claude.json" in check["detail"]
    assert f"uv run --script {os.path.join(str(root), 'server', 'launcher.py')}" \
        in check["remedy"]


def test_once_the_venv_is_gone_those_configs_fail(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    _venv_config(tmp_path, root, relative=".claude.json")
    env = {"USERPROFILE": str(tmp_path), "APPDATA": str(tmp_path / "x")}
    records = preflight.collect_client_configs(str(root), env=env)
    refs = preflight.collect_venv_references(str(root), env=env)
    check = preflight.check_client_config(records, discovery(health()), str(root), refs)
    assert check["ok"] is False
    assert "does not exist" in check["detail"] and "uv run --script" in check["remedy"]


def test_the_registration_remedy_names_uv_by_its_absolute_path(tmp_path):
    """A bare `uv` is resolved by the client, and a Windows client can resolve it to a
    uv.exe planted in the project directory it was opened in."""
    root = tmp_path / "root"
    root.mkdir()
    _venv_config(tmp_path, root, relative=".claude.json")
    env = {"USERPROFILE": str(tmp_path), "APPDATA": str(tmp_path / "x")}
    refs = preflight.collect_venv_references(str(root), env=env)
    uv = os.path.join(str(tmp_path), "bin", "uv.exe")
    launcher = os.path.join(str(root), "server", "launcher.py")
    check = preflight.check_client_config([], discovery(health()), str(root), refs, uv=uv)
    assert f"`{uv} run --script {launcher}`" in check["remedy"]
    assert f"command `{uv}`" in check["remedy"]
    bare = preflight.check_client_config([], discovery(health()), str(root), refs)
    assert "full path" in bare["remedy"]


def test_run_checks_hands_the_client_config_check_the_uv_it_finds(tmp_path, monkeypatch):
    seen = {}
    real = preflight.check_client_config

    def spy(*a, **kw):
        seen["uv"] = kw.get("uv")
        return real(*a, **kw)
    monkeypatch.setattr(preflight, "check_client_config", spy)
    monkeypatch.setattr(preflight.server_env, "find_uv", lambda environ=None: "C:\\x\\uv.exe")
    preflight.run_checks(str(tmp_path), probe_server=False,
                         env={"APPDATA": str(tmp_path), "USERPROFILE": str(tmp_path)})
    assert seen["uv"] == "C:\\x\\uv.exe"


def test_an_unregistered_client_is_told_the_absolute_uv_command(tmp_path):
    (tmp_path / ".claude.json").write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    records = preflight.collect_client_configs(
        str(tmp_path), env={"APPDATA": str(tmp_path / "x"), "USERPROFILE": str(tmp_path)})
    check = preflight.check_client_config(records, discovery(health()), str(tmp_path),
                                          uv="C:\\tools\\uv.exe")
    assert check["ok"] is False and "C:\\tools\\uv.exe run --script" in check["remedy"]


# --- How a client starts the launcher -------------------------------------------------------
# The interpreter form (uv's python runs launcher.py) is the one to register; the `uv run
# --script` form works and costs a resident uv.exe per client; a patch-pinned interpreter
# stops following `uv python upgrade`; a missing interpreter is a spawn failure that names
# the command, not the path.

LINK_PYTHON = os.path.join("uv", "python", "cpython-3.12-windows-x86_64-none", "python.exe")
PATCH_PYTHON = os.path.join("uv", "python", "cpython-3.12.14-windows-x86_64-none", "python.exe")


def _file(path, text="x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return str(path)


def _attached():
    """A /health with an MCP client attached, so the check reports the registration itself
    rather than the 'config written but never loaded' hint."""
    return discovery(health(client_pids=[999]))


def _registered(tmp_path, command):
    """(records, root, launcher path) for a kilo.json that starts `command(launcher)`."""
    root = tmp_path / "root"
    launcher = _file(root / "server" / "launcher.py")
    return configs_for(tmp_path, command(launcher)), str(root), launcher


def test_the_interpreter_registration_passes_without_a_note(tmp_path):
    python = _file(tmp_path / LINK_PYTHON)
    records, root, _launcher = _registered(tmp_path, lambda launcher: [python, launcher])
    check = preflight.check_client_config(records, _attached(), root, launcher=(python, None))
    assert check["ok"] is True
    assert "uv run" not in check["detail"] and "resident" not in check["detail"]
    assert not check["remedy"]


def test_the_uv_run_registration_passes_with_its_cost_and_the_replacement(tmp_path):
    python = _file(tmp_path / LINK_PYTHON)
    uv = _file(tmp_path / "bin" / "uv.exe")
    records, root, launcher = _registered(
        tmp_path, lambda launcher: [uv, "run", "--script", launcher])
    check = preflight.check_client_config(records, _attached(), root, uv=uv,
                                          launcher=(python, None))
    assert check["ok"] is True, "it works: only the cost is reported"
    assert "uv run --script" in check["detail"] and "uv.exe resident" in check["detail"]
    assert f"`{python} {launcher}`" in check["remedy"]
    assert "keeps working until then" in check["remedy"]


def test_the_uv_run_note_says_how_to_get_the_interpreter_when_uv_has_none(tmp_path):
    uv = _file(tmp_path / "bin" / "uv.exe")
    records, root, _launcher = _registered(
        tmp_path, lambda launcher: [uv, "run", "--script", launcher])
    check = preflight.check_client_config(
        records, _attached(), root, uv=uv,
        launcher=(None, "uv has no managed Python 3.12; run `uv python install 3.12` first"))
    assert check["ok"] is True
    assert "uv python install 3.12" in check["remedy"]


def test_a_patch_pinned_interpreter_passes_with_a_note(tmp_path):
    python = _file(tmp_path / PATCH_PYTHON)
    records, root, _launcher = _registered(tmp_path, lambda launcher: [python, launcher])
    check = preflight.check_client_config(records, _attached(), root, launcher=(python, None))
    assert check["ok"] is True
    assert "uv python upgrade" in check["detail"] and "minor-version link" in check["detail"]


def test_a_missing_minor_link_interpreter_fails_with_the_uv_remedy(tmp_path):
    python = str(tmp_path / LINK_PYTHON)                      # never created
    records, root, _launcher = _registered(tmp_path, lambda launcher: [python, launcher])
    check = preflight.check_client_config(records, _attached(), root)
    assert check["ok"] is False
    assert python in check["detail"] and "ENOENT" in check["detail"]
    assert "`uv python install 3.12`" in check["remedy"]
    assert "minor-version link" in check["remedy"] and "restart that client" in check["remedy"]


def test_a_missing_patch_directory_interpreter_says_to_name_the_link(tmp_path):
    python = str(tmp_path / PATCH_PYTHON)
    records, root, _launcher = _registered(tmp_path, lambda launcher: [python, launcher])
    check = preflight.check_client_config(records, _attached(), root)
    assert check["ok"] is False
    assert "3.12.14" in check["remedy"]
    assert "uv python find --managed-python 3.12" in check["remedy"]


def test_a_missing_path_that_is_not_uvs_keeps_the_edit_remedy(tmp_path):
    records, root, _launcher = _registered(
        tmp_path, lambda launcher: [str(tmp_path / "gone" / "python.exe"), launcher])
    check = preflight.check_client_config(records, _attached(), root)
    assert check["ok"] is False
    assert "so every absolute path resolves here" in check["remedy"]
    assert "uv python install" not in check["remedy"]

# The launcher interpreter may be uv's minor-version link for ANY minor inside the server's
# requires-python. Read from the REAL server/server.py block (>=3.11,<3.13), so the floor
# alone cannot pass: a 3.12 link is fine, a 3.13 one is outside the range.

REAL_SERVER = os.path.join(os.path.dirname(__file__), "..", "..", "server", "server.py")
LINK_313 = os.path.join("uv", "python", "cpython-3.13-windows-x86_64-none", "python.exe")


def _registered_in_real_range(tmp_path, python):
    records, root, launcher = _registered(tmp_path, lambda launcher: [python, launcher])
    shutil.copyfile(REAL_SERVER, os.path.join(root, "server", "server.py"))
    return records, root


def test_a_3_12_link_registration_passes_while_the_floor_is_3_11(tmp_path):
    python = _file(tmp_path / LINK_PYTHON)
    records, root = _registered_in_real_range(tmp_path, python)
    assert server_env.server_python_minor(root) == "3.11"
    check = preflight.check_client_config(records, _attached(), root, launcher=(python, None))
    assert check["ok"] is True
    assert "outside" not in check["detail"] and not check["remedy"]


def test_a_3_13_link_registration_is_flagged_as_out_of_range(tmp_path):
    python = _file(tmp_path / LINK_313)
    good = _file(tmp_path / LINK_PYTHON)
    records, root = _registered_in_real_range(tmp_path, python)
    check = preflight.check_client_config(records, _attached(), root, launcher=(good, None))
    assert "outside the server's requires-python range (3.11 to 3.12)" in check["detail"]
    assert good in check["remedy"] and "restart each client" in check["remedy"]
    # With no interpreter to name, the remedy still names the minor it recommends.
    bare = preflight.check_client_config(
        records, _attached(), root,
        launcher=(None, "uv has no managed Python 3.12 or 3.11; run `uv python install 3.12` "
                        "first"))
    assert "uv python install 3.12" in bare["remedy"]
    bare = preflight.check_client_config(records, _attached(), root)
    assert "uv python install 3.12" in bare["remedy"]


def test_a_missing_out_of_range_link_recommends_the_highest_minor_in_range(tmp_path):
    python = str(tmp_path / LINK_313)                        # never created
    records, root = _registered_in_real_range(tmp_path, python)
    check = preflight.check_client_config(records, _attached(), root)
    assert check["ok"] is False
    assert "`uv python install 3.12`" in check["remedy"]
    assert "install 3.13" not in check["remedy"]


def test_a_missing_in_range_link_names_its_own_minor(tmp_path):
    python = str(tmp_path / "uv" / "python" / "cpython-3.11-windows-x86_64-none" / "python.exe")
    records, root = _registered_in_real_range(tmp_path, python)
    check = preflight.check_client_config(records, _attached(), root)
    assert "`uv python install 3.11`" in check["remedy"]


@pytest.mark.parametrize("version,form", [
    ("3.11", "interpreter"), ("3.12", "interpreter"), ("3.13", "interpreter_range"),
    ("3.10", "interpreter_range"),
])
def test_the_launch_form_flags_a_minor_outside_the_range(version, form):
    parts = [f"C:/uv/python/cpython-{version}-windows-x86_64-none/python.exe",
             "C:/r/server/launcher.py"]
    assert preflight._launch_form(parts, ["3.11", "3.12"]) == form
    assert preflight._launch_form(parts) == "interpreter", "no range known: no flag"
    patch = [f"C:/uv/python/cpython-{version}.1-windows-x86_64-none/python.exe",
             "C:/r/server/launcher.py"]
    assert preflight._launch_form(patch, ["3.11", "3.12"]) == "interpreter_patch"


@pytest.mark.parametrize("parts,form", [
    (["C:/uv/python/cpython-3.12-windows-x86_64-none/python.exe", "C:/r/server/launcher.py"],
     "interpreter"),
    (["C:/uv/python/cpython-3.12.14-windows-x86_64-none/python.exe", "C:/r/server/launcher.py"],
     "interpreter_patch"),
    (["/usr/bin/python3.12", "/r/launcher/launcher.py"], "interpreter"),
    (["C:/bin/uv.exe", "run", "--script", "C:/r/server/launcher.py"], "uv_run"),
    (["uv", "run", "--script", "/r/server/launcher.py"], "uv_run"),
    (["node", "C:/r/server/launcher.py"], "other"),
    (["C:/uv/python/cpython-3.12-windows-x86_64-none/python.exe", "C:/r/server/other.py"],
     "other"),
    ([], "other"),
])
def test_the_launch_form_is_told_from_the_command(parts, form):
    assert preflight._launch_form(parts) == form


def test_the_registration_text_names_the_interpreter_when_there_is_one(tmp_path):
    root, uv = str(tmp_path / "root"), os.path.join(str(tmp_path), "bin", "uv.exe")
    launcher = os.path.join(root, "server", "launcher.py")
    python = os.path.join(str(tmp_path), LINK_PYTHON)
    text = preflight._registration(root, uv, (python, None))
    assert f"`{python} {launcher}`" in text and "run --script" not in text
    fallback = preflight._registration(root, uv, (None, "run `uv python install 3.12` first"))
    assert f"`{uv} run --script {launcher}`" in fallback
    assert "uv python install 3.12" in fallback
    # The venv route needs no uv, so it is named beside every uv-only form.
    assert server_env.SERVER_PYTHON_ENV in fallback
    assert server_env.SERVER_PYTHON_ENV not in text


# --- The TOML configs (Codex, Mistral Vibe, Reasonix) -----------------------------------------

# Codex keeps one table per server; Mistral Vibe and Reasonix keep arrays of tables (the two
# shapes cannot share a file, so there is a text for each).
TOML_CODEX = """\
model = "x"

[mcp_servers.livebrowser]
command = 'C:\\uv\\python.exe'
args = ['C:\\r\\server\\launcher.py']

[mcp_servers.other]
command = "npx"
args = ["-y", "thing"]
"""
TOML_ARRAYS = """\
[[mcp_servers]]
name = "legacy-browser"
command = "C:/uv/python.exe"
args = [
  "C:/r/server/launcher.py",  # a trailing comma and a comment
]

[[plugins]]
name = "browserplug"
command = "uv"
args = ["run", "--script", "C:/r/launcher.py"]
"""


def test_the_toml_configs_are_parsed_and_checked_like_the_json_ones(tmp_path):
    """Codex's `livebrowser` and Vibe's `legacy-browser` say neither "browserplug": they are found by
    the launcher path they run, and a missing interpreter in them fails like any other."""
    home, root = tmp_path / "home", tmp_path / "root"
    launcher = _file(root / "server" / "launcher.py")
    gone = str(tmp_path / "gone" / "python.exe")
    _file(home / ".codex" / "config.toml",
          f"[mcp_servers.livebrowser]\ncommand = '{gone}'\nargs = ['{launcher}']\n\n"
          f"[mcp_servers.other]\ncommand = 'npx'\n")
    _file(home / ".vibe" / "config.toml",
          f'[[mcp_servers]]\nname = "legacy-browser"\ncommand = "{gone.replace(chr(92), "/")}"\n'
          f'args = ["{launcher.replace(chr(92), "/")}"]\n')
    records = preflight.collect_client_configs(
        str(root), env={"USERPROFILE": str(home), "APPDATA": str(tmp_path / "x")})
    assert sorted(r["client"] for r in records) == ["Codex CLI", "Mistral Vibe"]
    assert all(r["declares_browserplug"] and len(r["entries"]) == 1 for r in records)
    check = preflight.check_client_config(records, _attached(), str(root))
    assert check["ok"] is False
    assert "does not exist on this machine" in check["detail"]
    assert "config.toml" in check["detail"]


def test_a_toml_config_with_no_browserplug_entry_is_not_reported(tmp_path):
    home = tmp_path / "home"
    _file(home / ".codex" / "config.toml", "[mcp_servers.github]\ncommand = 'npx'\n")
    _file(home / ".vibe" / "config.toml", "[[mcp_servers]]\nname = 'other'\ncommand = 'x'\n")
    assert preflight.collect_client_configs(
        str(tmp_path / "root"), env={"USERPROFILE": str(home),
                                     "APPDATA": str(tmp_path / "x")}) == []


def test_a_good_toml_registration_counts_as_declared(tmp_path):
    home, root = tmp_path / "home", tmp_path / "root"
    launcher = _file(root / "server" / "launcher.py")
    python = _file(tmp_path / LINK_PYTHON)
    _file(home / ".codex" / "config.toml",
          f"[mcp_servers.livebrowser]\ncommand = '{python}'\nargs = ['{launcher}']\n")
    records = preflight.collect_client_configs(
        str(root), env={"USERPROFILE": str(home), "APPDATA": str(tmp_path / "x")})
    check = preflight.check_client_config(records, _attached(), str(root),
                                          launcher=(python, None))
    assert check["ok"] is True and "Codex CLI" in check["detail"]
    assert "uv run" not in check["detail"]


@pytest.mark.parametrize("text,count", [(TOML_CODEX, 2), (TOML_ARRAYS, 2)],
                         ids=["tables", "arrays"])
def test_the_line_reader_agrees_with_tomllib_on_the_server_tables(text, count):
    """Python 3.10 has no tomllib, so the same file is read line by line there."""
    import tomllib
    tomllib.loads(text)                                           # valid TOML, or the test lies

    def shape(tables):
        return sorted((key, str(name), entry.get("command"), entry.get("args"))
                      for name, entry, key in tables)
    by_line = shape(preflight._toml_tables_by_line(text))
    assert by_line == shape(preflight._toml_server_tables(text))
    assert len(by_line) == count


def test_the_launcher_interpreter_is_asked_of_uv_only_by_the_standalone_doctor(
        tmp_path, monkeypatch):
    """server_info and /doctor run in the server, where a uv process on every call would
    block the loop; the CLI may ask."""
    asked = []
    monkeypatch.setattr(preflight.server_env, "launcher_python",
                        lambda root, **kw: asked.append(kw["ask_uv"]) or (None, "why"))
    monkeypatch.setattr(preflight, "collect_client_configs", lambda *a, **k: [])
    monkeypatch.setattr(preflight, "collect_venv_references", lambda *a, **k: [])
    env = {"APPDATA": str(tmp_path), "USERPROFILE": str(tmp_path)}
    monkeypatch.setattr(preflight, "_answers_in_process", lambda root, packages: False)
    preflight.run_checks(str(tmp_path), probe_server=False, env=env)
    monkeypatch.setattr(preflight, "_answers_in_process", lambda root, packages: True)
    preflight.run_checks(str(tmp_path), probe_server=False, env=env)
    assert asked == [True, False]


def test_a_working_interpreter_registration_does_not_start_the_lookup(tmp_path, monkeypatch):
    python = _file(tmp_path / LINK_PYTHON)
    records, root, _launcher = _registered(tmp_path, lambda launcher: [python, launcher])
    monkeypatch.setattr(preflight.server_env, "launcher_python",
                        lambda *a, **k: pytest.fail("nothing needs a registration text"))
    monkeypatch.setattr(preflight, "collect_client_configs", lambda *a, **k: records)
    monkeypatch.setattr(preflight, "collect_venv_references", lambda *a, **k: [])
    preflight.run_checks(root, probe_server=False,
                         env={"APPDATA": str(tmp_path), "USERPROFILE": str(tmp_path)})


def test_the_toml_configs_are_scanned_as_text(tmp_path):
    """Codex and Vibe keep TOML; nothing parses them, so they are read for the path."""
    root = tmp_path / "root"
    (root / ".venv").mkdir(parents=True)
    python = os.path.join(str(root), ".venv", "Scripts", "python.exe")
    _venv_config(tmp_path, root, relative=os.path.join(".codex", "config.toml"),
                 body=f"[mcp_servers.livebrowser]\ncommand = '{python}'\n")
    refs = preflight.collect_venv_references(str(root), env={"USERPROFILE": str(tmp_path)})
    assert [(r["client"], r["lines"]) for r in refs] == [("Codex CLI", [2])]


def test_a_relative_venv_path_counts_only_in_a_file_the_checkout_ships(tmp_path):
    """reasonix.toml names `.venv/Scripts/python.exe` relative to the checkout."""
    root = tmp_path / "root"
    (root / ".venv").mkdir(parents=True)
    (root / "reasonix.toml").write_text('command = ".venv/Scripts/python.exe"\n',
                                        encoding="utf-8")
    elsewhere = tmp_path / "home" / ".vibe" / "config.toml"
    elsewhere.parent.mkdir(parents=True)
    elsewhere.write_text('command = ".venv/Scripts/python.exe"\n', encoding="utf-8")
    refs = preflight.collect_venv_references(str(root),
                                             env={"USERPROFILE": str(tmp_path / "home")})
    assert [r["client"] for r in refs] == ["Reasonix"]


def test_a_config_that_moved_to_uv_is_not_a_venv_reference(tmp_path):
    root = tmp_path / "root"
    (root / ".venv").mkdir(parents=True)
    launcher = os.path.join(str(root), "server", "launcher.py")
    _venv_config(tmp_path, root, relative=".claude.json", body=json.dumps(
        {"mcpServers": {"browserplug": {"command": "uv", "args": ["run", "--script", launcher]}}}))
    assert preflight.collect_venv_references(str(root), env={"USERPROFILE": str(tmp_path)}) == []


def test_no_config_anywhere_is_could_not_check_not_a_failure(tmp_path):
    check = preflight.check_client_config([], discovery(health()))
    assert check["ok"] is None
    assert "each MCP client has its OWN" in check["detail"]


def test_relative_commands_are_not_reported_as_missing_paths(tmp_path):
    """`python` / `npx` resolve through PATH at spawn time; calling them missing files
    would be a fabricated failure."""
    records = configs_for(tmp_path, ["python", "-m", "server.launcher"])
    entry = records[0]["entries"][0]
    assert entry["missing_paths"] == []


def test_claude_code_style_command_plus_args_is_parsed(tmp_path):
    (tmp_path / ".claude.json").write_text(json.dumps({"mcpServers": {"browserplug": {
        "type": "stdio", "command": r"C:\gone\python.exe",
        "args": [r"C:\gone\server\launcher.py"]}}}), encoding="utf-8")
    records = preflight.collect_client_configs(
        str(tmp_path), env={"APPDATA": str(tmp_path / "x"),
                            "USERPROFILE": str(tmp_path)})
    entry = records[0]["entries"][0]
    assert entry["missing_paths"] == [r"C:\gone\python.exe", r"C:\gone\server\launcher.py"]


# --- The invariants that make this a diagnostic rather than a second failure --------

ALL_SCENARIOS = [
    ("no server env", lambda: [preflight.check_interpreter(env_state(
        python_exists=False, problem="the server environment has not been built yet"))]),
    ("no uv", lambda: [preflight.check_interpreter(env_state(
        python_exists=False, problem="uv was not found", remedy="install uv (https://docs."
                                                               "astral.sh/uv/)"))]),
    ("gutted server env", lambda: [preflight.check_interpreter(env_state(
        damaged="this server runs from E, which has lost its pyvenv.cfg"))]),
    ("missing dep", lambda: [preflight.check_dependencies(
        env_state(modules={**{n: "1" for n in preflight.REQUIRED_PACKAGES},
                            "websockets": None}))]),
    ("no server", lambda: [preflight.check_mcp_port(discovery(None)),
                           preflight.check_server_process(discovery(None))]),
    ("dead pin", lambda: [preflight.check_mcp_port(discovery(None, pinned=3456))]),
    ("no extension", lambda: [preflight.check_extension(discovery(health()))]),
    ("severed ws", lambda: [preflight.check_extension(discovery(SEVERED))]),
    ("stale heartbeat", lambda: [preflight.check_heartbeat(
        discovery(health(**connected_browser(age=600))))]),
]


@pytest.mark.parametrize("label,build", ALL_SCENARIOS, ids=[s[0] for s in ALL_SCENARIOS])
def test_every_failing_check_names_a_remedy(label, build):
    for check in build():
        if check["ok"] is False:
            assert check["remedy"], f"{label}/{check['name']} failed with no remedy"
            assert len(check["remedy"]) > 20, f"{label}: remedy is not actionable"
            assert check["detail"], f"{label}: no detail"


def test_checks_constant_matches_what_run_checks_emits(tmp_path):
    checks = preflight.run_checks(str(tmp_path), probe_server=False,
                                  env={"APPDATA": str(tmp_path),
                                       "USERPROFILE": str(tmp_path)})
    assert tuple(c["name"] for c in checks) == preflight.CHECKS
    for check in checks:
        assert set(check) == {"name", "ok", "detail", "remedy"}
        assert check["ok"] in (True, False, None)


def test_probe_server_false_touches_no_socket(tmp_path, monkeypatch):
    """The MCP surface calls it this way from inside the server. If it probed, an
    already-degraded server would be diagnosing itself over its own transport — the
    exact circularity this module exists to break."""
    def boom(*a, **kw):
        raise AssertionError("preflight made a network call with probe_server=False")

    monkeypatch.setattr(preflight.mcp_port, "seek_browserplug", boom)
    monkeypatch.setattr(preflight.mcp_port, "browserplug_health", boom)
    monkeypatch.setattr(preflight.mcp_port, "port_is_bindable", boom)
    monkeypatch.setattr(socket, "socket", boom)
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    monkeypatch.setattr(urllib.request, "build_opener", boom)

    checks = preflight.run_checks(str(tmp_path), probe_server=False,
                                  env={"APPDATA": str(tmp_path),
                                       "USERPROFILE": str(tmp_path)})
    assert len(checks) == len(preflight.CHECKS)


def test_a_collector_that_explodes_becomes_could_not_check(monkeypatch):
    monkeypatch.setattr(preflight.mcp_port, "seek_browserplug",
                        lambda *a, **kw: (_ for _ in ()).throw(OSError("resolver gone")))
    state = preflight.collect_server(probe=True, env={})
    assert state["health"] is None and "resolver gone" in state["error"]
    assert preflight.check_mcp_port(state)["ok"] is None


def test_checks_never_raise_on_a_garbage_health_payload():
    for junk in (None, "", [], {"browsers": "nope", "connections": 7}):
        state = {"probed": True, "pinned": None, "port": 2830, "health": junk,
                 "error": None}
        for fn in (preflight.check_server_process, preflight.check_extension,
                   preflight.check_heartbeat):
            check = fn(state)
            assert check["ok"] in (True, False, None)


def test_injected_health_answers_the_server_items_without_a_probe(tmp_path, monkeypatch):
    """How both in-process surfaces run. A server probing its own HTTP port to find out
    whether it is up is the circularity that made the old diagnostic go silent."""
    def boom(*a, **kw):
        raise AssertionError("injected health must not trigger a probe")

    monkeypatch.setattr(preflight.mcp_port, "seek_browserplug", boom)
    monkeypatch.setattr(preflight.mcp_port, "browserplug_health", boom)

    checks = {c["name"]: c for c in preflight.run_checks(
        str(tmp_path), health=health(), env={"APPDATA": str(tmp_path),
                                             "USERPROFILE": str(tmp_path)})}
    assert checks["mcp_port"]["ok"] is True          # taken from health.mcp_port
    assert checks["server_process"]["ok"] is True
    assert checks["extension"]["ok"] is False        # ... and the browser leg is honest


# --- GET /doctor: the surface that answers while the WebSocket is dead --------------

class FakeServer:
    def __init__(self, payload=None, raises=False):
        self.mcp_port = 2830
        self._payload = payload
        self._raises = raises

    def get_health_info(self):
        if self._raises:
            raise RuntimeError("psutil exploded")
        return self._payload


async def doctor(server, client=("127.0.0.1", 5555)):
    sent = []

    async def send(message):
        sent.append(message)

    await preflight.handle_doctor_request({"type": "http", "method": "GET",
                                           "path": "/doctor", "client": client},
                                          send, server)
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, json.loads(body.decode("utf-8"))


@pytest.mark.asyncio
async def test_doctor_answers_while_the_extension_leg_is_dead():
    status, payload = await doctor(FakeServer(health()))
    assert status == 200
    assert payload["service"] == "browserplug"
    assert payload["ok"] is False
    names = {c["name"] for c in payload["checks"]}
    assert names == set(preflight.CHECKS)
    extension = next(c for c in payload["checks"] if c["name"] == "extension")
    assert extension["ok"] is False and extension["remedy"]
    assert "BrowserPlug preflight:" in payload["summary"]


@pytest.mark.asyncio
async def test_doctor_is_ok_when_a_browser_is_attached():
    status, payload = await doctor(FakeServer(health(**connected_browser())))
    assert status == 200 and payload["ok"] is True
    assert payload["connections"]["browsers"]


@pytest.mark.asyncio
async def test_doctor_reports_a_server_that_cannot_describe_itself():
    """A get_health_info() that raises is a finding, not a 500 — the caller is asking
    what is wrong, so "I could not read my own state" is the useful answer."""
    status, payload = await doctor(FakeServer(raises=True))
    assert status == 200
    assert payload["checks"][0]["ok"] is None
    assert "psutil exploded" in payload["checks"][0]["detail"]
    assert payload["connections"] is None


@pytest.mark.asyncio
async def test_doctor_is_localhost_only():
    status, payload = await doctor(FakeServer(health()), client=("10.0.0.9", 5555))
    assert status == 403 and payload == {"error": "localhost only"}


# --- Rendering ----------------------------------------------------------------------

def test_render_lists_every_check_and_indents_the_remedies():
    checks = [preflight.check_interpreter(env_state()),
              preflight.check_extension(discovery(health()))]
    text = preflight.render(checks, root=r"C:\dr\browserplug")
    assert "BrowserPlug preflight: 1 ok, 1 FAILED" in text
    assert r"C:\dr\browserplug" in text
    # The verdict points at the first failure — later items are usually its shadow.
    assert "Fix the FIRST failure listed (extension)" in text
    assert "-> " in text and "popup" in text
    for check in checks:
        assert check["name"] in text


def test_render_of_a_healthy_install_has_no_remedy_lines():
    checks = [preflight.check_interpreter(env_state()),
              preflight.check_dependencies(env_state()),
              preflight.check_mcp_port(discovery(health()))]
    text = preflight.render(checks)
    assert text.startswith("BrowserPlug preflight: all 3 checks passed.")
    assert "-> " not in text
    assert "FAIL" not in text


def test_render_counts_not_checked_separately_from_failed():
    checks = [preflight.check_extension(discovery(None, probed=False)),
              preflight.check_extension(discovery(health()))]
    assert "0 ok, 1 FAILED, 1 not checked" in preflight.render(checks)


def test_render_survives_an_empty_list():
    assert preflight.render([]) == "BrowserPlug preflight: no checks ran."


def test_verdict_is_the_headline_only():
    line = preflight.verdict([preflight.check_extension(discovery(health()))])
    assert "\n" not in line and "FAILED" in line


# --- summarize_connections: the compact server_info view ----------------------------

FULL_INFO = {
    "count": 3, "generated_hms": "14:05:00", "generated_epoch": 1_000_000.0,
    "version_warning": "Browser Zen Browser (PID 1) has a newer version (1.7.7)",
    "connections": [
        {"kind": "server", "label": "BrowserPlug server", "pid": 100, "version": "1.7.6",
         "detail": "ws://localhost:8772  +  http://localhost:2830/mcp"},
        {"kind": "extension", "label": "Zen Browser", "slot": 1, "pid": 4012,
         "is_remote": False, "activity_hms": "14:04:59", "activity_epoch": 999_999.0},
        {"kind": "mcp_client", "friendly": "Claude Code", "pid": 123,
         "cwd": r"C:\dr\browserplug", "launcher_pid": 456, "session_count": 17,
         "activity_hms": "14:04:58",
         "sessions": [{"id": f"s{i}", "last_activity_hms": "14:04:58",
                       "commands": [{"time_hms": "14:04:58", "tool": "page_read",
                                     "params": "url=https://example.org/very/long/path"}
                                    for _ in range(10)]} for i in range(17)]},
    ],
    "extension_instances": [{"slot": 1, "version": "1.7.6", "windows": 2, "tabs": 31}],
}


def test_summarize_drops_the_session_command_replay():
    summary = reports.summarize_connections(FULL_INFO)
    blob = json.dumps(summary)
    assert "page_read" not in blob and "commands" not in blob
    # The popup's 2s-poll payload is not agent context: measured ~9.4 KB, of which the
    # sessions array alone is ~4.2 KB.
    assert len(blob) < len(json.dumps(FULL_INFO)) / 4


def test_summarize_keeps_what_an_agent_can_act_on():
    summary = reports.summarize_connections(FULL_INFO)
    assert summary["server"]["pid"] == 100
    assert summary["browsers"][0]["slot"] == 1
    assert summary["browsers"][0]["extension_version"] == "1.7.6"
    assert summary["browsers"][0]["tabs"] == 31
    client = summary["mcp_clients"][0]
    assert (client["friendly"], client["pid"], client["sessions"]) == ("Claude Code",
                                                                      123, 17)
    assert client["cwd"] == r"C:\dr\browserplug"
    assert "1.7.7" in summary["version_warning"]
    assert "verbose" in summary["note"]


def test_summarize_never_raises_on_a_partial_or_wrong_payload():
    assert reports.summarize_connections({})["browsers"] == []
    assert reports.summarize_connections({"connections": [None, 7, {}]})["mcp_clients"] == []
    assert "error" in reports.summarize_connections(None)
    assert "error" in reports.summarize_connections("nope")


# --- The CLI: the one surface that answers when the server process is gone ----------

def test_cli_text_mode_reports_and_exits_nonzero_on_a_failure(no_server_env, capsys):
    code = preflight.main(["--no-probe", "--root", str(no_server_env)])
    out = capsys.readouterr().out
    assert code == 1
    assert out.startswith("BrowserPlug preflight:")
    assert "interpreter" in out and "https://docs.astral.sh/uv/" in out


def test_cli_json_mode_is_machine_readable(no_server_env, capsys):
    tmp_path = no_server_env
    code = preflight.main(["--json", "--no-probe", "--root", str(tmp_path)])
    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert [c["name"] for c in payload] == list(preflight.CHECKS)
    assert all({"name", "ok", "detail", "remedy"} == set(c) for c in payload)


def test_cli_rejects_unknown_arguments(tmp_path, capsys):
    assert preflight.main(["--wat"]) == 2
    assert preflight.main(["--root"]) == 2
    assert "usage:" in capsys.readouterr().err


def test_cli_exit_code_is_zero_when_nothing_failed(monkeypatch, capsys):
    monkeypatch.setattr(preflight, "run_checks",
                        lambda *a, **kw: [preflight._check("interpreter", True, "fine"),
                                          preflight._check("extension", None, "skipped")])
    assert preflight.main(["--no-probe"]) == 0
    assert "not checked" in capsys.readouterr().out


def test_module_imports_nothing_that_could_be_missing():
    """The module diagnoses a venv whose dependencies are absent, so it must not need
    any of them. Guard against a future import creeping in — statements only, so the
    docstring may keep naming the packages it refuses to import."""
    with open(preflight.__file__, "r", encoding="utf-8") as fh:
        statements = [line.strip() for line in fh
                      if line.strip().startswith(("import ", "from "))]
    for statement in statements:
        for forbidden in ("websockets", "fastmcp", "uvicorn", "psutil", "pydantic",
                          "cryptography", "server.server", "mcp_tools"):
            assert forbidden not in statement, \
                f"preflight.py must not import {forbidden}: {statement!r}"
        assert not re.match(r"(from|import)\s+mcp(\.|\s|$)", statement), statement
        assert not re.match(r"(from|import)\s+mcp_types\b", statement), statement


def test_the_cli_runs_on_a_bare_interpreter_with_none_of_the_dependencies(tmp_path):
    """The doctor's whole point: it answers when the dependencies are what is missing.
    Run on the base interpreter, which has none of them."""
    base = os.path.join(sys.base_prefix, "python.exe" if os.name == "nt" else "bin/python3")
    env = {k: v for k, v in os.environ.items()
           if k.upper() not in ("PYTHONPATH", "VIRTUAL_ENV", server_env.SERVER_PYTHON_ENV)}
    env.update(USERPROFILE=str(tmp_path), HOME=str(tmp_path), APPDATA=str(tmp_path))
    proc = subprocess.run([base, os.path.join(preflight.repo_root(), "server", "preflight.py"),
                           "--no-probe", "--json", "--root", str(tmp_path)],
                          capture_output=True, text=True, timeout=120, env=env,
                          stdin=subprocess.DEVNULL)
    assert proc.returncode in (0, 1), proc.stderr
    assert [c["name"] for c in json.loads(proc.stdout)] == list(preflight.CHECKS)
