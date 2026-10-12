"""A RotatingFileHandler that cannot wedge, spiral or eat history when its rename is blocked.

Why this exists (incident 2026-07-17): the shared server's `browserplug-server.log` reached
37 MB against a 2 MB cap and the server's event loop wedged: ports listening, /health
timing out, no extension able to connect. A SECOND server process was holding the same
log file open, so the primary's stdlib `RotatingFileHandler` could not rename the file on
rollover (Windows `WinError 32: being used by another process`). The stdlib handler retries
the doomed rollover on EVERY subsequent emit; each failure runs handleError (a ~30-line
traceback to stderr), and once stderr is redirected back into the same file the file grows
without bound and the per-emit work starves the loop.

The first fix truncated the handler's own stream when the rename failed. That bounded the
file but destroyed history (review L7): the stdlib cascade had already moved `.1` to `.2`
and deleted the old `.2` before the base rename failed, and the truncation then wiped the
live log too.

So the base rename is the FIRST step here, as a probe to a temporary name. If it fails,
nothing else has been touched: keep appending to the base and try again after a backoff
that doubles up to a cap. The file may outgrow its cap while a blocker lives; that is the
price of keeping history, and the backoff keeps it from costing CPU. Only after the probe
succeeds does the backup cascade run. It is stdlib-only so it can be imported and tested in
isolation.
"""

import logging.handlers
import os
import time

_BACKOFF_START = 30.0
_BACKOFF_CAP = 900.0


class SafeRotatingFileHandler(logging.handlers.RotatingFileHandler):
    _next_attempt = 0.0
    _backoff = _BACKOFF_START

    def shouldRollover(self, record):
        if time.monotonic() < self._next_attempt:
            return False
        return super().shouldRollover(record)

    def doRollover(self):
        if self.backupCount <= 0:
            return super().doRollover()
        base = self.baseFilename
        probe = base + ".rotating"
        if self.stream:
            self.stream.close()
            self.stream = None
        try:
            # Probe. Also the step that moves the live log out of the way, so a failure
            # here leaves every backup and the live log exactly as they were.
            if os.path.exists(probe):
                os.remove(probe)
            os.rename(base, probe)
        except OSError:
            self._defer()
            return
        try:
            self._cascade(probe)
        except OSError:
            # Backups blocked midway: put the live log back rather than lose it.
            self._restore(probe, base)
            self._defer()
            return
        finally:
            if not self.delay and self.stream is None:
                self._reopen()
        self._backoff = _BACKOFF_START
        self._next_attempt = 0.0

    def _cascade(self, probe):
        base = self.baseFilename
        for i in range(self.backupCount - 1, 0, -1):
            src, dst = f"{base}.{i}", f"{base}.{i + 1}"
            if os.path.exists(src):
                if os.path.exists(dst):
                    os.remove(dst)
                os.rename(src, dst)
        dst = base + ".1"
        if os.path.exists(dst):
            os.remove(dst)
        os.rename(probe, dst)

    def _restore(self, probe, base):
        # Never overwrite a base that a writer recreated meanwhile; the probe then stays
        # as `.rotating` for a human to find.
        try:
            if os.path.exists(probe) and not os.path.exists(base):
                os.rename(probe, base)
        except OSError:
            pass

    def _defer(self):
        self._next_attempt = time.monotonic() + self._backoff
        self._backoff = min(self._backoff * 2, _BACKOFF_CAP)
        # Never raise out of logging: keep writing to the base (append mode).
        self._reopen()

    def _reopen(self):
        try:
            self.stream = self._open()
        except OSError:
            self.stream = None
