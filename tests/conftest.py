"""
Pytest configuration and fixtures
"""

import os as _os
import sys as _sys
import tempfile as _tempfile

# The repo root (the `server` and `launcher` packages) and shared-py/ (the folder holding the
# one package `browserplug_shared`) are importable whatever PYTHONPATH says; the entry scripts
# do the same from __file__.
_REPO_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
for _p in (_REPO_ROOT, _os.path.join(_REPO_ROOT, "shared-py")):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)

# A server a test spawns inherits BROWSERPLUG_LOG_FILE, and its entry point attaches the
# rotating file handler (server/runtime/lifecycle.py setup_logging). At its default that is the
# LIVE server's browserplug-server.log, and a second writer there blocks its rotation on
# Windows. So the whole run logs to a throwaway file, set here before anything starts a
# server; pytest_unconfigure below removes it.
TEST_LOG_DIR = _tempfile.mkdtemp(prefix="browserplug-test-log-")
_os.environ["BROWSERPLUG_LOG_FILE"] = _os.path.join(TEST_LOG_DIR, "server.log")
# Every launcher and /restart a test triggers runs its server on THIS interpreter, never on
# one uv builds: without it a test would sync the user's real server environment, the one
# the live server runs from (shared-py/browserplug_shared/server_env.py, the hermetic seam).
_os.environ.setdefault("BROWSERPLUG_SERVER_PYTHON", _sys.executable)

from browserplug_shared.config_store import STATE_DIR_ENV as _STATE_DIR_ENV
from browserplug_shared.mcp_era import ERA_ENV as _ERA_ENV
from browserplug_shared.ws_token import (WS_HOST_ENV as _WS_HOST_ENV,
                                         WS_TOKEN_ENV as _WS_TOKEN_ENV,
                                         WS_TOKEN_FILE_ENV as _WS_TOKEN_FILE_ENV)

# The server keeps its token, config.json and instance locks in the state directory. Left
# at its default, every server a test builds reads the user's real token and settings in
# %LOCALAPPDATA%\browserplug, and parallel workers share one token file. Each pytest
# process (an xdist worker included) gets a scratch directory, set here before anything
# builds a server and removed in pytest_unconfigure. A token-file override would bypass
# it, so that goes too.
TEST_STATE_DIR = _tempfile.mkdtemp(prefix="browserplug-test-state-")
_os.environ[_STATE_DIR_ENV] = TEST_STATE_DIR
# Exported in the developer's shell, these would change every server in the run: a token
# turns strict auth on, a host moves the WebSocket bind, an era moves the protocol off the
# default. tests/unit/conftest.py removes the same ones for its layer; a test that needs
# one sets it itself.
for _name in (_WS_TOKEN_FILE_ENV, _WS_TOKEN_ENV, _WS_HOST_ENV, _ERA_ENV):
    _os.environ.pop(_name, None)

import logging as _logging
import asyncio
import json
import pytest
import pytest_asyncio
from unittest.mock import Mock, AsyncMock, patch
import os
import logging

import contextlib
import functools
import http.server
import threading
import time
import zlib
import struct
from urllib.parse import urlparse, parse_qs


def _png_chunk(typ, data):
    return (struct.pack(">I", len(data)) + typ + data +
            struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF))


def _solid_png(hexcolor, w, h):
    """Build a solid-color RGB PNG by hand (no Pillow dependency in the server).

    Used by the /slow-image endpoint so screenshot fixtures can load a real,
    network-delayed, known-color image below the fold."""
    r, g, b = int(hexcolor[0:2], 16), int(hexcolor[2:4], 16), int(hexcolor[4:6], 16)
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)  # 8-bit, color type 2 (RGB)
    row = b"\x00" + bytes((r, g, b)) * w                 # filter byte 0 + pixels
    idat = zlib.compress(row * h, 6)
    return sig + _png_chunk(b"IHDR", ihdr) + _png_chunk(b"IDAT", idat) + _png_chunk(b"IEND", b"")

from port_coordinator import get_port_by_type, coordinated_test_ports
from browserplug_shared.mcp_port import MCP_PORT_DEFAULT
from server.server import BrowserPlugServer
from firefox_test_utils import FirefoxTestManager
from test_config import FIREFOX_TEST_CONFIG

