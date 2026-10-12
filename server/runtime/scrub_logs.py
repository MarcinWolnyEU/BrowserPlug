"""One-shot: redact first-run token banners from BrowserPlug/LiveBrowser server logs.

Older servers logged the whole "generated a WebSocket token" banner at WARNING, and the
banner carried a working token (the master, later a device token). This rewrites the log
files in place, replacing only the token line; everything else is kept byte for byte.

    python server/runtime/scrub_logs.py [DIR]      # default: the repo root

Stdlib only. A file another process holds open for writing is skipped with a note.
"""
import glob
import os
import re
import sys

BANNER = "generated a WebSocket token for this server"
# The token sits alone on an indented line, a few lines below the banner start.
TOKEN_LINE = re.compile(r"^(\s{4,})[A-Za-z0-9_.\-]{20,}(\s*)$")
WINDOW = 14
PATTERNS = ("browserplug-server.log*", "livebrowser-server.log*")


def scrub_text(text):
    """(new_text, redacted_count). Line endings are preserved."""
    lines = text.splitlines(keepends=True)
    left, count = 0, 0
    for i, line in enumerate(lines):
        if BANNER in line:
            left = WINDOW
            continue
        if left:
            left -= 1
            body = line.rstrip("\r\n")
            m = TOKEN_LINE.match(body)
            if m:
                lines[i] = m.group(1) + "[token redacted]" + line[len(body):]
                count += 1
                left = 0
    return "".join(lines), count


def scrub_file(path):
    with open(path, "r", encoding="utf-8", errors="surrogateescape", newline="") as fh:
        text = fh.read()
    new, count = scrub_text(text)
    if count:
        with open(path, "w", encoding="utf-8", errors="surrogateescape", newline="") as fh:
            fh.write(new)
    return count


def main(argv):
    here = os.path.dirname(os.path.abspath(__file__))
    root = argv[1] if len(argv) > 1 else os.path.dirname(os.path.dirname(here))
    total = 0
    for pattern in PATTERNS:
        for path in sorted(glob.glob(os.path.join(root, pattern))):
            try:
                n = scrub_file(path)
            except OSError as e:
                print("skipped %s (%s)" % (path, e))
                continue
            total += n
            print("%s: %d redacted" % (os.path.basename(path), n))
    print("done, %d token line(s) redacted" % total)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
