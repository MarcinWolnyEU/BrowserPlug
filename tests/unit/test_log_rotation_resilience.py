"""Reproduction + fix for the log-rotation death spiral that wedged the shared server.

Incident 2026-07-17: the shared server (an OLD instance) had both ports LISTENING but
/health TIMED OUT — its event loop was wedged — and no extension could connect. Root
cause: `browserplug-server.log` had grown to 37 MB against a 2 MB cap because a SECOND server
process held the same file open, so the primary's RotatingFileHandler could not rename on
rollover (Windows `WinError 32`). The stdlib handler then retries the rollover on EVERY
subsequent emit; each failure runs handleError (a ~30-line traceback), and when the child's
stderr is redirected back into the same file (a separate, already-fixed spawn bug) the file
grows without bound and the emit work starves the loop.

The layered fix:
  1. detached children write stderr to a .startup.log sibling, not the log file
     (test_detached_logging.py) — removes the growth amplifier.
  2. THIS: SafeRotatingFileHandler. The base rename is probed first; if it is blocked
     nothing else is touched (backups and live log survive), writing continues to the
     base and the rename is retried after a backoff, so logging NEVER spirals or wedges
     and history is never truncated away (review L7).

Approach: reproduce the stdlib spiral, then prove the fix does not spiral.
"""

import logging
import os
import sys
from logging.handlers import RotatingFileHandler


import test_imports  # noqa: F401
from server.runtime.log_handler import SafeRotatingFileHandler


def _blocked_rename(*_a, **_k):
    # Exactly what Windows raises when another process holds the target file open.
    raise PermissionError(32, "The process cannot access the file because it is being "
                              "used by another process")


def _drive(handler, monkeypatch, n=600, msg_len=100):
    """Log n records through `handler` with os.rename blocked; count handleError calls."""
    logger = logging.getLogger(f"rot_{id(handler)}")
    logger.propagate = False
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    errors = []
    monkeypatch.setattr(handler, "handleError", lambda record: errors.append(record))
    monkeypatch.setattr(os, "rename", _blocked_rename)
    try:
        for i in range(n):
            logger.info("%s %04d", "x" * msg_len, i)
        handler.flush()
    finally:
        logger.removeHandler(handler)
    return errors


# --- the bug, reproduced with the stdlib handler -----------------------------------

def test_stdlib_rotating_handler_spirals_when_rename_is_blocked(tmp_path, monkeypatch):
    """Demonstrates the defect: the stdlib handler retries the doomed rollover on every
    emit (flooding handleError) and DROPS the records whose emit triggered a failed
    rollover — i.e. logging both spams and silently loses data once the file is over cap
    and the rename is blocked."""
    log = tmp_path / "srv.log"
    handler = RotatingFileHandler(str(log), maxBytes=1000, backupCount=2, encoding="utf-8")
    try:
        errors = _drive(handler, monkeypatch)
        # Flood: nearly every emit past the cap hit a failed rollover.
        assert len(errors) > 100, f"expected a handleError flood, got {len(errors)}"
        # Data loss: the last record never reached the file (its emit was skipped).
        assert "0599" not in log.read_text(encoding="utf-8", errors="ignore")
    finally:
        handler.close()


# --- the fix -----------------------------------------------------------------------

def test_safe_handler_does_not_spiral_when_rename_is_blocked(tmp_path, monkeypatch):
    """SafeRotatingFileHandler: a blocked rename must NOT flood handleError, must keep
    logging (no dropped records), and must not retry the rename on every record.

    Changed deliberately (L7): the old version also asserted the file stayed under 6 KB,
    which was only true because the fallback truncated the live log and destroyed history.
    The file now keeps growing until the blocker lets go; the backoff is what stops the
    retry flood.
    """
    log = tmp_path / "srv.log"
    handler = SafeRotatingFileHandler(str(log), maxBytes=1000, backupCount=2, encoding="utf-8")
    attempts = []
    real = _blocked_rename
    monkeypatch.setattr(sys.modules[__name__], "_blocked_rename",
                        lambda *a, **k: (attempts.append(a), real(*a, **k)))
    try:
        errors = _drive(handler, monkeypatch)

        # No flood: rollover failure is handled cleanly, not surfaced as an error per emit.
        assert errors == [], f"the failure flooded handleError {len(errors)} times"

        text = log.read_text(encoding="utf-8", errors="ignore")
        # Logging kept working: the LAST record and the FIRST are present.
        assert "0599" in text, "records were dropped — logging did not survive the failure"
        assert "0000" in text, "the start of the log was truncated away"
        # Backoff: ~60 KB of records past the cap made a handful of rename attempts,
        # not one per record.
        assert len(attempts) <= 3, f"{len(attempts)} rename attempts, expected backoff"
    finally:
        handler.close()


