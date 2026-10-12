"""
Chromium testing utilities for BrowserPlug (the Blink/MV3 twin of firefox_test_utils.py).

ChromiumTestManager mirrors FirefoxTestManager's public surface — same method NAMES and
return shapes (setup_and_start_firefox / _start_firefox / stop_firefox / cleanup /
wait_for_extension_connection / async_wait_for_extension_connection /
_profile_points_at_test_port, plus the `firefox_process` attribute) — so the parametrized
`server_with_extension` fixture can drive either manager without branching in its body.

Config seeding (design D12): there is no LevelDB/storage seeding and no profile cache. The
per-session BAKED extension dir is a copy of extension-chromium/ with `config.js` REWRITTEN
per test to
    const BUILD_CONFIG = { wsPort: <test port>, wsHost: "localhost", testMode: true };
The SW imports config.js via importScripts(); a non-null BUILD_CONFIG wins over storage
config UNCONDITIONALLY, so the extension dials ONLY the test port. This is the fail-closed
analog of the gecko sqlite-stamp: if the bake failed, config.js is missing, importScripts
throws, and the SW is inert — it can never reach the developer's live :8772. The bake is
READ BACK and asserted before every launch (mirrors _apply_test_port's read-back).

Isolation vs. the developer's live browser: because BUILD_CONFIG pins the test port and is
verified by read-back, a chromium test profile can never fall back to ws://localhost:8772.
(Chrome loads unpacked extensions via --load-extension with no extensions.json / signing
dance, so there is no bootstrap-Firefox phase and no dead-SOCKS block to manage.)
"""

import os
import json
import shutil
import subprocess
import tempfile
import atexit
import threading
import time
from pathlib import Path

# Set up consistent imports

from test_config import FIREFOX_TEST_CONFIG, get_firefox_test_port
from port_coordinator import FirefoxPortCoordinator, coordinated_mcp_port

project_root = Path(__file__).resolve().parent.parent

# The port the extension falls back to when it has no baked/stored config — the developer's
# LIVE shared server. A chromium test bake must never let the SW reach it; the BUILD_CONFIG
# read-back guard (_profile_points_at_test_port) is what enforces that here.
DEFAULT_EXTENSION_WS_PORT = 8772

# The MV3 source tree the harness bakes from.
_CHROMIUM_EXT_DIR = project_root / 'extension-chromium'

# The vendored Chrome for Testing binary (v154), used when CHROME_PATH is unset.
_DIST_CHROME = project_root / 'dist' / 'chrome-for-testing' / 'chrome-win64' / 'chrome.exe'


def resolve_chrome_binary(explicit=None):
    """Resolve the Chromium binary: explicit arg → CHROME_PATH env → dist CfT → PATH.

    Returns an absolute path string, or None when no Chromium is installed on this box
    (which is the browser-availability gate — the ONE sanctioned skip).
    """
    candidates = []
    if explicit:
        candidates.append(explicit)
    env = os.environ.get('CHROME_PATH')
    if env:
        candidates.append(env)
    candidates.append(str(_DIST_CHROME))
    for name in ('chrome', 'chrome.exe', 'google-chrome', 'chromium', 'chromium-browser'):
        which = shutil.which(name)
        if which:
            candidates.append(which)

    for cand in candidates:
        if not cand:
            continue
        expanded = os.path.expanduser(cand)
        if os.path.exists(expanded):
            return expanded
    return None