# Importing the server used to run logging.basicConfig(level=INFO), so caplog saw INFO
# records. Imports configure nothing now (server/runtime/lifecycle.py setup_logging is for entry
# points), so the harness asks for the level itself.
_logging.getLogger().setLevel(_logging.INFO)

# Store allocated ports for Firefox configuration
_allocated_test_ports = {}


class _QuietHTTPHandler(http.server.SimpleHTTPRequestHandler):
    """SimpleHTTPRequestHandler that doesn't spam the test log.

    Besides static files from tests/fixtures, it implements a tiny JSON API used
    by the fixture pages (e.g. silent-fail-login.html):
      POST /api/login -> always 401 {"error": "invalid credentials"}
      POST /api/echo  -> 200, echoes the request body back as JSON
    """

    def log_message(self, *args, **kwargs):
        pass

    def _send_json(self, status, payload_bytes):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload_bytes)))
        # CORS so fixture pages can fetch() the API even cross-origin
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(payload_bytes)

    def _redirect(self, location, status=302):
        # The target can come from the request (/offsite?to=), so a line break is encoded, not sent.
        location = location.replace("\r", "%0D").replace("\n", "%0A")
        self.send_response(status)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send_html(self, status, html, extra_headers=()):
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for name, value in extra_headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        """Static files, plus a few dynamic routes for navigation/screenshot tests:
          /redirect-chain -> 302 /redirect-2 -> 302 /titled.html (redirect-chain report)
          /slow?ms=N      -> hold N ms then a tiny page (lets a 2nd nav supersede it)
          /slow-image?ms=N&c=RRGGBB -> hold N ms then a solid-color PNG (lazy-image settle)

        And the landing-honesty routes. These reproduce, deterministically,
        the three ways a navigation reported plain success while the browser was somewhere
        else -- the live retail targets that produced them may be fixed by their owners at
        any time, which is exactly why the durable checks live here:
          /mangle-query   -> 308 to ITSELF, dropping one parameter and re-encoding another
                             (the allegro rewrite that produced a wrong price answer)
          /offsite?to=U   -> 302 to an arbitrary, possibly cross-origin, target
          /captcha-wall   -> 302 to a captcha interstitial that returns HTTP 200
          /not-found-page -> 404 carrying a full, normal-looking HTML body
          /set-cookies    -> sets two ordinary cookies and one HttpOnly cookie
        """
        path = urlparse(self.path).path
        if path == "/redirect-chain":
            return self._redirect("/redirect-2")
        if path == "/redirect-2":
            return self._redirect("/titled.html")
        if path == "/mangle-query":
            # Rebuild the query the way the observed retailer did: drop `drop` entirely
            # and turn `+` (a space) into a literal plus. Both degrade the query's MEANING
            # and neither is visible without comparing requested to landed.
            raw = urlparse(self.path).query
            kept = []
            for pair in raw.split("&"):
                if not pair or pair.split("=", 1)[0] == "drop":
                    continue
                key, _, value = pair.partition("=")
                kept.append(f"{key}={value.replace('+', '%2B')}")
            return self._redirect("/mangled-result.html?" + "&".join(kept), status=308)
        if path == "/offsite":
            q = parse_qs(urlparse(self.path).query)
            return self._redirect((q.get("to") or ["/titled.html"])[0])
        if path == "/captcha-wall":
            return self._redirect("/Captcha/Add?ReturnUrl=%2Fcaptcha-wall")
        if path == "/Captcha/Add":
            # A 200, so ONLY the landed URL betrays that this is not the document asked for.
            return self._send_html(200, "<!doctype html><meta charset=utf-8>"
                                        "<title>Weryfikacja</title><h1>Confirm you are human</h1>")
        if path == "/not-found-page":
            # A 404 whose body reads like an ordinary page -- extraction succeeds and looks
            # entirely healthy, so the status is the only signal that anything went wrong.
            return self._send_html(404, "<!doctype html><meta charset=utf-8><title>Not Found</title>"
                                        "<h1>Not Found</h1><p>The page you asked for is gone, but "
                                        "this body renders perfectly well.</p>")
        if path == "/set-cookies":
            return self._send_html(
                200,
                "<!doctype html><meta charset=utf-8><title>cookies</title>"
                "<h1>cookie fixture</h1><p>Two readable cookies, one HttpOnly.</p>",
                extra_headers=[
                    ("Set-Cookie", "fp=visible1; Path=/"),
                    ("Set-Cookie", "lang=en; Path=/"),
                    # The one that matters: document.cookie CANNOT see this, and nothing
                    # in a page_eval("document.cookie") result says a class was withheld.
                    ("Set-Cookie", "sess=hidden1; Path=/; HttpOnly"),
                ])
        if path == "/slow":
            q = parse_qs(urlparse(self.path).query)
            time.sleep(min(int((q.get("ms") or ["3000"])[0]), 30000) / 1000.0)
            body = b"<!doctype html><meta charset=utf-8><title>slow</title><h1>SLOW_OK</h1>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/slow-image":
            q = parse_qs(urlparse(self.path).query)
            time.sleep(min(int((q.get("ms") or ["300"])[0]), 10000) / 1000.0)
            png = _solid_png((q.get("c") or ["2266dd"])[0], 600, 600)
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(png)))
            self.end_headers()
            self.wfile.write(png)
            return
        return super().do_GET()

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        if self.path == "/api/login":
            self._send_json(401, json.dumps({"error": "invalid credentials"}).encode("utf-8"))
        elif self.path == "/api/echo":
            self._send_json(200, body if body else b"{}")
        else:
            self._send_json(404, json.dumps({"error": "not found"}).encode("utf-8"))


