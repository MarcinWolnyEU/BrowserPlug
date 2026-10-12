#!/usr/bin/env python3
"""BrowserPlug's server-level self-description, sent once at the initialize handshake.

Agent-experience feedback, 2026-08-03. The server initialised with a name and nothing
else. The complete self-description available to a connected client was the tool names
plus their parameter docs: the same channel one client truncates to ~50 characters and
another re-transmits 780 times.

What per-tool descriptions structurally CANNOT carry is CROSS-TOOL WORKFLOW: "for a
multi-page harvest do this", "to debug authentication start at requests, not page_eval",
"a verification call after a successful page_act is usually redundant". No single tool's
description is the right place to say any of it, and the corpus shows the cost: an agent
installed BrowserPlug from a local checkout, with 86 KB of documentation on disk feet away, and
still never learned what `requests` did, because once connected the only thing it read was
the tool list.

Two constraints shaped this text, both from the same feedback:

1. It is delivered ONCE at initialize, not on every tools/list, so it is the right home
   for guidance that would be unaffordable per-tool. It is still kept short: the tool
   description block measured 13,240 bytes re-sent 780 times, and the lesson there was
   that MORE prose is not what changes behaviour.

2. It says only what the measurements support. The same corpus showed that advocacy at
   PLAN time loses to model priors formed on other browser-automation APIs: page_eval's own
   "(escape hatch, prefer page_act/page_read/page_snapshot)" was ignored 143 times. So
   this text does not campaign. It states the few cross-tool facts an agent cannot derive
   from any single tool description, and leaves the steering to the responses, which is
   the channel that works.

Kept in its own module so the wording can be reviewed and unit-pinned without opening the
2,500-line tool file, and so its byte cost stays visible.
"""

import re

from server.browsers import default_browser

# The canonical repository, and the homepage both manifests carry. It has to resolve: it is
# what the Chrome Web Store and AMO listings point at, and it is where PRIVACY.md is served
# from. README.md, package.json and both manifests are reconciled to match this one.
REPOSITORY_URL = "https://github.com/MarcinWolnyEU/browserplug"

# The pin is the only setting that names a browser in advance. Under "first_started" no
# browser is named until one connects, so the text says that instead of guessing.
UNPINNED_BROWSER = "the first one to connect"
# Last resort, for a pin with neither a stored name nor a program to point at: a REMOTE
# browser has no local executable, so there is nothing left to name it by.
PINNED_WITHOUT_NAME = "the one pinned in the server's settings"

# The stored label can end in the process id the browser had when it was pinned
# ("Firefox Developer Edition pid 10648"; ExtBrowser.name's shape). That process is
# usually gone by the time anyone reads this text, and the id tells an agent nothing, so
# it is stripped.
_PID_SUFFIX = re.compile(r"\s+pid\s+\d+\s*$", re.IGNORECASE)

# Claude Code shows a server's instructions only up to 2,048 characters and drops the rest
# (seen 2026-10-08: the 3,026-character text stopped mid-sentence in an agent's context).
# tests/unit/test_mcp_server.py keeps the text under that, with room for a 60-character
# browser name.
CLIENT_SHOWN_CHARS = 2048


def instructions() -> str:
    """The server-level instructions string handed to the MCP server at construction.

    Built per call rather than at import: the default browser comes from config.json,
    which is not readable while this module is being imported. The server takes the text
    once, so a pin changed later is picked up by the next restart, not by the next
    client session.
    """
    return f"""\
BrowserPlug drives the user's REAL, logged-in browser (their profile, sessions and
cookies), so pages that turn away a signed-out browser load here. Several browsers can be
connected at once. The default is {default_browser_name()}.

- Open your own tab with tabs(action="open", background=true, url="https://…") and pass
  its tab_id. A page_* tool without tab_id acts on the tab the USER is looking at;
  navigating it away is the mistake they notice.
- Tab ids and page_snapshot refs belong to ONE browser. With several connected, a
  browsers tool appears.
- navigate(tab_id=N, read="markdown", url="https://…") returns the page IN THE SAME
  CALL, as does page_read(url=...).
- For a repeated listing (product grid, search results), page_snapshot(mode="records")
  gives one row per card with its own title, link and price. Zipping querySelectorAll
  arrays by index mismatches prices and titles.
- To see what a page SENDS (auth headers, cookies, the XHR behind a login), use
  requests. document.cookie misses HttpOnly cookies without saying so.
- page_eval is the escape hatch, not a faster page_snapshot.
- Results say when they are incomplete: "continue with offset=", TRUNCATED, "no content
  extracted", BROWSER BUSY.
- A navigation that lands at a DIFFERENT url, or drops a query parameter, says so.
  Believe that over the page body.
- A captcha or bot check is the user's to solve: stop and ask; never script it.
- page_act reports what each step CHANGED; a probe to check a click usually repeats it.
- Failures set the MCP error flag. BROWSER BUSY means nothing was sent, so the identical
  call is safe to re-issue.
- Calls to one browser queue fairly: fanning out adds latency, not failures. Retrying
  into a full queue or raising timeout_ms makes it worse; server_info shows the depth
  and limit.

Docs: {REPOSITORY_URL}
Diagnostics: server_info. If nothing answers, `python server/preflight.py` in the
checkout reports each precondition with its remedy.\
"""


def _executable_filename(exe_path) -> str:
    """The program's file name out of a stored path: "firefox.exe".

    Split on both separators rather than with os.path.basename: the path was written by
    whichever machine pinned the browser, and POSIX does not read a backslash as one, so
    a Windows path would come back whole.
    """
    if not isinstance(exe_path, str):
        return ""
    return re.split(r"[\\/]", exe_path.strip())[-1].strip()


def default_browser_name(settings=None) -> str:
    """What the instructions should call the default browser.

    The stored name is the picker's own label ("Zen Browser", not "Zen Browser pid
    38288"), and it survives that browser being closed, which is the point of the
    snapshot default_browser keeps. A pin saved without one falls back to the program it
    runs ("firefox.exe"), which the same snapshot carries for auto_launch. A settings
    file that cannot be read falls back to the unpinned wording instead of raising: this
    runs while the server is starting.
    """
    try:
        settings = default_browser.read() if settings is None else settings
        if settings.get("mode") == default_browser.MODE_PINNED:
            name = _PID_SUFFIX.sub("", (settings.get("name") or "")).strip()
            return (name or _executable_filename(settings.get("exe_path"))
                    or PINNED_WITHOUT_NAME)
    except Exception:
        return UNPINNED_BROWSER
    return UNPINNED_BROWSER
