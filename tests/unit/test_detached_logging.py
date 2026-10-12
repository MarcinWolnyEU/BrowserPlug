"""A detached child's stderr must never be pointed at BROWSERPLUG_LOG_FILE.

The server logs through a RotatingFileHandler(maxBytes=2_000_000) on BROWSERPLUG_LOG_FILE.
Both spawners (launcher.spawn_detached_server and server.runtime.lifecycle.spawn_restart_process) hand
the detached child stdout/stderr so a crash BEFORE logging is configured is not
traceless. If that target is the log file itself, the child holds a SECOND, life-long
handle to the path its own handler must rename at rollover -- on Windows the rename
then fails forever (WinError 32), and each failed emit dumps a ~30-line handler
traceback to stderr, i.e. back into the same file.

This is not theoretical. When these tests were written the repo's own logs were:
    browserplug-server.log        37.7 MB   (cap: 2 MB)   13,289 WinError-32 blocks
    C:/dr/.launchers/browserplug.log.1      16.4 MB        5,321 PermissionError blocks
Rotation had been dead for the entire life of the /restart path, and every unit test
passed the whole time -- nothing asserted the one property that mattered.

So: assert the property (rotation survives a concurrent holder of the child's stderr
target), not the shape of the code.
"""

import logging
import os
import subprocess
import sys
from logging.handlers import RotatingFileHandler
from unittest.mock import patch

import pytest

import test_imports  # noqa: F401
from launcher import launcher
from browserplug_shared.mcp_port import (DETACHED_ENV, LOG_FILE_ENV, default_log_path,
                                         resolve_log_path, startup_log_path)


def test_unset_log_file_still_resolves_to_a_real_file():
    """An unset BROWSERPLUG_LOG_FILE must not mean "log nowhere".

    /restart copies the environment verbatim, so a server started without the variable
    used to strand its whole lineage with no rotating handler; every record then went to
    the unrotated .startup.log capture instead. Caught live 2026-08-05.
    """
    assert resolve_log_path({}) == default_log_path()
    assert resolve_log_path({LOG_FILE_ENV: ""}) == default_log_path()
    assert resolve_log_path({LOG_FILE_ENV: "   "}) == default_log_path()
    assert default_log_path().endswith(".log")


def test_an_explicit_log_file_still_wins():
    assert resolve_log_path({LOG_FILE_ENV: "C:/tmp/custom.log"}) == "C:/tmp/custom.log"
    # Surrounding whitespace is stripped, not treated as part of the filename.
    assert resolve_log_path({LOG_FILE_ENV: " C:/tmp/custom.log "}) == "C:/tmp/custom.log"


def test_launcher_hands_the_child_the_same_default_the_server_would_pick():
    """The spawner and the server must never name different files, which is why the
    default lives in mcp_port.py alongside the port policy."""
    fake = _FakePopen()
    env_without = {k: v for k, v in os.environ.items() if k != LOG_FILE_ENV}
    with patch.dict(os.environ, env_without, clear=True), \
            patch.object(subprocess, "Popen", fake):
        launcher.spawn_detached_server()

    assert fake.kwargs is not None, "spawn never called Popen"
    handed = fake.kwargs["env"][LOG_FILE_ENV]
    stdout = fake.kwargs["stdout"]
    if stdout is not subprocess.DEVNULL:
        stdout.close()
    # The child is told a real path, and it is the one the server resolves for itself.
    assert handed == resolve_log_path({})
    assert handed == default_log_path()


def test_startup_log_path_is_a_sibling_never_the_log_itself():
    log = "C:/dr/browserplug/browserplug-server.log"
    assert startup_log_path(log) != log
    assert startup_log_path(log).endswith(".startup.log")
    # A path with no .log suffix must still not collide with itself.
    assert startup_log_path("/tmp/browserplug") != "/tmp/browserplug"