@contextlib.contextmanager
def _serve_fixtures():
    """Run one _QuietHTTPHandler over tests/fixtures/ on an ephemeral port."""
    fixtures_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
    handler = functools.partial(_QuietHTTPHandler, directory=fixtures_dir)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}/"
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.fixture(scope="session")
def fixtures_server():
    """Serve tests/fixtures/ over HTTP on an ephemeral localhost port.

    Yields the base URL (e.g. http://127.0.0.1:NNNNN/). These are deterministic,
    login-free pages that reproduce the hostile patterns from the agent-experience
    feedback (reactive inputs, deferred modals, hidden twins, frames, tall pages).
    Also exposes POST /api/login (always 401) and POST /api/echo (200 echo) for
    the silent-failure / network-visibility fixtures.
    Local-only — consistent with the project's "no external services" rule.
    """
    with _serve_fixtures() as base_url:
        yield base_url


@pytest.fixture(scope="session")
def fixtures_server_alt():
    """A SECOND fixtures server on a different port — i.e. a different ORIGIN.

    Exists only so the landing-honesty check can be proven against a genuinely
    cross-origin redirect without reaching for a third-party site. Same content,
    different port; a redirect from one to the other is the local stand-in for the
    live case where a requested product page landed on en.wikipedia.org and the
    agent then spent 20.6 KB of context summarising Wikipedia.
    """
    with _serve_fixtures() as base_url:
        yield base_url


@pytest.fixture(scope="session", autouse=True)
def auto_dynamic_ports():
    """
    Automatically patch BrowserPlugServer to use dynamic ports in tests.
    This fixture runs automatically for all tests and monkey-patches
    BrowserPlugServer instantiation to use dynamic ports when none are specified.
    """
    # No manual cleanup needed - get_port_by_type() handles allocation internally

    # Import here to avoid circular imports
    try:
        from server.server import BrowserPlugServer
        original_init = BrowserPlugServer.__init__

        # Forward any kwargs we don't explicitly remap (e.g. the fork's use_stdio)
        # straight through, so this patch never falls out of sync with
        # BrowserPlugServer.__init__ again when the fork adds constructor params.
        def patched_init(self, host="localhost", port=None, mcp_port=None, start_mcp=True, **kwargs):
            # ALWAYS allocate dynamic ports in test environment to prevent conflicts
            # This overrides any explicit port to ensure complete isolation
            if port is None or port == 8772:  # Override default port
                port = get_port_by_type('test_individual')
            if (mcp_port is None or mcp_port == MCP_PORT_DEFAULT) and start_mcp:  # Override default MCP port
                mcp_port = get_port_by_type('test_mcp_individual')

            # Store ports for potential Firefox use
            _allocated_test_ports['websocket'] = port
            if mcp_port:
                _allocated_test_ports['mcp'] = mcp_port

            print(f"🔧 Test server using WebSocket port: {port}, MCP port: {mcp_port}")

            # Call original init with dynamic ports
            return original_init(self, host=host, port=port, mcp_port=mcp_port, start_mcp=start_mcp, **kwargs)

        # Apply the patch
        with patch.object(BrowserPlugServer, '__init__', patched_init):
            yield

    except ImportError:
        # If BrowserPlugServer can't be imported, just yield without patching
        yield

