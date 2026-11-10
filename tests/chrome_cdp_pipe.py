"""Load an unpacked extension into a branded Chrome over the DevTools pipe.

Branded Chrome (Canary, Beta, Stable) ignores --load-extension and
--disable-extensions-except ("is not allowed in Google Chrome, ignoring"), so the flag
route the harness uses for Chrome for Testing loads nothing. The supported route is the
DevTools `Extensions.loadUnpacked` method, which Chrome serves only on a
--remote-debugging-pipe connection with --enable-unsafe-extension-debugging.

The pipe is two file descriptors the browser inherits as 3 (it reads) and 4 (it writes),
carrying JSON messages that end in a NUL byte. Chrome exits when the pipe closes, so a
test keeps the parent's ends open for as long as the browser runs. POSIX only: the
descriptors are mapped with a shell redirection, not preexec_fn, which is unsafe in a
process that already runs threads.
"""

import json
import os
import select
import subprocess
import time

PIPE_FLAGS = ['--remote-debugging-pipe', '--enable-unsafe-extension-debugging']


class DevToolsPipe:
    """The parent's two ends of one browser's DevTools pipe."""

    def __init__(self):
        self._to_browser = None
        self._from_browser = None
        self._buffer = b''
        self._next_id = 0

    def popen(self, flags, **kwargs):
        """Start `flags` (browser path first) with the pipe on its fds 3 and 4."""
        browser_reads, self._to_browser = os.pipe()
        self._from_browser, browser_writes = os.pipe()
        script = 'exec "$0" "$@" 3<&%d 4>&%d' % (browser_reads, browser_writes)
        try:
            return subprocess.Popen(['/bin/sh', '-c', script, *flags],
                                    pass_fds=(browser_reads, browser_writes), **kwargs)
        except BaseException:
            self.close()
            raise
        finally:
            os.close(browser_reads)
            os.close(browser_writes)

    def call(self, method, params=None, timeout=30.0):
        """Send one command and return its `result`; RuntimeError on an error reply or silence."""
        self._next_id += 1
        want = self._next_id
        message = {'id': want, 'method': method, 'params': params or {}}
        os.write(self._to_browser, json.dumps(message).encode() + b'\0')
        deadline = time.monotonic() + timeout
        while True:
            reply = self._read_message(deadline)
            if reply.get('id') != want:
                continue   # an event, or the answer to something else
            if 'error' in reply:
                raise RuntimeError(f'{method} failed: {reply["error"]}')
            return reply.get('result', {})

    def _read_message(self, deadline):
        while b'\0' not in self._buffer:
            left = deadline - time.monotonic()
            if left <= 0 or not select.select([self._from_browser], [], [], left)[0]:
                raise RuntimeError('the browser did not answer on the DevTools pipe in time')
            chunk = os.read(self._from_browser, 65536)
            if not chunk:
                raise RuntimeError('the browser closed the DevTools pipe')
            self._buffer += chunk
        raw, _, self._buffer = self._buffer.partition(b'\0')
        return json.loads(raw)

    def close(self):
        for name in ('_to_browser', '_from_browser'):
            fd = getattr(self, name)
            setattr(self, name, None)
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