class ChromiumTestManager:
    """Manage one Chrome-for-Testing instance for integration tests.

    Surface-compatible with FirefoxTestManager so the shared fixture can treat the two
    uniformly. The chrome Popen is stored in `self.firefox_process` on purpose (the
    fixture reads `.firefox_process.poll()` to tell a crashed browser from a browser
    whose extension never connected).
    """

    # The expensive copytree of extension-chromium/ (minus config.js) is done ONCE per
    # process and reused across tests; only config.js is rewritten per test (with that
    # test's port). Class-level so concurrent managers in one process share it; a second
    # pytest process gets its own (class state is per-interpreter), so cross-session runs
    # never collide.
    _session_template_dir = None
    _session_lock = threading.Lock()

    _EXTENSION_ID = 'browserplug@marcinwolny.net'   # gecko id; kept for parity of diagnostics only

    def __init__(self, chrome_path=None, test_port=None, coordination_file=None,
                 profile_tag=None, firefox_path=None, mcp_port=None):
        """Create a manager.

        chrome_path / firefox_path: either is accepted for call-site symmetry with
            FirefoxTestManager; chrome_path wins. Falls back to resolve_chrome_binary().
        test_port: the WS port to bake into BUILD_CONFIG (coordination_file overrides).
        profile_tag: distinguishes an ADDITIONAL browser sharing the same server/WS port
            (its own user-data-dir → its own extension instance_id → its own registry
            entry), exactly like FirefoxTestManager's profile_tag.
        mcp_port: the test server's MCP HTTP port, baked as the extension's `mcpPort`
            setting so its token bootstrap may fetch /ws-token from it; read from the
            coordination file when that names one.
        """
        self.chrome_path = chrome_path or firefox_path or resolve_chrome_binary()
        self.test_port = test_port or get_firefox_test_port()
        self.coordination_file = coordination_file
        self.profile_tag = profile_tag
        self.mcp_port = mcp_port or coordinated_mcp_port(coordination_file)

        # Named firefox_process for uniform access from the shared fixture.
        self.firefox_process = None
        self.user_data_dir = None
        self.baked_dir = None
        self._chrome_log_path = None
        self._chrome_log_handle = None
        # Kept for FirefoxTestManager attribute-compat (some diagnostics read it).
        self.profile_dir = None

    # ---- binary availability gate ------------------------------------------------
    @staticmethod
    def available(chrome_path=None):
        """True when a Chromium binary can be resolved (the browser-availability gate)."""
        return resolve_chrome_binary(chrome_path) is not None

    # ---- per-session bake --------------------------------------------------------
    @classmethod
    def _ensure_session_template(cls):
        """Copy extension-chromium/ (minus config.js) into a per-process temp dir once."""
        with cls._session_lock:
            if cls._session_template_dir and os.path.isdir(cls._session_template_dir):
                return cls._session_template_dir
            if not _CHROMIUM_EXT_DIR.is_dir():
                raise FileNotFoundError(
                    f"extension-chromium source tree not found at {_CHROMIUM_EXT_DIR}")
            dest = tempfile.mkdtemp(prefix='browserplug-chromium-ext-session-')
            # copytree needs a non-existent target; copy into a child dir.
            baked = os.path.join(dest, 'ext')

            def _ignore(dirpath, names):
                # Exclude config.js at the TOP LEVEL only — it is written per test.
                if Path(dirpath) == _CHROMIUM_EXT_DIR:
                    return {'config.js'}
                return set()

            shutil.copytree(_CHROMIUM_EXT_DIR, baked, ignore=_ignore)
            cls._session_template_dir = baked
            atexit.register(cls._cleanup_session_template, dest)
            print(f"✓ Baked chromium extension session template at {baked}")
            return baked

    @classmethod
    def _cleanup_session_template(cls, dest):
        shutil.rmtree(dest, ignore_errors=True)

    def _bake_config(self):
        """Rewrite config.js in the baked dir with THIS test's port, then read it back.

        This is the fail-closed config-seed: a bake that does not take (missing file or a
        port that did not stamp) raises rather than launching a browser that might dial
        the developer's live :8772.
        """
        if not self.baked_dir:
            raise RuntimeError("baked_dir not set — call setup first")
        cfg = os.path.join(self.baked_dir, 'config.js')
        # The SW seeds chrome.storage.sync from BUILD_CONFIG.storageSeed under testMode so
        # the popup/options/test-helper reads (which read storage.sync) reflect the test
        # port — the MV3 analog of the gecko harness stamping storage-sync-v2.sqlite. This
        # payload MIRRORS firefox_test_utils.FirefoxTestManager._test_storage_config so the
        # UI-sync tests assert the same values on both engines. The WS connection itself
        # still uses wsPort/wsHost unconditionally (independent of this seed).
        storage_seed = {
            "hostname": "localhost",
            "port": self.test_port,
            "retryInterval": 1000,
            "maxRetries": 5,
            "pingTimeout": 2000,
            "testPort": self.test_port,
            "testHostname": "localhost",
        }
        if self.mcp_port:
            # The extension's token bootstrap fetches /ws-token only from the MCP port it is
            # configured for (or 2830..2942); the test server's is random.
            storage_seed["mcpPort"] = self.mcp_port
        content = (
            "/* Baked by ChromiumTestManager — BUILD_CONFIG wins over storage (design D12). */\n"
            'const BUILD_CONFIG = { wsPort: %d, wsHost: "localhost", testMode: true, '
            # The popup switch a user turns on for the browser-wide proxy; a headless test
            # browser has nobody to click it (background.js hasProxyPermission).
            'allowBrowserProxy: true, '
            'storageSeed: %s };\n'
            % (self.test_port, json.dumps(storage_seed))
        )
        with open(cfg, 'w', encoding='utf-8') as f:
            f.write(content)

        if not self._profile_points_at_test_port():
            raise RuntimeError(
                f"config.js bake did not take test port {self.test_port} "
                f"(read-back failed) — refusing to launch Chrome")
        print(f"✓ Baked config.js with test port {self.test_port} (read-back OK)")
        return True

    def _profile_points_at_test_port(self):
        """True when the baked config.js really pins THIS test's port (not :8772).

        Mirrors FirefoxTestManager._profile_points_at_test_port so the fixture's failure
        diagnostics can call it uniformly. For chromium the config carrier is config.js's
        BUILD_CONFIG, not storage-sync-v2.sqlite.
        """
        if not self.baked_dir:
            return False
        cfg = os.path.join(self.baked_dir, 'config.js')
        try:
            with open(cfg, 'r', encoding='utf-8') as f:
                content = f.read()
        except OSError:
            return False
        return (f"wsPort: {self.test_port}" in content
                and self.test_port != DEFAULT_EXTENSION_WS_PORT)

    def _setup_extension_test_config(self):
        """Settle self.test_port (coordination file wins), mirroring the gecko manager."""
        if self.coordination_file:
            try:
                self.test_port = FirefoxPortCoordinator.read_ports(
                    self.coordination_file)['websocket']
                print(f"✓ Chromium extension configured from coordination file "
                      f"for port {self.test_port}")
                return
            except Exception as e:
                print(f"⚠ Failed to use coordination file: {e}, falling back to test_port")
        print(f"✓ Pre-configured chromium extension for test server port {self.test_port}")

    # ---- launch / relaunch -------------------------------------------------------
    def _launch_flags(self, headless):
        flags = [
            self.chrome_path,
            f"--user-data-dir={self.user_data_dir}",
            f"--load-extension={self.baked_dir}",
            f"--disable-extensions-except={self.baked_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--silent-debugger-extension-api",
        ]
        # --headless=new is the verified-working mode; a non-headless run (debugging) just
        # drops the flag.
        if headless:
            flags.insert(1, "--headless=new")
        return flags

    def _start_firefox(self, headless=True, wait_for_startup=True):
        """(Re)launch Chrome against the current baked dir + user-data-dir.

        Named _start_firefox to match FirefoxTestManager so the fixture's ONE retry
        (stop_firefox → _start_firefox) works for both browsers unchanged. Fresh
        user-data-dir if none exists yet; re-asserts the config bake fail-closed first.
        """
        if not self.chrome_path or not os.path.exists(self.chrome_path):
            print(f"✗ Chrome binary not found at {self.chrome_path}")
            return False
        if not self.baked_dir:
            self.baked_dir = self._ensure_session_template()
        if not self.user_data_dir:
            self.user_data_dir = tempfile.mkdtemp(prefix='browserplug-new-profile-chromium-')
            self.profile_dir = self.user_data_dir

        # Fail-closed: never launch a browser whose config does not pin the test port.
        self._bake_config()

        flags = self._launch_flags(headless)
        # Capture Chrome's stdout/stderr for post-mortem on a connection failure. No extra
        # logging flags are added (Chrome behavior unchanged); this just catches crashes.
        try:
            self._chrome_log_path = os.path.join(self.user_data_dir, 'chrome-stdouterr.log')
            self._chrome_log_handle = open(self._chrome_log_path, 'wb')
            self.firefox_process = subprocess.Popen(
                flags, stdout=self._chrome_log_handle, stderr=subprocess.STDOUT)
        except Exception as e:
            print(f"✗ Failed to start Chrome: {e}")
            return False

        if wait_for_startup:
            time.sleep(FIREFOX_TEST_CONFIG['firefox_startup_wait'])
            if self.firefox_process.poll() is not None:
                print(f"✗ Chrome exited immediately (code: {self.firefox_process.returncode})")
                return False

        print(f"✓ Chrome started with baked extension (PID: {self.firefox_process.pid})")
        return True

    def setup_and_start_firefox(self, headless=True, wait_for_startup=True,
                                skip_on_failure=True):
        """Bake the extension for this test's port and launch Chrome.

        Return contract mirrors FirefoxTestManager exactly: returns False ONLY for the one
        legitimate skip — the Chromium binary is not on this box. Every OTHER failure
        (bake did not take, Chrome crashed on start) RAISES with diagnostics, so a real
        breakage is never masked behind a green skip.
        """
        self.chrome_path = self.chrome_path or resolve_chrome_binary()
        if not self.chrome_path or not os.path.exists(self.chrome_path):
            print(f"✗ Chromium binary not found (looked at CHROME_PATH, {_DIST_CHROME}, PATH)")
            if skip_on_failure:
                return False
            raise RuntimeError("Chromium binary not found")

        try:
            self._setup_extension_test_config()
            self.baked_dir = self._ensure_session_template()
            self.user_data_dir = tempfile.mkdtemp(prefix='browserplug-new-profile-chromium-')
            self.profile_dir = self.user_data_dir

            started = self._start_firefox(headless=headless, wait_for_startup=wait_for_startup)
            if not started:
                raise RuntimeError(
                    "Chrome failed to start (binary present, extension baked, but the "
                    "process exited immediately — see chrome-stdouterr.log)")
            return True
        except Exception as e:
            raise RuntimeError(
                f"Chromium test setup failed after the binary was found at "
                f"{self.chrome_path}: {type(e).__name__}: {e} — this is a real "
                "bake/launch failure, not a missing browser, so it is not skippable"
            ) from e

    # ---- connection waits (browser-neutral; mirror FirefoxTestManager) ----------
    def wait_for_extension_connection(self, timeout=10.0, server=None):
        """Sync wait — see FirefoxTestManager.wait_for_extension_connection."""
        print(f"Waiting up to {timeout}s for extension to connect to port {self.test_port}...")
        if server and hasattr(server, 'wait_for_extension_connection'):
            import asyncio
            try:
                try:
                    loop = asyncio.get_event_loop()
                except RuntimeError:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                connected = loop.run_until_complete(
                    server.wait_for_extension_connection(timeout=timeout))
                print("✓ Extension connected to server" if connected
                      else "✗ Extension connection timeout")
                return connected
            except Exception as e:
                print(f"✗ Error waiting for extension connection: {e}")

        time.sleep(FIREFOX_TEST_CONFIG['extension_install_wait'])
        if not (self.firefox_process and self.firefox_process.poll() is None):
            print("✗ Chrome process not running")
            return False
        if server is not None and hasattr(server, 'live_browsers'):
            if server.live_browsers():
                print("✓ Extension connected (server registry shows a live browser)")
                return True
            print("✗ Chrome is running but no browser registered with the server")
            return False
        print("⚠ Chrome running but no server to verify the connection — not claiming success")
        return False

    async def async_wait_for_extension_connection(self, timeout=10.0, server=None):
        """Async wait — see FirefoxTestManager.async_wait_for_extension_connection."""
        print(f"Waiting up to {timeout}s for extension to connect to port {self.test_port}...")
        if server and hasattr(server, 'wait_for_extension_connection'):
            try:
                connected = await server.wait_for_extension_connection(timeout=timeout)
                print("✓ Extension connected to server" if connected
                      else "✗ Extension connection timeout")
                return connected
            except Exception as e:
                print(f"✗ Error waiting for extension connection: {e}")

        import asyncio
        await asyncio.sleep(FIREFOX_TEST_CONFIG['extension_install_wait'])
        if not (self.firefox_process and self.firefox_process.poll() is None):
            print("✗ Chrome process not running")
            return False
        if server is not None and hasattr(server, 'live_browsers'):
            if server.live_browsers():
                print("✓ Extension connected (server registry shows a live browser)")
                return True
            print("✗ Chrome is running but no browser registered with the server")
            return False
        print("⚠ Chrome running but no server to verify the connection — not claiming success")
        return False

    # ---- teardown ----------------------------------------------------------------
    def stop_firefox(self):
        """Kill the Chrome PROCESS TREE (parent + renderers/gpu/utility children).

        Chrome forks helper processes; terminate() on the parent alone orphans them, so we
        taskkill /T the tree on Windows and fall back to killpg elsewhere.
        """
        proc = self.firefox_process
        if not proc:
            return
        try:
            if proc.poll() is None:
                if os.name == 'nt':
                    subprocess.run(
                        ['taskkill', '/F', '/T', '/PID', str(proc.pid)],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                else:
                    proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
            print("✓ Chrome stopped")
        except Exception as e:
            print(f"⚠ Error stopping Chrome: {e}")
        finally:
            self.firefox_process = None
            if self._chrome_log_handle:
                try:
                    self._chrome_log_handle.close()
                except Exception:
                    pass
                self._chrome_log_handle = None

    def chrome_log_tail(self, n=4000):
        """Return the tail of Chrome's captured stdout/stderr (for failure diagnostics)."""
        if not self._chrome_log_path or not os.path.exists(self._chrome_log_path):
            return "(no chrome log)"
        try:
            with open(self._chrome_log_path, 'r', errors='replace') as f:
                return f.read()[-n:]
        except OSError as e:
            return f"(could not read chrome log: {e})"

    def cleanup(self):
        """Stop Chrome and remove this test's user-data-dir (session template survives)."""
        self.stop_firefox()
        if self.user_data_dir and os.path.exists(self.user_data_dir):
            # A just-killed Chrome can hold profile files for a beat on Windows; retry.
            for attempt in range(4):
                try:
                    shutil.rmtree(self.user_data_dir)
                    print("✓ Chrome user-data-dir cleaned up")
                    break
                except OSError:
                    if attempt == 3:
                        print(f"⚠ Could not remove user-data-dir {self.user_data_dir}")
                    else:
                        time.sleep(0.5)
        self.user_data_dir = None
        self.profile_dir = None
        self.baked_dir = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.cleanup()


def start_additional_chromium(test_port, profile_tag='c', chrome_path=None, headless=True,
                              mcp_port=None):
    """Start a SECOND (third, ...) Chrome against an already-running test server.

    The Blink twin of start_additional_firefox: same server/WS port, its OWN user-data-dir
    (→ its own extension instance_id → a second registry entry). Returns a started
    ChromiumTestManager, or None if the binary is missing (caller may skip). The caller
    owns cleanup() — always in a finally.
    """
    manager = ChromiumTestManager(
        chrome_path=chrome_path or resolve_chrome_binary(),
        test_port=test_port,
        profile_tag=profile_tag,
        mcp_port=mcp_port,
    )
    try:
        started = manager.setup_and_start_firefox(headless=headless)
    except Exception:
        manager.cleanup()
        raise
    if not started:
        manager.cleanup()
        return None
    return manager