@pytest.fixture(scope="function")
def firefox_with_test_ports():
    """
    Provides a Firefox configuration that uses the currently allocated test ports.
    This can be used to create Firefox instances that connect to test servers.
    """
    try:
        from firefox_test_utils import FirefoxTestManager

        # Get the most recently allocated ports
        websocket_port = _allocated_test_ports.get('websocket')
        if websocket_port:
            return FirefoxTestManager(test_port=websocket_port)
        else:
            # Fallback: allocate a new port
            port = get_port_by_type('test_individual')
            return FirefoxTestManager(test_port=port)

    except ImportError:
        return None


def _integration_browsers():
    """Which browser engines the parametrized integration fixture runs over.

    Default: both firefox and chromium (parity by construction — every integration test
    runs on both engines unless it is VISIBLY exempted with an engine-only marker). The
    `BROWSERPLUG_TEST_BROWSERS` env var (comma list, e.g. "firefox" or "chromium,firefox")
    overrides the set — useful to run a single engine on a box that has only one, or to
    bisect an engine-specific failure. Unknown names are dropped; an empty result falls
    back to firefox so collection never yields zero params.
    """
    override = os.environ.get("BROWSERPLUG_TEST_BROWSERS")
    if override:
        wanted = [b.strip().lower() for b in override.split(",") if b.strip()]
    else:
        wanted = ["firefox", "chromium"]
    ordered = []
    for b in wanted:
        if b in ("firefox", "chromium") and b not in ordered:
            ordered.append(b)
    return ordered or ["firefox"]


# Computed once at import; visible in test ids as [firefox] / [chromium].
_INTEGRATION_BROWSERS = _integration_browsers()


def pytest_configure(config):
    """Give each xdist worker its own --log-file: `<name>-gw0.log` beside the file the
    caller named. Workers inherit the caller's arguments, so without this they all write
    one file and their setup lines interleave. Runs before pytest's logging plugin opens
    the file (that one is trylast)."""
    worker = os.environ.get("PYTEST_XDIST_WORKER")
    log_file = config.getoption("log_file", None)
    if worker and log_file:
        stem, ext = os.path.splitext(log_file)
        config.option.log_file = f"{stem}-{worker}{ext or '.log'}"


def pytest_runtest_logstart(nodeid, location):
    """Mark where each test starts in the --log-file. A worker runs many tests one after
    another, and without the mark its log has no test boundaries."""
    logging.getLogger("tests").info(f"---- {nodeid}")


def pytest_unconfigure(config):
    """Close and delete the throwaway server log and the scratch state directory set at
    the top of this file."""
    import shutil
    root = logging.getLogger()
    prefix = os.path.normcase(os.path.abspath(TEST_LOG_DIR))
    for handler in list(root.handlers):
        name = getattr(handler, "baseFilename", "")
        if name and os.path.normcase(os.path.abspath(name)).startswith(prefix):
            root.removeHandler(handler)
            handler.close()
    shutil.rmtree(TEST_LOG_DIR, ignore_errors=True)
    shutil.rmtree(TEST_STATE_DIR, ignore_errors=True)


def pytest_collection_modifyitems(config, items):
    """Engine-marker DESELECT for the parametrized integration fixture.

    A test carrying `firefox_only` / `chromium_only` that got collected under the WRONG
    browser param is DESELECTED (removed cleanly), which is tidier than emitting a skip
    for every mismatched combo. The binary-availability SKIP stays in the fixture — that
    is the one sanctioned skip family (a browser is not installed on this box).

    The browser param propagates into `callspec.params['server_with_extension']` for any
    test that depends on the fixture directly OR transitively (mcp / full_mcp_system /
    full_monitoring_system all chain to it), so this covers those too. Tests that do not
    use the fixture have no browser param and are never deselected here.
    """
    selected, deselected = [], []
    for item in items:
        callspec = getattr(item, 'callspec', None)
        browser = callspec.params.get('server_with_extension') if callspec else None
        if browser is None:
            selected.append(item)
            continue
        firefox_only = item.get_closest_marker('firefox_only')
        chromium_only = item.get_closest_marker('chromium_only')
        if firefox_only and browser != 'firefox':
            deselected.append(item)
        elif chromium_only and browser != 'chromium':
            deselected.append(item)
        else:
            selected.append(item)
    if deselected:
        config.hook.pytest_deselected(items=deselected)
        items[:] = selected