def test_blocked_rename_keeps_backups_and_live_log(tmp_path, monkeypatch):
    """L7: a blocked base rename used to run the stdlib cascade (.1 -> .2, .1 deleted) and
    then truncate the base, so both the live log and the .1 history were lost."""
    log = tmp_path / "srv.log"
    b1, b2 = tmp_path / "srv.log.1", tmp_path / "srv.log.2"
    b1.write_text("BACKUP-ONE", encoding="utf-8")
    b2.write_text("BACKUP-TWO", encoding="utf-8")
    log.write_text("LIVE-HISTORY", encoding="utf-8")
    handler = SafeRotatingFileHandler(str(log), maxBytes=200, backupCount=2, encoding="utf-8")
    _drive(handler, monkeypatch, n=50)
    handler.close()
    assert b1.read_text(encoding="utf-8") == "BACKUP-ONE"
    assert b2.read_text(encoding="utf-8") == "BACKUP-TWO"
    text = log.read_text(encoding="utf-8")
    assert text.startswith("LIVE-HISTORY"), "the live log was truncated"
    assert "0049" in text


def test_rotation_resumes_after_blocker_releases(tmp_path, monkeypatch):
    """Once the rename works again (backoff elapsed), the handler rotates normally and
    the cascade preserves older backups."""
    import server.runtime.log_handler as lh
    log = tmp_path / "srv.log"
    handler = SafeRotatingFileHandler(str(log), maxBytes=500, backupCount=2, encoding="utf-8")
    logger = logging.getLogger("rot_resume")
    logger.propagate = False
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    clock = [1000.0]
    monkeypatch.setattr(lh.time, "monotonic", lambda: clock[0])
    real_rename = os.rename
    monkeypatch.setattr(os, "rename", _blocked_rename)
    try:
        for i in range(20):
            logger.info("%s %04d", "z" * 100, i)
        assert not os.path.exists(str(log) + ".1")
        monkeypatch.setattr(os, "rename", real_rename)
        clock[0] += 10_000  # past any backoff
        for i in range(20, 25):
            logger.info("%s %04d", "z" * 100, i)
        handler.flush()
        assert os.path.exists(str(log) + ".1"), "rotation did not resume"
        # The pre-block history moved down the cascade, it was not dropped.
        kept = "".join(open(f"{log}.{n}", encoding="utf-8").read() for n in (1, 2)
                       if os.path.exists(f"{log}.{n}"))
        assert "0000" in kept
    finally:
        logger.removeHandler(handler)
        handler.close()


def test_safe_handler_rotates_normally_when_rename_works(tmp_path):
    """When nothing blocks it, the safe handler rotates exactly like the stdlib one —
    the degradation path must not change the happy path."""
    log = tmp_path / "srv.log"
    handler = SafeRotatingFileHandler(str(log), maxBytes=1000, backupCount=2, encoding="utf-8")
    logger = logging.getLogger("rot_happy")
    logger.propagate = False
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    try:
        for i in range(300):
            logger.info("%s %04d", "y" * 100, i)
        handler.flush()
        assert os.path.exists(str(log) + ".1"), "a normal rollover must produce a .1 backup"
        assert log.stat().st_size < 2000, "the live file must be bounded by maxBytes"
    finally:
        logger.removeHandler(handler)
        handler.close()