class _FakePopen:
    """Captures the kwargs a spawner would hand subprocess.Popen."""

    def __init__(self):
        self.kwargs = None
        self.args = None

    def __call__(self, args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        return object()


def test_launcher_does_not_point_the_child_at_the_rotating_log_file(tmp_path):
    log_file = tmp_path / "browserplug-server.log"
    fake = _FakePopen()
    with patch.dict(os.environ, {"BROWSERPLUG_LOG_FILE": str(log_file)}, clear=False), \
         patch.object(subprocess, "Popen", fake):
        launcher.spawn_detached_server()

    assert fake.kwargs is not None, "spawn never called Popen"
    stdout, stderr = fake.kwargs["stdout"], fake.kwargs["stderr"]
    # Not DEVNULL: a pre-logging crash must land somewhere.
    assert stdout is not subprocess.DEVNULL, "a crash before logging would be traceless"
    assert stderr is stdout
    written_to = os.path.abspath(stdout.name)
    stdout.close()

    assert written_to != os.path.abspath(str(log_file)), (
        "the child's stderr is the rotating handler's own file — rollover will fail "
        "forever (WinError 32) and the log grows unbounded")
    # Per-spawn name (launcher pid + timestamp) so concurrent launchers cannot truncate
    # each other's capture; still a `.startup.log` sibling of the log it stands for.
    assert os.path.dirname(written_to) == os.path.dirname(os.path.abspath(str(log_file)))
    assert os.path.basename(written_to).startswith("browserplug-server.")
    assert written_to.endswith(".startup.log")
    # And the child must be told to stop echoing to stderr once its handler is up,
    # or the startup file grows without bound and every line is logged twice.
    assert fake.kwargs["env"].get(DETACHED_ENV) == "1"


def test_launcher_starts_each_spawn_with_a_fresh_capture_file(tmp_path):
    """It holds ONE server's startup, not history: each spawn gets its own new file, so
    earlier output is neither reused nor overwritten (and concurrent launchers cannot
    truncate each other, which the old shared name with "w" allowed)."""
    log_file = tmp_path / "browserplug-server.log"
    startup = tmp_path / "browserplug-server.startup.log"
    startup.write_text("output from a previous server\n", encoding="utf-8")

    fake = _FakePopen()
    with patch.dict(os.environ, {"BROWSERPLUG_LOG_FILE": str(log_file)}, clear=False), \
         patch.object(subprocess, "Popen", fake):
        launcher.spawn_detached_server()
    new = fake.kwargs["stdout"]
    new.close()

    assert os.path.abspath(new.name) != os.path.abspath(str(startup))
    assert open(new.name, encoding="utf-8").read() == ""
    assert "previous server" in startup.read_text(encoding="utf-8")


def test_launcher_does_not_retain_the_childs_handle(tmp_path):
    """The parent must not keep the file open for its whole life (the child has its own)."""
    log_file = tmp_path / "browserplug-server.log"
    fake = _FakePopen()
    with patch.dict(os.environ, {"BROWSERPLUG_LOG_FILE": str(log_file)}, clear=False), \
         patch.object(subprocess, "Popen", fake):
        launcher.spawn_detached_server()

    assert fake.kwargs["stdout"].closed, "launcher leaked its handle on the startup file"


def test_spawn_survives_an_unwritable_startup_path(tmp_path):
    """An unwritable target must degrade to DEVNULL, never take the spawn down."""
    log_file = tmp_path / "nonexistent-dir" / "browserplug-server.log"  # parent does not exist
    fake = _FakePopen()
    with patch.dict(os.environ, {"BROWSERPLUG_LOG_FILE": str(log_file)}, clear=False), \
         patch.object(subprocess, "Popen", fake):
        launcher.spawn_detached_server()

    assert fake.kwargs["stdout"] is subprocess.DEVNULL
    assert fake.args is not None, "the server must still be spawned"


@pytest.mark.skipif(os.name != "nt", reason="the rename-while-open failure is Windows-specific")
def test_rotation_still_works_while_the_startup_file_is_held_open(tmp_path):
    """THE property: a second process holding the child's stderr target must not
    prevent the log file from rotating.

    Reproduces the real topology — one writer rotating BROWSERPLUG_LOG_FILE, another
    holding the spawner's capture file open — and asserts rotation completes. Point
    both at the same path (the old behaviour) and this fails with WinError 32, which
    is exactly what production was doing 13,289 times.
    """
    log_file = tmp_path / "browserplug-server.log"
    held_path = startup_log_path(str(log_file))

    handler = RotatingFileHandler(str(log_file), maxBytes=1000, backupCount=1,
                                  encoding="utf-8")
    log = logging.getLogger("test_rotation_isolated")
    log.propagate = False
    log.setLevel(logging.INFO)
    log.addHandler(handler)

    held = open(held_path, "w", buffering=1)  # stands in for the detached child
    try:
        for i in range(200):
            held.write(f"child stderr line {i}\n")
            log.info("x" * 100)  # forces several rollovers past maxBytes=1000
        handler.flush()

        assert os.path.exists(str(log_file) + ".1"), "the log never rotated"
        assert os.path.getsize(log_file) < 5000, (
            f"log is {os.path.getsize(log_file)} bytes against a 1000-byte cap — "
            "rotation is jammed (this is the 37 MB bug)")
    finally:
        log.removeHandler(handler)
        handler.close()
        held.close()


# --- the other half: the child must stop echoing to stderr ---------------------
# Pointing the child's stderr at a separate file only bounds it if the child stops
# writing every log record there. lifecycle.setup_logging's basicConfig(stream=sys.stderr) means it
# does by default -- so a detached child would stream its ENTIRE log into the startup
# file (twice: once bare via stderr, once formatted via the handler). DETACHED_ENV is
# how the spawner says "your stderr is a file, the handler has it covered".

_PROBE = r"""
import json, logging, os, sys
sys.path[:0] = [{root!r}, {shared_py!r}]
from server.runtime import lifecycle
lifecycle.setup_logging()
root = logging.getLogger()
print(json.dumps({{
    "stderr_handlers": sum(
        1 for h in root.handlers
        if isinstance(h, logging.StreamHandler)
        and not isinstance(h, logging.FileHandler)
        and getattr(h, "stream", None) is sys.stderr),
    "file_handlers": sum(1 for h in root.handlers if isinstance(h, logging.FileHandler)),
}}))
"""


def _probe_handlers(tmp_path, detached):
    root = os.path.abspath(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".."))
    env = dict(os.environ)
    env["BROWSERPLUG_LOG_FILE"] = str(tmp_path / "probe.log")
    env.pop(DETACHED_ENV, None)
    if detached:
        env[DETACHED_ENV] = "1"
    out = subprocess.run(
        [sys.executable, "-c", _PROBE.format(root=root, shared_py=os.path.join(root, "shared-py"))],
        capture_output=True, text=True, env=env, timeout=60)
    assert out.returncode == 0, out.stderr
    import json as _json
    return _json.loads(out.stdout.strip().splitlines()[-1])


def test_detached_child_stops_echoing_records_to_stderr(tmp_path):
    """Its stderr is the startup FILE — echoing there duplicates the whole log into it."""
    r = _probe_handlers(tmp_path, detached=True)
    assert r["file_handlers"] >= 1, "the rotating file handler must still be installed"
    assert r["stderr_handlers"] == 0, (
        "a detached child still echoes every record to stderr, i.e. into the startup "
        "file — which then grows unbounded and duplicates every line")


def test_an_interactive_run_keeps_its_console_output(tmp_path):
    """Only a spawner sets DETACHED_ENV. A human running the server by hand (which
    AGENTS.md documents WITH BROWSERPLUG_LOG_FILE set) must still see logs on the console."""
    r = _probe_handlers(tmp_path, detached=False)
    assert r["stderr_handlers"] == 1, "an interactive run lost its console logging"
    assert r["file_handlers"] >= 1