# --- Startup readiness --------------------------------------------------------------
# The fixture used to sleep a fixed firefox_startup_wait (5 s) after launching the
# browser. time.sleep blocked the event loop the WebSocket server runs on, so the extension
# could not even connect until it ended: about 5 s of every test, a third of the sweep.
# The fixture launches without that sleep and waits for the state a test's first call
# needs, which the 25 s connection budget already bounded. Revert this block and the two
# `wait_for_startup=False` arguments below to get the sleep back.

def _browser_not_ready(server):
    """None once every connected browser can take a tool call, else what is missing.

    Registered is not enough: the hello names the browser (version, proxy grant, which
    tests read at once), and the first browser_state push shows the browser has a window
    with a tab in it. The extension sends both right after its socket opens, and a later
    push follows when the first window or tab appears.
    """
    browsers = server.live_browsers()
    if not browsers:
        return "no browser is registered"
    for b in browsers:
        if b.provisional:
            return "a browser registered but its extension_hello has not been applied"
        state = b.browser_state or {}
        if state.get("tabs", 0) < 1 or state.get("windows", 1) < 1:
            return (f"a browser has not reported a window with a tab yet "
                    f"(last browser_state: {b.browser_state})")
    return None


async def _await_browser_ready(server, timeout):
    """Poll _browser_not_ready until it clears or `timeout` passes; returns its last word."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        missing = _browser_not_ready(server)
        if missing is None or loop.time() >= deadline:
            return missing
        await asyncio.sleep(0.05)
# --- end of startup readiness ---------------------------------------------------------


@pytest_asyncio.fixture(params=_INTEGRATION_BROWSERS, ids=lambda b: b)
async def server_with_extension(request):
    """
    Shared fixture for starting server + a real browser extension for integration testing.

    PARAMETRIZED over the browser ENGINE (`params=["firefox","chromium"]` by default, or
    the `BROWSERPLUG_TEST_BROWSERS` override). `request.param` selects FirefoxTestManager vs
    ChromiumTestManager — both expose the same surface (setup_and_start_firefox /
    _start_firefox / stop_firefox / cleanup / (async_)wait_for_extension_connection /
    _profile_points_at_test_port / the `firefox_process` attribute) so everything below is
    engine-neutral and the downstream `setup['firefox']`/`setup['server']` reads unchanged.
    Engine-specific modules opt out with a `firefox_only`/`chromium_only` marker (deselected
    by pytest_collection_modifyitems above).

    FUNCTION-SCOPED ON PURPOSE: every test gets its own server + fresh-profile browser,
    so cross-test browser-state pollution cannot occur and failures are self-contained.
    Cost: about 2 s of browser start per test. Before "optimizing" this to scope="module", read the
    module-scope note in .tmp/dev-docs/testing.md — it requires loop_scope="module" + per-module
    asyncio markers AND suite-wide cleanup discipline.

    Returns:
        FixtureResult behaving as both dict and tuple: (server, firefox, test_port).
    """
    browser = request.param

    # --- Browser-availability gate (the ONE sanctioned skip), applied BEFORE we start a
    #     server so a skipped param never leaks a running server/port. Mirrors the historic
    #     Firefox FIREFOX_PATH gate; the chromium twin resolves CHROME_PATH / dist CfT / PATH.
    firefox_path = None
    chrome_path = None
    if browser == "firefox":
        firefox_path = os.environ.get('FIREFOX_PATH', 'firefox')
        if not os.path.exists(os.path.expanduser(firefox_path)):
            pytest.skip(f"Firefox not found at {firefox_path}")
    elif browser == "chromium":
        from chromium_test_utils import ChromiumTestManager, resolve_chrome_binary
        chrome_path = resolve_chrome_binary()
        if not chrome_path:
            pytest.skip("Chromium binary not found (CHROME_PATH / dist chrome-for-testing / PATH)")
    else:
        pytest.skip(f"Unknown browser param: {browser}")

    # Use dynamic port allocation
    with coordinated_test_ports() as (ports, coord_file):
        test_port = ports['websocket']
        mcp_port = ports['mcp']

        # Create server
        server = BrowserPlugServer(
            host="localhost",
            port=test_port,
            mcp_port=mcp_port,
            start_mcp=True
        )

        # Start server
        server_task = asyncio.create_task(server.start_server())
        await asyncio.sleep(0.1)  # Let server start

        # Build the manager for this engine. `firefox` keeps its name for downstream
        # compatibility (FixtureResult.firefox) even when it is a ChromiumTestManager.
        if browser == "firefox":
            firefox = FirefoxTestManager(
                firefox_path=firefox_path,
                test_port=test_port,
                coordination_file=coord_file
            )
        else:
            firefox = ChromiumTestManager(
                chrome_path=chrome_path,
                test_port=test_port,
                coordination_file=coord_file
            )

        try:
            # Set up Firefox with extension and start it
            success = firefox.setup_and_start_firefox(headless=True, wait_for_startup=False)
            # setup_and_start_firefox returns as soon as the browser process exists.
            started_at = time.monotonic()
            if not success:
                # Fail-closed backstop. setup_and_start_firefox now RAISES on any genuine
                # setup failure (missing XPI, broken profile, crashed bootstrap) and returns
                # False ONLY for a missing Firefox binary — which we already skipped on above
                # (the os.path.exists check). So reaching here with success=False should be
                # unreachable; if it ever does, FAIL loudly rather than mask it as a skip
                # (a green "skipped" reads as "not applicable", not "this is broken").
                raise AssertionError(
                    "Firefox setup returned False despite the binary existing — unreachable "
                    "under the current setup_and_start_firefox contract (it raises on real "
                    "failures). Treating as a hard failure, not a skip."
                )

            # Wait for extension to connect using awaitable mechanism
            connected = await firefox.async_wait_for_extension_connection(
                timeout=FIREFOX_TEST_CONFIG['extension_install_wait'], server=server
            )

            # ONE retry, and only for the cause we can actually rule in: Firefox came up but
            # its extension never opened a socket. Under a full-suite load that happens to
            # roughly 1 startup in 200. A retry here is not masking — a genuinely broken WS
            # contract, a failed token bootstrap or a handler exception fails BOTH attempts,
            # and the second failure below still reports everything. Loud on purpose: if this
            # line shows up often, the startup path has a real problem.
            if not connected:
                print(f"⚠ Extension did not connect within "
                      f"{FIREFOX_TEST_CONFIG['extension_install_wait']}s — restarting Firefox once")
                firefox.stop_firefox()
                # relaunch the browser only — the profile is built, seeded and verified
                if firefox._start_firefox(headless=True, wait_for_startup=False):
                    started_at = time.monotonic()
                    connected = await firefox.async_wait_for_extension_connection(
                        timeout=FIREFOX_TEST_CONFIG['extension_install_wait'], server=server
                    )

            # NOT a skip. "The extension never connected" is the single most important
            # failure this suite can observe — it is what a broken WS contract, a failed
            # token bootstrap, or an exception thrown out of a message handler all look
            # like. Skipping it turned a real regression green once already (the registry
            # changed handle_extension_message's signature and the tracker broke); a whole
            # module reporting "skipped" reads like "not applicable", not "the product is
            # broken". Fail, and hand over the state needed to tell those apart.
            if not connected:
                browsers = getattr(server, 'browsers', None)
                alive = firefox.firefox_process and firefox.firefox_process.poll() is None
                # The stamped port is the one thing that would send the extension somewhere
                # else entirely (a stale cached profile => it dials a dead port and we see
                # NO connection attempt at all, which is exactly what this looks like).
                try:
                    stamped = firefox._profile_points_at_test_port()
                except Exception as e:
                    stamped = f"could not read: {e}"
                raise AssertionError(
                    f"Extension never connected to the test server on port {test_port} "
                    f"within {FIREFOX_TEST_CONFIG['extension_install_wait']}s (2 attempts).\n"
                    f"  Firefox still running: {alive}\n"
                    f"  profile points at {test_port}: {stamped}\n"
                    f"  browsers registered:   {list(browsers) if browsers is not None else 'n/a'}\n"
                    "Look in the captured server log for: a 4401 auth rejection (token "
                    "bootstrap failed), or an exception out of the message handler (both "
                    "drop the socket). If Firefox died, it is a profile/XPI problem."
                )

            missing = await _await_browser_ready(
                server, FIREFOX_TEST_CONFIG['extension_install_wait'])
            if missing:
                raise AssertionError(
                    f"The extension connected to the test server on port {test_port} but "
                    f"was not ready within {FIREFOX_TEST_CONFIG['extension_install_wait']}s: "
                    f"{missing}.")
            print(f"✓ {browser} ready {time.monotonic() - started_at:.2f}s after launch")

            # Return a dict that also acts like a tuple for backward compatibility
            # This allows both patterns:
            # - setup = server_with_extension; setup['server']
            # - server, firefox, test_port = server_with_extension

            class FixtureResult:
                def __init__(self, server, firefox, test_port, mcp_port, ports, engine):
                    self.server = server
                    self.firefox = firefox
                    self.test_port = test_port
                    self.mcp_port = mcp_port
                    self.ports = ports
                    # The browser engine this parametrization drives ("firefox" |
                    # "chromium"). Tests read it to make eval-based VERIFICATION
                    # engine-aware: page_eval needs the un-grantable "Allow user
                    # scripts" toggle on headless Chromium (EVAL_UNAVAILABLE), so a
                    # chromium run verifies DOM state via page_read/page_snapshot/
                    # page_act instead. The FEATURE under test still runs on both.
                    self.engine = engine

                def __getitem__(self, key):
                    return getattr(self, key)

                def __iter__(self):
                    yield self.server
                    yield self.firefox
                    yield self.test_port

                def get(self, key, default=None):
                    return getattr(self, key, default)

            yield FixtureResult(server, firefox, test_port, mcp_port, ports, browser)

        finally:
            # Cleanup
            firefox.cleanup()
            await server.shutdown(server_task)


# Test fixtures
@pytest.fixture
def sample_request():
    """Sample WebSocket request message"""
    return {
        "id": "test_001",
        "type": "request",
        "action": "tabs.list",
        "data": {},
        "timestamp": "2025-09-03T12:00:00.000Z"
    }

@pytest.fixture
def sample_response():
    """Sample WebSocket response message"""
    return {
        "id": "test_001", 
        "type": "response",
        "action": "tabs.list",
        "data": {
            "tabs": [
                {
                    "id": 123,
                    "windowId": 1,
                    "url": "https://example.com",
                    "title": "Example Page",
                    "active": True,
                    "index": 0,
                    "pinned": False
                }
            ]
        },
        "timestamp": "2025-09-03T12:00:01.000Z"
    }

@pytest.fixture
def sample_error():
    """Sample WebSocket error message"""
    return {
        "id": "test_001",
        "type": "error",
        "action": "tabs.close", 
        "data": {
            "code": "TAB_NOT_FOUND",
            "message": "Tab with ID 999 not found",
            "details": {"tabId": 999}
        },
        "timestamp": "2025-09-03T12:00:01.000Z"
    }

@pytest.fixture
def mock_websocket():
    """Mock WebSocket connection"""
    mock_ws = AsyncMock()
    mock_ws.send = AsyncMock()
    mock_ws.close = AsyncMock()
    mock_ws.remote_address = ("127.0.0.1", 12345)
    return mock_ws

@pytest.fixture
def mock_chrome_api():
    """Mock Chrome extension API"""
    chrome_mock = Mock()
    
    # Mock chrome.tabs
    chrome_mock.tabs = Mock()
    chrome_mock.tabs.query = Mock()
    chrome_mock.tabs.create = Mock() 
    chrome_mock.tabs.remove = Mock()
    chrome_mock.tabs.update = Mock()
    
    # Mock chrome.history
    chrome_mock.history = Mock()
    chrome_mock.history.search = Mock()
    chrome_mock.history.deleteUrl = Mock()
    chrome_mock.history.deleteRange = Mock()

    return chrome_mock

@pytest.fixture
def event_loop():
    """Create event loop for async tests"""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()

@pytest.fixture
def sample_tab_data():
    """Sample tab data for testing"""
    return {
        "id": 123,
        "windowId": 1,
        "url": "https://example.com",
        "title": "Example Page", 
        "active": True,
        "index": 0,
        "pinned": False,
        "favIconUrl": "https://example.com/favicon.ico"
    }

@pytest.fixture 
def sample_history_data():
    """Sample history data for testing"""
    return [
        {
            "id": "hist_123",
            "url": "https://github.com/user/repo",
            "title": "GitHub Repository",
            "visitTime": "2025-09-02T14:30:00.000Z",
            "visitCount": 5
        },
        {
            "id": "hist_124", 
            "url": "https://example.com",
            "title": "Example Site",
            "visitTime": "2025-09-02T15:00:00.000Z", 
            "visitCount": 2
        }
    ]
